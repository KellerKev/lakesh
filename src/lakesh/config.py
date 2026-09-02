"""Profile + config loading.

Config lives at `$LAKESH_CONFIG` if set, else `$XDG_CONFIG_HOME/lakesh/config.toml`,
else `~/.config/lakesh/config.toml`. The file holds named *profiles* plus a
`default` pointer.

Two profile types are supported:

**Iceberg REST catalog** (`type = "iceberg-rest"`, default):

    [profiles.local]
    uri       = "http://127.0.0.1:8181"
    warehouse = "lake"

    [profiles.local.s3]
    endpoint    = "http://127.0.0.1:9000"
    access_key  = "minioadmin"
    secret_key  = "minioadmin"

    [profiles.local.oauth]           # optional
    client_id     = "demo-client"
    client_secret = "demo-secret"

**DuckLake direct** (`type = "ducklake"`) — bypasses the Iceberg REST layer
and talks to DuckLake's Postgres metadata + S3 data path directly:

    [profiles.lake_direct]
    type         = "ducklake"
    postgres_dsn = "dbname=ducklake host=/tmp/.pgsock port=55432 user=ducklake"
    data_path    = "s3://lakehouse/data/"
    catalog      = "lake"            # the `AS <name>` in ATTACH

    [profiles.lake_direct.s3]
    endpoint   = "http://127.0.0.1:9000"
    access_key = "minioadmin"
    secret_key = "minioadmin"

**ADBC source** (`type = "adbc"`) — attaches any database with an ADBC
driver (Postgres, MySQL, Snowflake, SQL Server, Trino, SQLite, …) via
the `adbc_scanner` DuckDB extension. Drivers are installed out-of-band
with the `dbc` CLI (`dbc install postgresql`):

    [profiles.pg]
    type    = "adbc"
    driver  = "postgresql"
    # The postgresql driver takes credentials in the URI; a password in
    # the config is fine, or use `uri_env` to pull the DSN from an env var.
    uri     = "postgresql://reporting:s3cret@db.example.com:5432/appdb"
    catalog = "pg"                   # the `AS <name>` in ATTACH

    [profiles.pg.options]            # open-ended driver options, for
    username = "reporting"           # drivers that accept option-based
    password = "hunter2"             # auth (or password_env = "...")

Any profile can carry an `[profiles.X.oauth]` block selecting a grant:
`client_credentials` (default), `device_code`, or `authorization_code`
(PKCE). Interactive grants cache tokens under `$XDG_STATE_HOME/lakesh/`
— run `lakesh auth login -p <name>` to sign in.

Env-var indirection (`*_env` keys) keeps secrets out of the file. The
`--uri` and `--warehouse` CLI overrides apply to Iceberg REST profiles
only; DuckLake profiles use `postgres_dsn`, `data_path`, and `catalog`.
"""
from __future__ import annotations

import fnmatch
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(Exception):
    """Raised for missing / malformed config files and profile lookup failures."""


@dataclass
class S3Config:
    endpoint: str | None = None
    region: str = "us-east-1"
    access_key: str | None = None
    secret_key: str | None = None
    session_token: str | None = None
    path_style: bool = True
    """For MinIO and most on-prem S3. Flip off for AWS S3 proper."""


OAUTH_GRANTS = ("client_credentials", "device_code", "authorization_code")


@dataclass
class SigningConfig:
    """How this profile proves to the source that lakesh is asking.

    Absent means the session stamp stays client-asserted, which is the
    default and is fine for attribution. Present means lakesh signs a
    short-lived token a masking policy can verify. See `attest`.
    """

    method: str = "hmac"
    """`hmac` (default) or `ecdsa`.

    Measured through a real masking policy over 1M rows: `hmac` verifies
    in pure SQL at 0.41s against a 0.20s no-policy floor, while `ecdsa`
    needs a Python UDF at 2.75s. `hmac` is the default because 2.5s on
    every query is a tax an agentic tool pays constantly.

    The trade is that `hmac` is symmetric — whoever can read the secret
    can forge a proof — so the generator keeps it in an RBAC-protected
    table rather than in any DDL. Choose `ecdsa` when the requirement is
    that the source hold nothing forgeable at all."""
    kid: str = ""
    """Key id. The Snowflake-side verifier maps it to a trust label, so
    this — not any claim the client makes — is what decides what the
    session is allowed to see."""
    key_file: str = ""
    key_env: str = ""
    key_keychain: str = ""
    """Private key sources, checked in that order. Explicit beats ambient:
    a path someone wrote in the config outranks an environment variable
    that may have been inherited from anywhere."""
    ttl_s: int = 0
    """Token lifetime for `ecdsa`; 0 means `attest.DEFAULT_TTL_S`. Short
    on purpose — the token is written to the source's query history.

    Ignored by `hmac`, whose proof carries no timestamp: the session is
    the expiry boundary there, and a clock dependency between client and
    source would mask everything on skew. See `attest.mint_proof`."""

    @property
    def enabled(self) -> bool:
        return bool(self.key_file or self.key_env or self.key_keychain)


@dataclass
class OAuthConfig:
    grant: str = "client_credentials"
    client_id: str | None = None
    client_secret: str | None = None
    """Optional for public clients using device_code / authorization_code."""
    token_endpoint: str | None = None
    """For iceberg-rest client_credentials, defaults to the catalog's own
    `{uri}/v1/oauth/tokens` when unset."""
    device_authorization_endpoint: str | None = None
    authorization_endpoint: str | None = None
    scope: str | None = None
    audience: str | None = None
    redirect_port: int | None = None
    """Loopback port for authorization_code — set when the IdP requires an
    exact pre-registered redirect URI; otherwise an ephemeral port is used."""
    extra: dict[str, str] = field(default_factory=dict)
    """Extra form params passed through on token requests (e.g. `resource`)."""

    @property
    def enabled(self) -> bool:
        if self.grant == "client_credentials":
            return bool(self.client_id and self.client_secret)
        return bool(self.client_id)


SUPPORTED_TYPES = ("iceberg-rest", "ducklake", "adbc", "python")

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# --------------------------------------------------------------------------
# table annotations: what an operator asserts about a table
#
# The observed half of freshness (a last-modified timestamp) is only
# available on some sources — see `lakesh.freshness`. The *declared*
# half works everywhere, which is why it lives in config: an agent can
# be told "this table is the canonical one and should be under six hours
# old" regardless of whether the source can confirm the second part.

TABLE_STATUSES = ("canonical", "deprecated", "unknown")

# `describe_table` output shapes. `object` carries the columns in an
# envelope alongside the table's status and freshness; `array` is the
# original bare list of columns, kept because it is the shape anything
# written before those existed will be parsing.
DESCRIBE_TABLE_SHAPES = ("object", "array")

# Masking modes. `audit` reports what would be masked without masking it,
# which is the only way to tune a rule set without discovering in
# production that it eats your order numbers.
MASK_MODES = ("off", "mask", "audit")

_MASKING_KEYS = frozenset({"mode", "rules", "custom"})

_ANNOTATION_KEYS = frozenset({"status", "max_staleness", "note", "superseded_by"})

_DURATION_RE = re.compile(r"(\d+)\s*([smhdw])", re.IGNORECASE)
_DURATION_FULL_RE = re.compile(r"^(?:\s*\d+\s*[smhdw]\s*)+$", re.IGNORECASE)
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text: str, context: str = "") -> int:
    """Seconds from a compact duration: "45m", "24h", "7d", "1h30m".

    A bare number is rejected rather than guessed at. "24" is 24 hours to
    whoever is writing a freshness threshold and 24 seconds to
    `time.sleep`, and silently picking one makes either every table stale
    or every table fresh — both of which look like the feature working.
    """
    raw = str(text).strip()
    if not _DURATION_FULL_RE.match(raw):
        where = f"{context}: " if context else ""
        raise ConfigError(
            f'{where}cannot parse duration {text!r} — use a unit, '
            f'e.g. "45m", "24h", "7d" (bare numbers are ambiguous)'
        )
    return sum(
        int(n) * _DURATION_UNITS[unit.lower()]
        for n, unit in _DURATION_RE.findall(raw)
    )


@dataclass
class TableAnnotation:
    """What the operator says about one table, or a glob of them."""
    pattern: str
    status: str = "unknown"
    max_staleness: str = ""
    """As written, kept for error messages and for echoing back."""
    max_staleness_seconds: int | None = None
    note: str = ""
    superseded_by: str = ""

    @property
    def is_empty(self) -> bool:
        return not (
            self.status != "unknown" or self.max_staleness_seconds
            or self.note or self.superseded_by
        )


@dataclass
class Profile:
    name: str
    type: str = "iceberg-rest"
    # iceberg-rest fields (`uri` doubles as the ADBC ATTACH string)
    uri: str = ""
    warehouse: str = ""
    # ducklake fields
    postgres_dsn: str = ""
    data_path: str = ""
    catalog: str = "lake"          # ATTACH alias (default "src" for adbc)
    # adbc fields
    driver: str = ""               # ADBC driver name, resolved via manifest
    options: dict[str, str] = field(default_factory=dict)
    """Open-ended ADBC driver options (username, password, dotted driver
    keys, …). Values support `*_env` indirection at parse time and a
    `{token}` placeholder replaced with the OAuth bearer token."""
    token_option: str = ""
    """Which ADBC option key receives the OAuth bearer token."""
    backend: str = ""
    """For `type = "python"`: which Python query backend serves this
    profile. A shipped name (`duckdb`, `snowflake`, `postgres`, `dbapi`)
    or a `"module:callable"` import path to a user factory. `options` are
    passed to the driver's `connect()`. See `lakesh.backend`."""
    read_only: bool = False
    query_timeout_s: float | None = None
    """Per-query deadline. When set this is a *ceiling*: a caller may ask
    for less but never more, the same way `read_only` cannot be widened
    by `LAKESH_MCP_WRITE`. Unset means the MCP server's own default
    applies."""
    status: str = "unknown"
    """Profile-wide default for `TableAnnotation.status`."""
    max_staleness: str = ""
    max_staleness_seconds: int | None = None
    """Profile-wide default freshness threshold, used for any table
    without one of its own."""
    tables: dict[str, TableAnnotation] = field(default_factory=dict)
    """Per-table annotations, keyed by `SCHEMA.TABLE` (globs allowed)."""
    masking_mode: str | None = None
    masking_rules: tuple[str, ...] | None = None
    masking_custom: tuple = ()
    """Per-profile masking override; None means "use the global setting"."""
    dialect: str = ""
    """Override the engine guess. The guess reads the driver's basename,
    which is right almost always and wrong for an unusual layout — this
    is the escape hatch."""
    session_context: bool = True
    """Whether to label sessions with who is driving lakesh.

    On by default, unlike `upload_roots`, because the two defaults answer
    opposite questions: an allow-list governs what lakesh may *do*, and
    this only adds a label to a session lakesh was going to open anyway.
    Off is for an engine that rejects the statement or an operator whose
    own tooling owns `QUERY_TAG`."""
    query_tag: str = ""
    """Override the session label. Empty means `lakesh/<version> <caller>`."""
    signing: "SigningConfig | None" = None
    """Signed attestation. None means the stamp stays client-asserted."""
    session_variables: dict[str, str] = field(default_factory=dict)
    """Extra session variables to set alongside `client`, e.g. a team or
    cost-centre. Names must be plain identifiers; each engine applies its
    own namespace prefix. These are client-asserted like everything else
    here — see `dialect.SessionContext`."""
    upload_roots: tuple[str, ...] = ()
    """Directories this profile may stage files from. Empty means uploads
    are refused — an unconfigured allow-list means the feature is off, not
    that everything is permitted."""
    file_format: str = ""
    """Inline file format for loads, e.g. "TYPE=CSV SKIP_HEADER=1"."""
    infer_file_format: str = ""
    """A NAMED file format object, required for `--create` because
    INFER_SCHEMA does not accept an inline spec."""
    max_upload_bytes: int = 0
    """0 uses the default in `staging`."""
    read_procedures: tuple[str, ...] = ()
    """Procedures the operator vouches for as reads, so `CALL` on them is
    allowed in a read-only session. A declaration, not a verification:
    lakesh cannot check what a procedure does."""
    # shared
    s3: S3Config = field(default_factory=S3Config)
    oauth: OAuthConfig = field(default_factory=OAuthConfig)

    def annotation_for(self, namespace: str, table: str) -> TableAnnotation | None:
        """The most specific annotation matching `SCHEMA.TABLE`.

        Specificity, not file order, decides: an exact key beats a glob,
        and a longer glob beats a shorter one, so `ANALYTICS.DIM_*` wins
        over `ANALYTICS.*` no matter which was written first.

        Matching is case-insensitive because Snowflake upper-cases
        unquoted identifiers and Postgres lower-cases them — the same
        logical table is spelled two ways depending on which source you
        ask, and an operator should not have to know which.
        """
        if not self.tables:
            return None
        key = f"{namespace}.{table}".casefold()
        best: TableAnnotation | None = None
        best_len = -1
        for pattern, ann in self.tables.items():
            folded = pattern.casefold()
            if folded == key:
                return ann
            # fnmatchcase on pre-folded strings, never fnmatch: fnmatch
            # applies os.path.normcase, which would make the semantics
            # of a config file depend on the platform reading it.
            if fnmatch.fnmatchcase(key, folded) and len(folded) > best_len:
                best, best_len = ann, len(folded)
        return best

    def validate(self) -> None:
        if self.type not in SUPPORTED_TYPES:
            raise ConfigError(
                f"profile {self.name!r}: unknown type {self.type!r} "
                f"(supported: {', '.join(SUPPORTED_TYPES)})"
            )
        if self.type == "iceberg-rest":
            if not self.uri:
                raise ConfigError(f"profile {self.name!r}: missing `uri`")
            if not self.warehouse:
                raise ConfigError(f"profile {self.name!r}: missing `warehouse`")
        elif self.type == "ducklake":
            if not self.postgres_dsn:
                raise ConfigError(
                    f"profile {self.name!r}: ducklake profile requires "
                    f"`postgres_dsn` (e.g. "
                    f'"dbname=ducklake host=/tmp/.pgsock port=55432 user=ducklake")'
                )
            if not self.data_path:
                raise ConfigError(
                    f"profile {self.name!r}: ducklake profile requires "
                    f"`data_path` (e.g. \"s3://bucket/prefix/\")"
                )
        elif self.type == "adbc":
            if not self.driver:
                raise ConfigError(
                    f"profile {self.name!r}: adbc profile requires `driver` "
                    f"(an ADBC driver name, e.g. \"postgresql\" — install "
                    f"drivers with `dbc install <name>`)"
                )
            if not _IDENTIFIER_RE.match(self.catalog):
                raise ConfigError(
                    f"profile {self.name!r}: `catalog` {self.catalog!r} must "
                    f"be a plain identifier ([A-Za-z_][A-Za-z0-9_]*)"
                )
            if self.oauth.enabled and not self.token_option and not any(
                "{token}" in v for v in self.options.values()
            ):
                raise ConfigError(
                    f"profile {self.name!r}: adbc profile with oauth needs "
                    f"`token_option` (the ADBC option that receives the "
                    f"bearer token) or a \"{{token}}\" placeholder in an "
                    f"option value"
                )
            self._check_snowflake_oauth()
        elif self.type == "python":
            if not self.backend:
                raise ConfigError(
                    f"profile {self.name!r}: python profile requires "
                    f"`backend` — a shipped name (duckdb, snowflake, "
                    f"postgres, dbapi) or a \"module:callable\" import path"
                )
            if not self.dialect:
                raise ConfigError(
                    f"profile {self.name!r}: python profile requires "
                    f"`dialect` — there is no driver path to guess "
                    f"capabilities from (e.g. dialect = \"snowflake\")"
                )
        # Type-agnostic: annotations apply to every profile type.
        if self.dialect:
            from .dialect import known_dialects
            if self.dialect.lower() not in known_dialects():
                raise ConfigError(
                    f"profile {self.name!r}: unknown dialect {self.dialect!r} "
                    f"(supported: {', '.join(known_dialects())})"
                )
        if self.session_variables:
            from .dialect import BARE_NAME_RE
            for key in self.session_variables:
                if not BARE_NAME_RE.match(key):
                    raise ConfigError(
                        f"profile {self.name!r}: session variable {key!r} is "
                        f"not a plain identifier. Use letters, digits and "
                        f"underscores — lakesh adds each engine's own "
                        f"namespace prefix, so a qualified name would "
                        f"collide with it."
                    )
                if key.lower() == "client":
                    raise ConfigError(
                        f"profile {self.name!r}: `client` is set by lakesh to "
                        f"name the caller (mcp or cli) and cannot be "
                        f"overridden — an overridable caller label would be "
                        f"worthless to a policy. Use a different name."
                    )
        if self.signing is not None:
            s = self.signing
            if not s.enabled:
                raise ConfigError(
                    f"profile {self.name!r}: `[signing]` needs one of "
                    f"`key_file`, `key_env` or `key_keychain`. An empty block "
                    f"would leave signing off while looking configured."
                )
            if not s.kid:
                raise ConfigError(
                    f"profile {self.name!r}: `[signing]` needs a `kid`. The "
                    f"verifier maps it to a trust label, so a token without "
                    f"one can never be recognised."
                )
            if s.ttl_s < 0:
                raise ConfigError(
                    f"profile {self.name!r}: `signing.ttl_s` cannot be negative"
                )
            from .attest import METHODS
            if s.method not in METHODS:
                raise ConfigError(
                    f"profile {self.name!r}: unknown signing method "
                    f"{s.method!r} (supported: {', '.join(METHODS)})"
                )
        if self.max_upload_bytes < 0:
            raise ConfigError(
                f"profile {self.name!r}: `max_upload_bytes` cannot be "
                f"negative (got {self.max_upload_bytes})"
            )
        if self.status not in TABLE_STATUSES:
            raise ConfigError(
                f"profile {self.name!r}: unknown status {self.status!r} "
                f"(supported: {', '.join(TABLE_STATUSES)})"
            )
        for ann in self.tables.values():
            if ann.status not in TABLE_STATUSES:
                raise ConfigError(
                    f"profile {self.name!r}: table {ann.pattern!r}: unknown "
                    f"status {ann.status!r} "
                    f"(supported: {', '.join(TABLE_STATUSES)})"
                )
        if self.oauth.enabled and self.type != "ducklake":
            self._validate_oauth()

    def _check_snowflake_oauth(self) -> None:
        """Snowflake needs `auth_type` set, or it ignores the token.

        Measured, and the reason this is a hard error rather than a note
        in the docs: with `client_option.auth_token` supplied but
        `auth_type` left unset, the driver **silently discards the token**
        and authenticates with whatever else the DSN carries. A deliberate
        garbage token connected fine that way.

        There is no error to debug from — the session simply is not the
        one you configured. That matters most for the case OAuth is
        usually being set up for here: an `IS_AGENTIC = TRUE` security
        integration, where the symptom is agent policies quietly never
        firing.
        """
        if not (self.oauth.enabled and self.token_option):
            return
        if "snowflake" not in os.path.basename(self.driver or "").lower():
            return
        key = "adbc.snowflake.sql.auth_type"
        if self.options.get(key, "").lower() == "auth_oauth":
            return
        raise ConfigError(
            f"profile {self.name!r}: a Snowflake profile using oauth must set "
            f'`options."{key}" = "auth_oauth"`. Without it the driver ignores '
            f"the bearer token and authenticates as whatever else the DSN "
            f"carries — it connects, so there is no error to notice, and the "
            f"session is not the one you configured."
        )

    def _validate_oauth(self) -> None:
        o = self.oauth
        if o.grant not in OAUTH_GRANTS:
            raise ConfigError(
                f"profile {self.name!r}: unknown oauth grant {o.grant!r} "
                f"(supported: {', '.join(OAUTH_GRANTS)})"
            )
        # iceberg-rest client_credentials defaults to the catalog's own
        # /v1/oauth/tokens endpoint, so token_endpoint may be omitted there.
        needs_token_endpoint = not (
            o.grant == "client_credentials" and self.type == "iceberg-rest"
        )
        if needs_token_endpoint and not o.token_endpoint:
            raise ConfigError(
                f"profile {self.name!r}: oauth grant {o.grant!r} requires "
                f"`token_endpoint`"
            )
        if o.grant == "device_code" and not o.device_authorization_endpoint:
            raise ConfigError(
                f"profile {self.name!r}: device_code grant requires "
                f"`device_authorization_endpoint`"
            )
        if o.grant == "authorization_code" and not o.authorization_endpoint:
            raise ConfigError(
                f"profile {self.name!r}: authorization_code grant requires "
                f"`authorization_endpoint`"
            )


@dataclass
class Config:
    profiles: dict[str, Profile]
    default: str | None = None
    source_path: Path | None = None
    masking_mode: str = "off"
    masking_rules: tuple[str, ...] | None = None
    masking_custom: tuple = ()
    """Global masking default. See `lakesh.mask` for what masking does and,
    more importantly, what it does not."""
    describe_table_shape: str = "object"
    """Output shape for the MCP `describe_table` tool.

    `object` wraps the columns in an envelope that can also carry the
    table's status and freshness. `array` is the older bare list of
    columns — no envelope, and therefore nowhere to report that a table
    is deprecated. Set it when a client parses the array shape and you
    would rather not change the client."""

    def get(self, name: str | None) -> Profile:
        name = name or self.default
        if not name:
            raise ConfigError(
                "no profile specified and no `default` set in config"
            )
        if name not in self.profiles:
            available = ", ".join(sorted(self.profiles)) or "<none>"
            raise ConfigError(
                f"profile {name!r} not found (available: {available})"
            )
        return self.profiles[name]


# --------------------------------------------------------------------------
# path discovery

def default_config_path() -> Path:
    """Where we look for config if the caller doesn't pass one.

    Precedence:
      1. `$LAKESH_CONFIG` (full path — explicit override)
      2. `$XDG_CONFIG_HOME/lakesh/config.toml`
      3. `~/.config/lakesh/config.toml`
    """
    env = os.environ.get("LAKESH_CONFIG")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "lakesh" / "config.toml"


# --------------------------------------------------------------------------
# load + parse

def _parse_signing(name: str, raw: Any) -> "SigningConfig | None":
    """The `[signing]` block, or None when the profile has none."""
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise ConfigError(f"profile {name!r}: `signing` must be a table")
    unknown = set(raw) - {"method", "kid", "key_file", "key_env",
                          "key_keychain", "ttl_s"}
    if unknown:
        # Rejected rather than ignored: a typo'd `key_path` would leave
        # signing silently off, which looks exactly like it working.
        raise ConfigError(
            f"profile {name!r}: unknown key(s) in `[signing]`: "
            f"{', '.join(sorted(unknown))}"
        )
    return SigningConfig(
        method=str(raw.get("method", "hmac")).lower(),
        kid=str(raw.get("kid", "")),
        key_file=str(raw.get("key_file", "")),
        key_env=str(raw.get("key_env", "")),
        key_keychain=str(raw.get("key_keychain", "")),
        ttl_s=int(raw.get("ttl_s", 0) or 0),
    )


def _resolve_env(value: Any, env_key: Any) -> Any:
    """Given a pair of (literal, literal_from_env), return whichever is set.
    Used for `client_id` / `client_id_env` pairs — literal wins if both
    are present (explicit-over-implicit)."""
    if value not in (None, ""):
        return value
    if env_key:
        return os.environ.get(str(env_key))
    return None


def _parse_options(raw: dict) -> dict[str, str]:
    """Parse an open-ended options table with `*_env` indirection on any
    key: `password_env = "PGPASS"` resolves `$PGPASS` into `password`.
    A literal key wins over its `_env` twin (same `_resolve_env` rule)."""
    out: dict[str, str] = {}
    for k, v in raw.items():
        if k.endswith("_env"):
            continue
        out[k] = str(v)
    for k, v in raw.items():
        if not k.endswith("_env"):
            continue
        target = k[: -len("_env")]
        if target in out:
            continue  # literal wins
        resolved = os.environ.get(str(v))
        if resolved is not None:
            out[target] = resolved
    return out


def _parse_timeout(name: str, value: Any) -> float | None:
    """`query_timeout_s`, as a positive number of seconds or unset.

    Coerced here rather than in `validate()` so a non-numeric value
    raises a `ConfigError` naming the profile instead of a bare
    `ValueError` from the dataclass construction — the same reason
    `redirect_port` guards for None before `int()`.
    """
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise ConfigError(
            f"profile {name!r}: `query_timeout_s` must be a number of "
            f"seconds, got {value!r}"
        ) from None
    if seconds <= 0:
        raise ConfigError(
            f"profile {name!r}: `query_timeout_s` must be greater than zero "
            f"(got {seconds!r}); omit the key to accept the default"
        )
    return seconds


def parse_masking(
    raw: Any, where: str
) -> tuple[str, tuple[str, ...] | None, tuple]:
    """(mode, rule labels or None) from a `[masking]` table.

    Unknown keys are rejected for the same reason table annotations reject
    them: for a governance feature a silently-ignored typo is the worst
    possible failure, because the operator believes the protection is on.
    A `rules` entry naming a rule that does not exist is rejected too — a
    rule you thought you enabled and didn't is the same failure.
    """
    from .mask import CustomRuleError, build_custom_rules, rule_index

    if raw is None:
        return "off", None, ()
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: `masking` must be a table")
    try:
        custom = build_custom_rules(raw.get("custom") or {})
    except CustomRuleError as e:
        raise ConfigError(f"{where}: {e}") from None
    unknown = sorted(set(raw) - _MASKING_KEYS)
    if unknown:
        raise ConfigError(
            f"{where}: unknown masking key{'s' if len(unknown) > 1 else ''} "
            f"{', '.join(repr(u) for u in unknown)} "
            f"(supported: {', '.join(sorted(_MASKING_KEYS))})"
        )
    mode = str(raw.get("mode", "off"))
    if mode not in MASK_MODES:
        raise ConfigError(
            f"{where}: unknown masking mode {mode!r} "
            f"(supported: {', '.join(MASK_MODES)})"
        )
    labels = raw.get("rules")
    if labels is None:
        return mode, None, custom
    if not isinstance(labels, list):
        raise ConfigError(f"{where}: `masking.rules` must be a list of labels")
    known = dict(rule_index())
    known.update({r.label: r for r in custom})
    out = []
    for label in labels:
        name = str(label)
        if name not in known:
            raise ConfigError(
                f"{where}: unknown masking rule {name!r} "
                f"(available: {', '.join(sorted(known))})"
            )
        out.append(name)
    return mode, tuple(out), custom


def _parse_table_annotation(profile: str, key: str, raw: Any) -> TableAnnotation:
    """One `[profiles.X.tables]` entry.

    The key must be quoted in TOML — an unquoted `ANALYTICS.FCT_ORDERS`
    is dotted-key syntax and silently nests into
    `{"ANALYTICS": {"FCT_ORDERS": …}}` instead of becoming a literal
    key, so the resulting annotation would never match anything.
    """
    if not isinstance(raw, dict):
        raise ConfigError(
            f"profile {profile!r}: [profiles.{profile}.tables] entry {key!r} "
            f'must be a table, e.g. {{ status = "canonical" }}'
        )
    if "." not in key:
        raise ConfigError(
            f"profile {profile!r}: table key {key!r} must be SCHEMA.TABLE "
            f'(quote it — use "*.{key}" to match that table in any schema)'
        )
    # `_parse_profile` ignores unknown keys, which for a governance
    # feature is the worst possible failure: `max_stalenes = "24h"` would
    # parse clean and the annotation would silently never apply, while
    # the operator believes the table is marked. Guard the annotation's
    # own keys — narrowly, without imposing a profile-wide key-set diff
    # that would break existing configs.
    unknown = sorted(set(raw) - _ANNOTATION_KEYS)
    if unknown:
        raise ConfigError(
            f"profile {profile!r}: table {key!r}: unknown key"
            f"{'s' if len(unknown) > 1 else ''} {', '.join(repr(u) for u in unknown)} "
            f"(supported: {', '.join(sorted(_ANNOTATION_KEYS))})"
        )
    staleness = str(raw.get("max_staleness", ""))
    return TableAnnotation(
        pattern=key,
        status=str(raw.get("status", "unknown")),
        max_staleness=staleness,
        max_staleness_seconds=(
            parse_duration(staleness, f"profile {profile!r} table {key!r}")
            if staleness else None
        ),
        note=str(raw.get("note", "")),
        superseded_by=str(raw.get("superseded_by", "")),
    )


def _parse_profile(name: str, raw: dict) -> Profile:
    s3_raw = raw.get("s3") or {}
    s3 = S3Config(
        endpoint=s3_raw.get("endpoint"),
        region=s3_raw.get("region", "us-east-1"),
        access_key=_resolve_env(s3_raw.get("access_key"), s3_raw.get("access_key_env")),
        secret_key=_resolve_env(s3_raw.get("secret_key"), s3_raw.get("secret_key_env")),
        session_token=_resolve_env(
            s3_raw.get("session_token"), s3_raw.get("session_token_env")
        ),
        path_style=bool(s3_raw.get("path_style", True)),
    )
    oauth_raw = raw.get("oauth") or {}
    extra_raw = oauth_raw.get("extra") or {}
    if not isinstance(extra_raw, dict):
        raise ConfigError(f"profile {name!r}: `oauth.extra` must be a table")
    redirect_port = oauth_raw.get("redirect_port")
    oauth = OAuthConfig(
        grant=str(oauth_raw.get("grant", "client_credentials")),
        client_id=_resolve_env(oauth_raw.get("client_id"), oauth_raw.get("client_id_env")),
        client_secret=_resolve_env(
            oauth_raw.get("client_secret"), oauth_raw.get("client_secret_env")
        ),
        token_endpoint=oauth_raw.get("token_endpoint"),
        device_authorization_endpoint=oauth_raw.get("device_authorization_endpoint"),
        authorization_endpoint=oauth_raw.get("authorization_endpoint"),
        scope=oauth_raw.get("scope"),
        audience=oauth_raw.get("audience"),
        redirect_port=int(redirect_port) if redirect_port is not None else None,
        extra={str(k): str(v) for k, v in extra_raw.items()},
    )
    # DuckLake profiles can stash the Postgres password in an env var
    # rather than baking it into the DSN string committed to config.
    postgres_dsn = _resolve_env(raw.get("postgres_dsn"), raw.get("postgres_dsn_env")) or ""
    ptype = str(raw.get("type", "iceberg-rest"))
    options_raw = raw.get("options") or {}
    if not isinstance(options_raw, dict):
        raise ConfigError(f"profile {name!r}: `options` must be a table")
    # `uri_env` matters for ADBC drivers (e.g. postgresql) that only take
    # credentials embedded in the connection URI — keeps the password out
    # of the config file, like `postgres_dsn_env` for ducklake.
    uri = _resolve_env(raw.get("uri"), raw.get("uri_env")) or ""
    tables_raw = raw.get("tables") or {}
    if not isinstance(tables_raw, dict):
        raise ConfigError(f"profile {name!r}: `tables` must be a table")
    tables = {
        str(key): _parse_table_annotation(name, str(key), value)
        for key, value in tables_raw.items()
    }
    profile_staleness = str(raw.get("max_staleness", ""))
    if "masking" in raw:
        prof_mask_mode, prof_mask_rules, prof_mask_custom = parse_masking(
            raw.get("masking"), f"profile {name!r}")
    else:
        prof_mask_mode, prof_mask_rules, prof_mask_custom = None, None, ()
    p = Profile(
        name=name,
        type=ptype,
        uri=str(uri),
        warehouse=str(raw.get("warehouse", "")),
        postgres_dsn=str(postgres_dsn or ""),
        data_path=str(raw.get("data_path", "")),
        catalog=str(raw.get("catalog", "src" if ptype == "adbc" else "lake")),
        driver=str(raw.get("driver", "")),
        options=_parse_options(options_raw),
        token_option=str(raw.get("token_option", "")),
        backend=str(raw.get("backend", "")),
        read_only=bool(raw.get("read_only", False)),
        query_timeout_s=_parse_timeout(name, raw.get("query_timeout_s")),
        status=str(raw.get("status", "unknown")),
        max_staleness=profile_staleness,
        max_staleness_seconds=(
            parse_duration(profile_staleness, f"profile {name!r}")
            if profile_staleness else None
        ),
        tables=tables,
        masking_mode=prof_mask_mode,
        masking_rules=prof_mask_rules,
        masking_custom=prof_mask_custom,
        dialect=str(raw.get("dialect", "")),
        read_procedures=tuple(
            str(n) for n in (raw.get("read_procedures") or [])),
        session_context=bool(raw.get("session_context", True)),
        query_tag=str(raw.get("query_tag", "")),
        signing=_parse_signing(name, raw.get("signing")),
        session_variables={
            str(k): str(v) for k, v in (raw.get("session_variables") or {}).items()},
        upload_roots=tuple(str(n) for n in (raw.get("upload_roots") or [])),
        max_upload_bytes=int(raw.get("max_upload_bytes", 0) or 0),
        file_format=str(raw.get("file_format", "")),
        infer_file_format=str(raw.get("infer_file_format", "")),
        s3=s3,
        oauth=oauth,
    )
    p.validate()
    return p


def load_config(path: Path | None = None) -> Config:
    """Load and parse the config file. Raises `ConfigError` if it doesn't exist
    or can't be parsed."""
    path = Path(path) if path else default_config_path()
    if not path.exists():
        raise ConfigError(
            f"config file not found: {path}\n"
            f"create one with `lakesh config init` or set LAKESH_CONFIG."
        )
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"failed to parse {path}: {e}") from e

    raw_profiles = data.get("profiles") or {}
    if not isinstance(raw_profiles, dict):
        raise ConfigError(f"{path}: `profiles` must be a table")
    profiles = {n: _parse_profile(n, raw) for n, raw in raw_profiles.items()}

    default = data.get("default")
    if default and default not in profiles:
        raise ConfigError(
            f"{path}: `default = {default!r}` but no such profile"
        )
    shape = str(data.get("describe_table_shape", "object"))
    if shape not in DESCRIBE_TABLE_SHAPES:
        raise ConfigError(
            f"{path}: unknown describe_table_shape {shape!r} "
            f"(supported: {', '.join(DESCRIBE_TABLE_SHAPES)})"
        )

    mask_mode, mask_rules, mask_custom = parse_masking(
        data.get("masking"), str(path))

    return Config(
        profiles=profiles, default=default, source_path=path,
        describe_table_shape=shape,
        masking_mode=mask_mode, masking_rules=mask_rules,
        masking_custom=mask_custom,
    )


def write_example_config(path: Path) -> None:
    """Drop a working example config file. Used by `lakesh config init`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_EXAMPLE)


_EXAMPLE = """\
# lakesh config — profiles for Iceberg REST catalogs and/or DuckLake
# metastores. Switch between them via `lakesh -p <name>` or by setting
# `default` below.

default = "local"

# Masking: hide recognisable PII in results. "audit" reports what would
# be masked without masking it — use it to tune the rule set before
# turning masking on. Applies at RENDER time and is not access control:
# substr(), LIKE filters, hashing and ORDER BY all defeat it.
# [masking]
# mode  = "off"                      # off | mask | audit
# rules = ["pii.email", "pii.phone"] # override the default-on set

# Override the engine guess, which normally reads the driver's basename.
# One of: duckdb | postgres | snowflake | ansi
# dialect = "postgres"

# Procedures you vouch for as reads, so CALL on them is allowed in a
# read-only session. A declaration, not a verification: lakesh cannot
# check what a procedure does. DuckLake's read procedures ship as known.
# read_procedures = ["my_reporting_proc"]

# Staging: directories this profile may upload files from. Uploads are
# refused unless this is set — an unconfigured allow-list means the
# feature is off, not that everything is permitted. Symlinks are resolved
# before the check. Note this is NOT the filesystem sandbox: a stage
# upload is read by the source's driver, outside DuckDB's reach.
# upload_roots     = ["~/data/exports"]
# max_upload_bytes = 104857600        # default 100 MB

# File format for `stage load` (COPY INTO). Inline spec.
# file_format = "TYPE=CSV SKIP_HEADER=1"
# A NAMED file format object, required only for `stage load --create`:
# Snowflake's INFER_SCHEMA does not accept an inline format.
# infer_file_format = "MYDB.FMTS.CSV_INFER"




# Your own masking patterns. Needs: pip install 'lakesh[mask]'
# Compiled with RE2, which cannot backtrack — so an untrusted pattern
# can't hang the server. RE2 has no lookaround or backreferences; a
# pattern using them is refused rather than downgraded to `re`.
# `requires` is a literal the pattern cannot match without, which lets
# the scanner skip cells that cannot possibly match.
# [masking.custom."pii.employee_id"]
# value    = 'EMP-[0-9]{6}'
# requires = "EMP-"



# Output shape for the MCP `describe_table` tool: "object" (default)
# wraps the columns alongside the table's status and freshness; "array"
# is the older bare list of columns, for clients that already parse it.
# A bare array has nowhere to report that a table is deprecated.
# describe_table_shape = "array"


# --- Iceberg REST profile (default) --------------------------------------

[profiles.local]
# Local duckicelake (or any Iceberg REST catalog) running on the laptop.
uri       = "http://127.0.0.1:8181"
warehouse = "lake"

[profiles.local.s3]
endpoint   = "http://127.0.0.1:9000"
region     = "us-east-1"
access_key = "minioadmin"
secret_key = "minioadmin"
path_style = true

# Uncomment if your local catalog has DUCKICELAKE_OAUTH_CLIENTS configured.
# [profiles.local.oauth]
# client_id     = "demo-client"
# client_secret = "demo-secret"

# --- DuckLake direct profile (bypass the Iceberg REST layer) -------------
# Useful for INSERT/UPDATE/DELETE workloads that the iceberg-ext can't do.
# Same underlying data as `local`, just via the ducklake extension.

# [profiles.lake_direct]
# type         = "ducklake"
# postgres_dsn = "dbname=ducklake host=/tmp/.pgsock port=55432 user=ducklake"
# data_path    = "s3://lakehouse/data/"
# catalog      = "lake"
#
# [profiles.lake_direct.s3]
# endpoint   = "http://127.0.0.1:9000"
# access_key = "minioadmin"
# secret_key = "minioadmin"

# --- Production example with env-backed secrets --------------------------
# [profiles.prod]
# uri       = "https://catalog.prod.example.com"
# warehouse = "prod"
#
# [profiles.prod.s3]
# region = "us-west-2"
#
# [profiles.prod.oauth]
# # client_credentials against an external IdP. Omit `token_endpoint` to
# # use the catalog's own /v1/oauth/tokens endpoint instead.
# token_endpoint    = "https://idp.example.com/oauth2/token"
# scope             = "catalog:read catalog:write"
# client_id_env     = "LAKESH_PROD_CLIENT_ID"
# client_secret_env = "LAKESH_PROD_CLIENT_SECRET"

# --- ADBC profile: query any database with an ADBC driver -----------------
# Install drivers with the `dbc` CLI (https://dbc.columnar.tech):
#     dbc install postgresql
#
# The postgresql driver takes credentials in the URI (it rejects
# username/password options). A password in the config file is fine, or
# use `uri_env` to source the whole DSN from an env var instead.
# [profiles.pg]
# type    = "adbc"
# driver  = "postgresql"
# uri     = "postgresql://reporting:s3cret@db.example.com:5432/appdb"
# # uri_env = "LAKESH_PG_DSN"    # alternative; literal `uri` wins if both set
# catalog = "pg"            # tables appear as pg.<schema>.<table>
# read_only = true

# --- ADBC: Snowflake with a PAT ------------------------------------------
# Credentials go in the DSN, the account goes in [options]. The driver
# reads each from exactly one place and ignores the other; see
# examples/config.snowflake-adbc.toml for the why and the error codes.
# [profiles.snowflake]
# type      = "adbc"
# driver    = "/path/to/libadbc_driver_snowflake.so"  # pip install adbc-driver-snowflake
# uri_env   = "LAKESH_SNOWFLAKE_DSN"                  # "USER:PAT@MYORG-ACCOUNT"
# catalog   = "snow"
# read_only = true
#
# # A per-query deadline. This is a CEILING: a caller may ask for less
# # but never more, the same way read_only can't be widened.
# query_timeout_s = 60
#
# [profiles.snowflake.options]
# "adbc.snowflake.sql.account"   = "MYORG-ACCOUNT"    # required
# "adbc.snowflake.sql.warehouse" = "MY_WH"
# "adbc.snowflake.sql.db"        = "SNOWFLAKE"
#
# # Which tables an agent should trust. Keys MUST be quoted — unquoted,
# # ANALYTICS.FCT_REVENUE is TOML dotted-key syntax and nests silently
# # instead of becoming a literal key. Globs allowed; most specific wins.
# [profiles.snowflake.tables]
# "ANALYTICS.FCT_REVENUE"    = { status = "canonical", max_staleness = "6h" }
# "ANALYTICS.FCT_REVENUE_V1" = { status = "deprecated", superseded_by = "ANALYTICS.FCT_REVENUE" }
# "STAGING.*"                = { status = "deprecated", note = "raw landing zone" }

# --- ADBC + OAuth2 device-code login (e.g. Snowflake via an IdP) ----------
# No `username` option: the Snowflake driver parses the ATTACH path as a
# gosnowflake DSN and that parse overwrites user and password, so a
# `username` option is silently discarded.
# [profiles.snow]
# type         = "adbc"
# driver       = "snowflake"
# catalog      = "snow"
# token_option = "adbc.snowflake.sql.client_option.auth_token"
#
# [profiles.snow.options]
# "adbc.snowflake.sql.account" = "myorg-account1"
# # REQUIRED with oauth, and validated at load. Without it the driver
# # silently ignores the bearer token and authenticates as whatever else
# # the DSN carries — it connects, so there is no error to notice.
# "adbc.snowflake.sql.auth_type" = "auth_oauth"
#
# [profiles.snow.oauth]
# grant                         = "device_code"
# client_id                     = "lakesh-cli"
# device_authorization_endpoint = "https://idp.example.com/oauth2/v1/device/authorize"
# token_endpoint                = "https://idp.example.com/oauth2/v1/token"
# scope                         = "session:role:ANALYST offline_access"

# --- Session context: telling the source who is asking -------------------
# On by default. lakesh labels every session it opens with whether a
# human (cli) or an agent (mcp) is driving, so the engine's audit trail
# can tell them apart. On Snowflake that is QUERY_TAG plus a
# LAKESH_CLIENT session variable; on Postgres, application_name plus
# lakesh.client.
#
# This is ATTRIBUTION, NOT ACCESS CONTROL. The value is client-asserted:
# the same credentials that set LAKESH_CLIENT = 'mcp' can set it to
# anything. A masking policy may read it (verified — GETVARIABLE works in
# a policy body), but it is trusting the client to be honest.
#
# Session context reaches more than Snowflake. Postgres gets
# application_name + lakesh.client (visible in pg_stat_activity); a
# DuckLake metastore is labelled through its DSN; an Iceberg REST
# catalog such as duckicelake sees lakesh in the HTTP User-Agent. On
# DuckDB-hosted engines the variable is process-local -- nothing
# server-side reads it. ADBC profiles used WITHOUT --native have no
# handle to send the statement down and report that honestly.
#
# Snowflake's IS_AGENT_ACTIVATED is the trustworthy version, because it
# is derived from how the session authenticated and no client can set it.
# lakesh cannot turn it on; it can only report it. Check with:
#     lakesh profiles show <name> --probe
# To actually earn it, an ACCOUNTADMIN creates an agentic OAuth
# integration and the profile authenticates through it:
#     CREATE SECURITY INTEGRATION lakesh_agent
#       TYPE = OAUTH
#       OAUTH_CLIENT = CUSTOM
#       OAUTH_CLIENT_TYPE = 'CONFIDENTIAL'
#       OAUTH_REDIRECT_URI = 'http://localhost:8080/callback'
#       IS_AGENTIC = TRUE
#       ENABLED = TRUE;
#
# The keys, for any profile above (they belong inside that profile's own
# table — do not open a second [profiles.<name>] block for them, TOML
# refuses a redefined table):
#
#   session_context = false        # opt out entirely
#   query_tag       = "acme-etl"   # override the default label
#
#   [profiles.<name>.session_variables]
#   # Extra variables set alongside `client`, which lakesh owns and which
#   # cannot be overridden. Names must be plain identifiers; each engine
#   # applies its own namespace prefix (LAKESH_TEAM / lakesh.team).
#   team = "data-eng"
# --- Signed attestation: making the caller claim unforgeable --------------
# The session stamp above is client-asserted. Signing makes it verifiable:
# lakesh mints a short-lived token bound to the source session, and a UDF
# inside a Snowflake masking policy checks it. No valid signature, no
# unmasked data. Needs `pip install 'lakesh[sign]'`.
#
#   lakesh session keygen --kid agent-2026-08 -o ~/.config/lakesh/keys/agent.pem
#   lakesh session install-sql -p <name> --label human   # review, run as ACCOUNTADMIN
#
# The token is written to QUERY_HISTORY verbatim and kept a year, so it is
# bound to one CURRENT_SESSION() and expires in seconds — a token lifted
# from history is useless. Cost is ~2s per query (the Python UDF runtime,
# flat regardless of row count).
#
# The trust label comes from the KEY, not the token: generate a separate
# key per caller. This only separates callers as far as the keys are
# separated — an agent that can read the human key can sign as a human.
#
#   [profiles.<name>.signing]
#   method       = "hmac"                  # hmac (default) | ecdsa
#   kid          = "agent-2026-08"
#   key_file     = "~/.config/lakesh/keys/agent.key"
#   # key_env      = "LAKESH_SIGNING_KEY"    # alternative
#   # key_keychain = "lakesh-agent"          # macOS Keychain / libsecret
#
# `hmac` verifies in pure SQL and costs ~0.2s per query over the
# no-policy floor. `ecdsa` needs a Python UDF and costs ~2.5s, and is
# for environments that require the source to hold nothing forgeable --
# an HMAC secret can mint proofs, a public key cannot. The generator
# keeps the HMAC secret in an RBAC-protected table, never in DDL:
# verified, a role with only SELECT on the protected table is denied on
# the keys table and on GET_DDL of the policy, and a valid proof still
# unmasks.

# --- Python backend: a PEP 249 driver instead of ADBC ---------------------
# type = "python" serves a profile from a Python driver (python-duckdb,
# snowflake-connector-python, psycopg) or your own "module:callable". No
# ADBC .so; on Snowflake it can reach agent-activation the ADBC driver
# cannot. Extras: pip install 'lakesh[snowflake-python]' /
# 'lakesh[postgres-python]'. `dialect` is required (no .so to guess from).
# [profiles.snow_py]
# type    = "python"
# backend = "snowflake"          # duckdb | snowflake | postgres | pyiceberg | module:callable
# dialect = "snowflake"
#
# [profiles.snow_py.options]     # passed to the driver's connect()
# account   = "myorg-account1"
# warehouse = "MY_WH"
# # application override: default activates agent-masking (cortex_code_cli)
# # over MCP; set an honest value to opt out -- see the README.
# # application = "lakesh/mcp"
"""
