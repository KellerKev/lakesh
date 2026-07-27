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


SUPPORTED_TYPES = ("iceberg-rest", "ducklake", "adbc")

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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
    read_only: bool = False
    # shared
    s3: S3Config = field(default_factory=S3Config)
    oauth: OAuthConfig = field(default_factory=OAuthConfig)

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
        if self.oauth.enabled and self.type != "ducklake":
            self._validate_oauth()

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
        read_only=bool(raw.get("read_only", False)),
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
    return Config(profiles=profiles, default=default, source_path=path)


def write_example_config(path: Path) -> None:
    """Drop a working example config file. Used by `lakesh config init`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_EXAMPLE)


_EXAMPLE = """\
# lakesh config — profiles for Iceberg REST catalogs and/or DuckLake
# metastores. Switch between them via `lakesh -p <name>` or by setting
# `default` below.

default = "local"

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

# --- ADBC + OAuth2 device-code login (e.g. Snowflake via an IdP) ----------
# [profiles.snow]
# type         = "adbc"
# driver       = "snowflake"
# catalog      = "snow"
# token_option = "adbc.snowflake.sql.client_option.auth_token"
#
# [profiles.snow.options]
# "adbc.snowflake.sql.account" = "myorg-account1"
# username                     = "kevin"
#
# [profiles.snow.oauth]
# grant                         = "device_code"
# client_id                     = "lakesh-cli"
# device_authorization_endpoint = "https://idp.example.com/oauth2/v1/device/authorize"
# token_endpoint                = "https://idp.example.com/oauth2/v1/token"
# scope                         = "session:role:ANALYST offline_access"
"""
