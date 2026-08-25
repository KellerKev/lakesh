"""DuckDB connection setup.

Three profile types are supported, each with its own ATTACH shape:

1. **iceberg-rest** — standard Iceberg REST catalog. We install + load
   the `iceberg` extension, optionally fetch an OAuth2 token, set up an
   `s3` secret for MinIO / S3 data-file reads, and ATTACH the remote
   catalog as `ice`. Result: `SELECT * FROM ice.<ns>.<table>` works.

2. **ducklake** — DuckLake direct. Install + load the `ducklake` +
   `postgres` extensions, set up an `s3` secret, and ATTACH the DuckLake
   URI (`ducklake:postgres:<dsn>`) as the profile's `catalog` alias.
   This is the same path `duckicelake` uses internally; here it's
   exposed as a catalog you can query alongside (or instead of) an
   Iceberg REST endpoint.

3. **adbc** — any database with an ADBC driver, via the `adbc_scanner`
   community extension. Credentials go into a `CREATE SECRET (TYPE
   adbc, SCOPE …)` (never into the ATTACH string), then the source is
   ATTACHed under the profile's `catalog` alias. The extension is also
   best-effort loaded on the other profile types so `adbc_scan()` /
   `adbc_connect()` etc. are usable from any lakesh session.

### `ACCESS_DELEGATION_MODE 'none'` on the iceberg path

Without it, the iceberg extension builds its own S3 secret from the
REST `config` map with a path-scoped lifetime, and has a
`use_ssl`/`path_style` conflation that causes signature failures on
MinIO for delete-file HEAD requests. With `'none'` the extension falls
back to whatever `CREATE SECRET (TYPE S3, ...)` we already installed —
same pattern as any other `httpfs` consumer.
"""
from __future__ import annotations

import re
import sys

import duckdb

from . import oauth
from .config import ConfigError, Profile


def _host_without_scheme(endpoint: str) -> str:
    """DuckDB's S3 SECRET wants the host, not the full URL."""
    return endpoint.split("://", 1)[-1]


def _install_s3_secret(con: duckdb.DuckDBPyConnection, profile: Profile) -> None:
    """Shared helper — creates an `ice_s3` secret when the profile
    supplies access+secret keys. Used by both profile types for
    data-file reads from MinIO / S3. STS temporary credentials (e.g.
    duckicelake's vended ducklake-credentials) also carry a session
    token, which DuckDB needs in the secret or MinIO rejects the key."""
    s3 = profile.s3
    if not (s3.access_key and s3.secret_key):
        return
    params = [
        s3.access_key,
        s3.secret_key,
        s3.region,
        _host_without_scheme(s3.endpoint) if s3.endpoint else "",
        bool(s3.endpoint and s3.endpoint.startswith("https://")),
        "path" if s3.path_style else "vhost",
    ]
    session_clause = ""
    if s3.session_token:
        session_clause = "SESSION_TOKEN ?,"
        params.insert(2, s3.session_token)
    con.execute(
        f"""
        CREATE OR REPLACE SECRET ice_s3 (
            TYPE S3,
            KEY_ID ?, SECRET ?, {session_clause}
            REGION ?, ENDPOINT ?,
            USE_SSL ?, URL_STYLE ?
        )
        """,
        params,
    )


# --------------------------------------------------------------------------
# adbc_scanner — query anything with an ADBC driver

_adbc_warned = False   # warn once per process, not per MCP tool call


def load_adbc_scanner(con: duckdb.DuckDBPyConnection, *, required: bool = False) -> bool:
    """Install + load the `adbc_scanner` community extension. Best-effort
    by default (offline / unsupported platform must not break startup);
    `required=True` re-raises for adbc profiles that can't work without it."""
    global _adbc_warned
    try:
        con.execute("INSTALL adbc_scanner FROM community")
        con.execute("LOAD adbc_scanner")
        return True
    except duckdb.Error as e:
        if required:
            raise
        if not _adbc_warned:
            _adbc_warned = True
            print(
                f"lakesh: adbc_scanner extension unavailable "
                f"(adbc_* functions disabled): {e}",
                file=sys.stderr,
            )
        return False


_ADBC_OPTION_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")

# The adbc secret type has a fixed schema (verified against
# adbc_scanner on DuckDB 1.4): only these connection options can live in
# the secret. Everything else — dotted driver options like
# `adbc.snowflake.sql.account` — must be passed inline in ATTACH.
_ADBC_SECRET_KEYS = frozenset({"username", "password", "database", "entrypoint"})


def _sql_quote(s: str) -> str:
    return s.replace("'", "''")


def _split_adbc_options(
    options: dict[str, str],
) -> tuple[dict[str, str], dict[str, str]]:
    """(secret_opts, attach_opts) — credentials the secret schema can
    hold vs. driver options that have to ride on the ATTACH."""
    secret = {k: v for k, v in options.items() if k in _ADBC_SECRET_KEYS}
    attach = {k: v for k, v in options.items() if k not in _ADBC_SECRET_KEYS}
    return secret, attach


def _install_adbc_secret(
    con: duckdb.DuckDBPyConnection, profile: Profile, options: dict[str, str]
) -> None:
    """`CREATE SECRET (TYPE adbc, SCOPE …)` holding the credential-shaped
    options (see `_ADBC_SECRET_KEYS`). Values are bound as prepared
    params so they never appear in SQL text or logs; the adbc_scanner
    ATTACH auto-looks the secret up by URI scope."""
    clauses = ["TYPE adbc", "SCOPE ?", "driver ?"]
    params: list[str] = [profile.uri, profile.driver]
    if profile.uri:
        clauses.append("uri ?")
        params.append(profile.uri)
    for key, value in options.items():
        if key not in _ADBC_SECRET_KEYS:
            raise ConfigError(
                f"profile {profile.name!r}: option {key!r} is not storable "
                f"in an adbc secret (allowed: {', '.join(sorted(_ADBC_SECRET_KEYS))})"
            )
        clauses.append(f'"{key}" ?')
        params.append(value)
    con.execute(
        f"CREATE OR REPLACE SECRET adbc_{profile.catalog} "
        f"({', '.join(clauses)})",
        params,
    )


def _adbc_options(profile: Profile, token: str | None) -> dict[str, str]:
    """Merge the profile's driver options with the OAuth bearer token —
    via the `token_option` key and/or `{token}` placeholders in values."""
    opts = dict(profile.options)
    if token:
        if profile.token_option:
            opts[profile.token_option] = token
        opts = {k: v.replace("{token}", token) for k, v in opts.items()}
    return opts


def _adbc_attach_sql(profile: Profile, attach_opts: dict[str, str]) -> str:
    """ATTACH statement carrying driver + non-secret driver options.
    ATTACH takes no prepared parameters, so keys are identifier-validated
    and values single-quote-escaped."""
    parts = [f"TYPE adbc", f"driver '{_sql_quote(profile.driver)}'"]
    for key, value in attach_opts.items():
        if not _ADBC_OPTION_KEY_RE.match(key):
            raise ConfigError(
                f"profile {profile.name!r}: invalid adbc option key {key!r}"
            )
        parts.append(f"\"{key}\" '{_sql_quote(value)}'")
    if profile.read_only:
        parts.append("READ_ONLY")
    return (
        f"ATTACH '{_sql_quote(profile.uri)}' AS {profile.catalog} "
        f"({', '.join(parts)})"
    )


def _connect_adbc(profile: Profile, token: str | None) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    load_adbc_scanner(con, required=True)
    secret_opts, attach_opts = _split_adbc_options(_adbc_options(profile, token))
    _install_adbc_secret(con, profile, secret_opts)
    con.execute(_adbc_attach_sql(profile, attach_opts))
    return con


# --------------------------------------------------------------------------
# native passthrough — send SQL to the source instead of through the ATTACH
#
# Reading through the ATTACHed catalog means DuckDB parses the SQL and
# adbc_scanner translates scans into ADBC calls, which inherits every
# limit of that translation layer. Measured against Snowflake:
#
#   * `SELECT count(*)` naming no column picks an arbitrary column and
#     fails to cast it — "Unimplemented type for cast (TIMESTAMP WITH
#     TIME ZONE -> BIGINT)" on Snowflake, "Could not convert string
#     '<uuid>' to INT64" on Postgres.
#   * A second database on the same connection is invisible: one ATTACH
#     is one database.
#   * `SHOW`, `QUALIFY` and `LATERAL FLATTEN` never reach the source —
#     DuckDB parses first, and it doesn't know that dialect.
#   * Catalog population is eager and serial (one `DESC TABLE` per
#     object), which is why introspection through the ATTACH costs
#     minutes against a remote source. See mcp.py.
#
# Native mode sidesteps all of it: the statement goes to the source
# verbatim and comes back as an Arrow result.


def adbc_native_handle(
    con: duckdb.DuckDBPyConnection, profile: Profile, token: str | None = None
) -> int:
    """Open a raw ADBC connection via `adbc_connect()` and return its
    handle. `adbc_connect` takes a struct whose keys must be literal, so
    keys are identifier-validated — but every *value* is a bound
    parameter, so no credential ever appears in SQL text. That makes
    this strictly safer than the ATTACH path, which has to inline
    non-secret options."""
    if profile.type != "adbc":
        raise ConfigError(
            f"profile {profile.name!r}: native passthrough requires an "
            f"adbc profile (this one is {profile.type!r})"
        )
    keys = ["driver"]
    params: list[str] = [profile.driver]
    if profile.uri:
        keys.append("uri")
        params.append(profile.uri)
    for key, value in _adbc_options(profile, token).items():
        if not _ADBC_OPTION_KEY_RE.match(key):
            raise ConfigError(
                f"profile {profile.name!r}: invalid adbc option key {key!r}"
            )
        keys.append(key)
        params.append(value)
    struct = ", ".join(f"'{key}': ?" for key in keys)
    row = con.execute(f"SELECT adbc_connect({{{struct}}})", params).fetchone()
    return row[0]


def adbc_native_scan(
    con: duckdb.DuckDBPyConnection, handle: int, sql: str
) -> duckdb.DuckDBPyConnection:
    """Run `sql` on the source through an `adbc_connect` handle. The
    statement rides as a bound parameter, so the source's own dialect
    applies and DuckDB never parses it."""
    return con.execute("SELECT * FROM adbc_scan(?, ?)", [handle, sql])


def connect_native(
    profile: Profile,
    *,
    token: str | None = None,
    interactive: bool = True,
) -> tuple[duckdb.DuckDBPyConnection, int]:
    """(connection, handle) for native passthrough — no ATTACH, so none
    of the eager catalog population happens."""
    profile.validate()
    if profile.type != "adbc":
        raise ConfigError(
            f"profile {profile.name!r}: native passthrough requires an "
            f"adbc profile (this one is {profile.type!r})"
        )
    if token is None:
        token = oauth.get_token(profile, interactive=interactive)
    con = duckdb.connect(":memory:")
    load_adbc_scanner(con, required=True)
    try:
        return con, adbc_native_handle(con, profile, token)
    except Exception:
        con.close()
        raise


def _connect_iceberg_rest(profile: Profile, token: str | None) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    for ext in ("httpfs", "iceberg"):
        con.execute(f"INSTALL {ext}")
        con.execute(f"LOAD {ext}")
    load_adbc_scanner(con)

    _install_s3_secret(con, profile)

    auth_type = "oauth2" if token else "none"
    if token:
        con.execute(
            "CREATE OR REPLACE SECRET ice_rest (TYPE ICEBERG, TOKEN ?)",
            [token],
        )

    con.execute(
        f"ATTACH '{profile.warehouse}' AS ice ("
        f"  TYPE ICEBERG, ENDPOINT '{profile.uri}',"
        f"  AUTHORIZATION_TYPE '{auth_type}',"
        f"  ACCESS_DELEGATION_MODE 'none'"
        f")"
    )
    return con


def _connect_ducklake(profile: Profile) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    for ext in ("ducklake", "postgres", "httpfs"):
        con.execute(f"INSTALL {ext}")
        con.execute(f"LOAD {ext}")
    load_adbc_scanner(con)

    _install_s3_secret(con, profile)

    # DuckLake URI: `ducklake:postgres:<libpq DSN>`. The catalog is
    # attached under the profile's `catalog` alias (default "lake"),
    # which is what `\l` / `\d` see as the top-level qualifier.
    uri = f"ducklake:postgres:{profile.postgres_dsn}"
    # Session TZ pinned to UTC so TIMESTAMPTZ stats / partition bounds
    # don't shift by the local offset — matches the guidance in
    # duckicelake's OPERATIONS doc.
    con.execute("SET TimeZone='UTC'")
    con.execute(
        f"ATTACH '{uri}' AS {profile.catalog} "
        f"(DATA_PATH '{profile.data_path}', DATA_INLINING_ROW_LIMIT 0)"
    )
    return con


_AUTH_ERROR_RE = re.compile(
    r"401|403|unauthorized|forbidden|invalid[ _-]?token|token.*expired",
    re.IGNORECASE,
)


def connect(
    profile: Profile,
    *,
    token: str | None = None,
    interactive: bool = True,
) -> duckdb.DuckDBPyConnection:
    """Build an attached DuckDB connection for the profile.

    Dispatches on `profile.type`. Tokens come from `oauth.get_token`
    (cache → refresh → grant flow); pass `token` to bypass that.
    `interactive=False` raises `oauth.AuthRequired` instead of running
    a browser/device login (MCP and piped `exec` use this). For
    `ducklake`, auth happens at the Postgres layer via the DSN.
    """
    profile.validate()
    if profile.type == "ducklake":
        return _connect_ducklake(profile)

    supplied = token is not None
    if token is None:
        token = oauth.get_token(profile, interactive=interactive)
    builder = _connect_adbc if profile.type == "adbc" else _connect_iceberg_rest
    try:
        return builder(profile, token)
    except duckdb.Error as e:
        # A cached token can be expiry-valid but server-revoked. Drop the
        # cache entry and retry once with a freshly acquired token.
        if supplied or not profile.oauth.enabled or not _AUTH_ERROR_RE.search(str(e)):
            raise
        oauth.TokenCache().clear(profile.name)
        token = oauth.get_token(profile, interactive=interactive)
        return builder(profile, token)


def catalog_alias(profile: Profile) -> str:
    """Return the catalog name the ATTACH landed under — `ice` for
    iceberg-rest profiles, `profile.catalog` for ducklake and adbc
    profiles. The REPL + MCP use this to scope information_schema
    queries."""
    return "ice" if profile.type == "iceberg-rest" else profile.catalog
