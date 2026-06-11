"""DuckDB connection setup.

Two profile types are supported, each with its own ATTACH shape:

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

### `ACCESS_DELEGATION_MODE 'none'` on the iceberg path

Without it, the iceberg extension builds its own S3 secret from the
REST `config` map with a path-scoped lifetime, and has a
`use_ssl`/`path_style` conflation that causes signature failures on
MinIO for delete-file HEAD requests. With `'none'` the extension falls
back to whatever `CREATE SECRET (TYPE S3, ...)` we already installed —
same pattern as any other `httpfs` consumer.
"""
from __future__ import annotations

import duckdb
import httpx

from .config import Profile


def _fetch_oauth_token(profile: Profile) -> str | None:
    """Fetch a bearer token via the Iceberg REST `/v1/oauth/tokens`
    endpoint. Returns None if the profile has no OAuth client configured.
    Raises on 4xx/5xx from the catalog."""
    if not profile.oauth.enabled:
        return None
    r = httpx.post(
        f"{profile.uri.rstrip('/')}/v1/oauth/tokens",
        data={
            "grant_type": "client_credentials",
            "client_id": profile.oauth.client_id,
            "client_secret": profile.oauth.client_secret,
        },
        timeout=15.0,
    )
    r.raise_for_status()
    return r.json()["access_token"]


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


def _connect_iceberg_rest(profile: Profile, token: str | None) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    for ext in ("httpfs", "iceberg"):
        con.execute(f"INSTALL {ext}")
        con.execute(f"LOAD {ext}")

    _install_s3_secret(con, profile)

    if token is None:
        token = _fetch_oauth_token(profile)
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


def connect(profile: Profile, *, token: str | None = None) -> duckdb.DuckDBPyConnection:
    """Build an attached DuckDB connection for the profile.

    Dispatches on `profile.type`. For `iceberg-rest`, `token` may be
    passed to reuse a cached one (the REPL does this); otherwise we fetch
    a fresh token when the profile has OAuth configured. For `ducklake`,
    `token` is ignored — auth happens at the Postgres connection layer
    via the DSN.
    """
    profile.validate()
    if profile.type == "ducklake":
        return _connect_ducklake(profile)
    return _connect_iceberg_rest(profile, token)


def catalog_alias(profile: Profile) -> str:
    """Return the catalog name the ATTACH landed under — `ice` for
    iceberg-rest profiles, `profile.catalog` for ducklake profiles.
    The REPL + MCP use this to scope information_schema queries."""
    return "ice" if profile.type == "iceberg-rest" else profile.catalog
