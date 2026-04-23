"""DuckDB connection setup for Iceberg REST catalogs.

Creates a `:memory:` DuckDB connection, installs + loads the `iceberg`
and `httpfs` extensions, writes an S3 secret from the profile, optionally
fetches an OAuth2 bearer token via the Iceberg REST
`/v1/oauth/tokens` endpoint, and ATTACHes the remote catalog as `ice`.

Result: a session where `SELECT * FROM ice.<ns>.<table>` works.

Why `ACCESS_DELEGATION_MODE 'none'`: DuckDB's iceberg extension otherwise
builds its own S3 secret from the REST `config` map with a path-scoped
lifetime, and has a use_ssl/path_style conflation that causes signature
failures on MinIO for delete-file HEAD requests. With `'none'` the
extension falls back to whatever `CREATE SECRET (TYPE S3, ...)` we
already installed — same pattern as any other `httpfs` consumer.
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


def connect(profile: Profile, *, token: str | None = None) -> duckdb.DuckDBPyConnection:
    """Build an attached DuckDB connection for the profile.

    If `token` is None and the profile has OAuth configured, fetches a
    fresh one. Pass an explicit token to reuse a cached one (see the
    CLI's persistent-session path).
    """
    profile.validate()
    con = duckdb.connect(":memory:")
    for ext in ("httpfs", "iceberg"):
        con.execute(f"INSTALL {ext}")
        con.execute(f"LOAD {ext}")

    # S3 credentials first — the iceberg extension falls back to these
    # for data-file reads when ACCESS_DELEGATION_MODE is 'none'.
    s3 = profile.s3
    if s3.access_key and s3.secret_key:
        con.execute(
            """
            CREATE OR REPLACE SECRET ice_s3 (
                TYPE S3,
                KEY_ID ?, SECRET ?,
                REGION ?, ENDPOINT ?,
                USE_SSL ?, URL_STYLE ?
            )
            """,
            [
                s3.access_key,
                s3.secret_key,
                s3.region,
                _host_without_scheme(s3.endpoint) if s3.endpoint else "",
                bool(s3.endpoint and s3.endpoint.startswith("https://")),
                "path" if s3.path_style else "vhost",
            ],
        )

    # OAuth token for the REST catalog — fetched lazily.
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


def _host_without_scheme(endpoint: str) -> str:
    """DuckDB's S3 SECRET wants the host, not the full URL."""
    return endpoint.split("://", 1)[-1]
