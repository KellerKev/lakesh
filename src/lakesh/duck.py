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

import os
import re
import sys
import threading
import time
from contextlib import contextmanager
from typing import Iterator

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

# Set once at startup by the CLI. A module global rather than a
# parameter because `connect()` has three return sites and the builders
# are also reached through the OAuth retry path; a flag that has to be
# passed is a flag that gets missed on one of them.
ALLOW_LOCAL_FILES = False

# Why the sandbox was skipped on the most recent connection, for
# `session_status` to report. A sandbox you believe is on but isn't is
# worse than no sandbox.
LAST_SANDBOX_SKIP: str | None = None


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
    _sandbox(con, profile)
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


# --------------------------------------------------------------------------
# filesystem sandbox
#
# A read-only session is not a sandbox on its own: `SELECT * FROM
# read_csv('/etc/passwd')` is a read, so the write gate lets it through
# and DuckDB happily reaches the local disk from inside a SELECT.
#
# `SET disabled_filesystems='LocalFileSystem'` closes that, and DuckDB
# makes it **self-locking** — measured: attempting to clear it in the
# same process is refused by DuckDB itself with "has been disabled
# previously, it cannot be added again". That is a stronger guarantee
# than anything lakesh could enforce, because it also binds caller SQL.
#
# Measured against DuckDB 1.5.2, with extensions already loaded:
#
#   read_text / read_csv / sniff_csv / glob   Permission Error
#   an existing ADBC handle                   still works
#   a NEW adbc_connect with a driver .so      still works (the ADBC
#                                             manager dlopens it, outside
#                                             DuckDB's filesystem layer)
#   HTTP / S3 through httpfs                  still works — an HTTP read
#                                             fails with a network error,
#                                             not a permission error
#   local Parquet                             blocked (intended)
#   INSTALL / LOAD                            blocked
#
# That last row is the ordering constraint: extension loading reads the
# local extension directory, so the lockdown has to come after every
# INSTALL/LOAD/ATTACH — which is why it is applied at the tail of each
# builder rather than anywhere earlier.
#
# Two alternatives were measured and rejected. `enable_external_access =
# false` also blocks HTTP/S3, which would break every Iceberg and
# DuckLake profile. `allowed_directories` does not combine with
# `disabled_filesystems` — the disabled filesystem wins and the allowed
# directory is blocked anyway — so local and remote access are mutually
# exclusive under lockdown rather than tunable.
#
# Scope, stated plainly: this stops lakesh's own engine reading your
# disk. It does nothing about what a remote source can do, and DuckDB's
# own documentation calls these defence-in-depth rather than a complete
# boundary against untrusted SQL.

_REMOTE_SCHEMES = ("s3://", "gs://", "gcs://", "az://", "azure://",
                   "abfss://", "r2://", "http://", "https://")


def needs_local_files(profile: Profile) -> str | None:
    """Why this profile cannot be sandboxed, or None if it can.

    A DuckLake `data_path` or an Iceberg warehouse on a local path means
    every data read goes through the local filesystem. Locking it would
    produce a session that connects cleanly and then fails on every
    query, which is worse than not sandboxing — so detect it and say so.
    """
    def _is_local(value: str) -> bool:
        return bool(value) and not value.startswith(_REMOTE_SCHEMES)

    if profile.type == "ducklake" and _is_local(profile.data_path):
        return (f"profile {profile.name!r} reads DuckLake data from a local "
                f"path ({profile.data_path!r})")
    if profile.type == "iceberg-rest" and _is_local(profile.warehouse) and "/" in profile.warehouse:
        return (f"profile {profile.name!r} has a local Iceberg warehouse "
                f"({profile.warehouse!r})")
    return None


def sandbox_wanted(profile: Profile) -> bool:
    """Sandbox when the session is read-only and local files weren't
    explicitly asked for. Tying it to read-only rather than a separate
    flag is deliberate: a read-only session is one where the caller has
    said they are only looking, so losing local-file reads costs little
    and closes the hole exactly where it matters."""
    from . import guard

    if ALLOW_LOCAL_FILES or os.environ.get(
            "LAKESH_ALLOW_LOCAL_FILES", "0").lower() in ("1", "true", "yes"):
        return False
    return guard.SESSION.effective(profile).read_only


def _sandbox(con: duckdb.DuckDBPyConnection, profile: Profile) -> None:
    """Apply the sandbox if this session wants one, recording any skip."""
    global LAST_SANDBOX_SKIP
    if not sandbox_wanted(profile):
        LAST_SANDBOX_SKIP = None
        return
    LAST_SANDBOX_SKIP = apply_sandbox(con, profile)


def apply_sandbox(
    con: duckdb.DuckDBPyConnection, profile: Profile
) -> str | None:
    """Block local filesystem access on `con`. Returns the reason it was
    skipped, or None when it was applied.

    Must be called after every INSTALL/LOAD/ATTACH; see the note above.
    """
    reason = needs_local_files(profile)
    if reason:
        return reason
    try:
        con.execute("SET disabled_filesystems='LocalFileSystem'")
    except duckdb.Error as e:                       # pragma: no cover
        return f"DuckDB refused the sandbox setting: {e}"
    return None


# Raised when caller SQL trips the sandbox — including indirectly, via
# DuckDB trying to autoload an extension it needs, which reads the local
# extension directory. The raw error mentions only the filesystem, which
# reads like a bug rather than a policy.
_SANDBOX_ERROR_RE = re.compile(
    r"File system LocalFileSystem has been disabled|"
    r"disabled by configuration",
    re.IGNORECASE,
)


def explain_sandbox_error(exc: Exception) -> str | None:
    """A hint for an error caused by the sandbox, or None."""
    if not _SANDBOX_ERROR_RE.search(str(exc)):
        return None
    return (
        "this session blocks local filesystem access because it is "
        "read-only. Local files, and any DuckDB extension not already "
        "loaded, are unreachable. Re-run with --allow-local-files if you "
        "need them."
    )


# --------------------------------------------------------------------------
# deadlines
#
# Two mechanisms, because one is not enough and pretending otherwise
# would be a lie to the caller.
#
# `interrupt()` is a hard deadline on the pure DuckDB path — measured at
# exactly 2.00s for a 2s deadline. It is NOT one on the native path: a
# statement blocked inside adbc_scan waiting for the source's first byte
# does not observe the interrupt until the driver returns control, so a
# 2s deadline on `SELECT pg_sleep(30)` returned after 30.01s. Mid-stream
# it lands sooner (5.63s on a 20M-row scan) but still lags by the
# driver's buffer size.
#
# So for adbc profiles we *also* ask the source to enforce its own
# statement timeout, which is the only thing that actually bounds wall
# clock there.


_DRIVER_TIMEOUT_RE = re.compile(
    r"statement timeout|canceling statement|query reached its timeout|"
    r"execution time exceeded",
    re.IGNORECASE,
)


class QueryTimeout(Exception):
    """A statement blew its deadline.

    `hard` says whether the deadline was actually enforced — the caller
    needs to know the difference between "we stopped it" and "we asked
    and it stopped when it felt like it"."""

    def __init__(self, seconds: float, elapsed: float, hard: bool) -> None:
        self.seconds, self.elapsed, self.hard = seconds, elapsed, hard
        super().__init__(
            f"query exceeded the {seconds:g}s deadline "
            f"(returned after {elapsed:.1f}s)"
        )


@contextmanager
def deadline(
    con: duckdb.DuckDBPyConnection, seconds: float | None
) -> Iterator[None]:
    """Interrupt `con`'s in-flight statement after `seconds`.

    The whole execute *and* fetch must happen inside the block:
    `adbc_scan` streams, so most of the wall clock is in the fetch.

    Firing `interrupt()` with nothing running is a harmless no-op that
    does not poison the next statement (verified on DuckDB 1.5.2), so
    the race between the final fetch and `timer.cancel()` needs no lock.
    """
    if not seconds or seconds <= 0:
        yield
        return
    fired = threading.Event()
    started = time.monotonic()

    def _fire() -> None:
        fired.set()
        try:
            con.interrupt()
        except Exception:      # already closed — nothing left to abort
            pass

    timer = threading.Timer(seconds, _fire)
    timer.daemon = True        # never hold up interpreter shutdown
    timer.start()
    try:
        yield
    except duckdb.InterruptException as e:
        if not fired.is_set():
            raise              # somebody else's interrupt; don't relabel it
        elapsed = time.monotonic() - started
        raise QueryTimeout(seconds, elapsed, hard=elapsed < seconds * 1.5) from e
    except duckdb.Error as e:
        # The source's own statement timeout, if it beat the watchdog to
        # it. In practice the watchdog fires first (it is armed at the
        # same number of seconds the driver takes 2x to honour), but a
        # raw "canceling statement due to statement timeout" reaching the
        # caller as a generic IO error would be needlessly opaque.
        if not _DRIVER_TIMEOUT_RE.search(str(e)):
            raise
        elapsed = time.monotonic() - started
        raise QueryTimeout(seconds, elapsed, hard=False) from e
    finally:
        timer.cancel()


def _driver_timeout_sql(driver: str, seconds: float) -> str | None:
    """The source's own statement timeout, when it has one we can set
    over the same connection. Matching is a substring test because
    `driver` is usually a path to a shared library."""
    d = driver.lower()
    if "postgres" in d:
        # Verified: returns ('3s',) and the next statement dies with
        # "canceling statement due to statement timeout". Note the
        # driver pays the timeout twice — once on PREPARE and once on
        # COPY — so wall clock is 2x what is configured. Don't halve it
        # to compensate: the multiplier is a driver detail, and the
        # error reports what actually happened.
        return f"SELECT set_config('statement_timeout', '{int(seconds * 1000)}', false)"
    if "snowflake" in d:
        # Reasoned from the docs, not yet measured against an account —
        # callers are told `best_effort` until it is.
        return f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {int(seconds)}"
    return None


def arm_driver_timeout(
    con: duckdb.DuckDBPyConnection, handle: int, profile: Profile,
    seconds: float | None,
) -> bool:
    """Ask the source to enforce `seconds` itself. True if it accepted.

    Best-effort by design: a source that rejects the statement still
    gets the watchdog, and failing the query because we could not arm a
    timeout would be worse than the timeout being soft.
    """
    if not seconds or seconds <= 0:
        return False
    sql = _driver_timeout_sql(profile.driver, seconds)
    if not sql:
        return False
    try:
        adbc_native_scan(con, handle, sql).fetchall()
        return True
    except Exception:
        return False


def connect_native(
    profile: Profile,
    *,
    token: str | None = None,
    interactive: bool = True,
    timeout_s: float | None = None,
) -> tuple[duckdb.DuckDBPyConnection, int]:
    """(connection, handle) for native passthrough — no ATTACH, so none
    of the eager catalog population happens.

    `timeout_s` additionally arms the source's own statement timeout
    where the driver has one; see `arm_driver_timeout`."""
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
        handle = adbc_native_handle(con, profile, token)
    except Exception:
        con.close()
        raise
    arm_driver_timeout(con, handle, profile, timeout_s)
    _sandbox(con, profile)
    return con, handle


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
    _sandbox(con, profile)
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
    _sandbox(con, profile)
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
