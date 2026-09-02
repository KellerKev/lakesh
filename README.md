# lakesh

<p align="center">
  <img src="assets/lakesh-logo.svg" alt="lakesh — duck captain steering a tugboat across the duckicelake" width="640"/>
</p>

`lakesh` is a DuckDB-powered SQL shell for **Iceberg REST catalogs,
DuckLake metastores, any database with an ADBC driver, and any Python
data driver** (PEP 249 or pyiceberg). Profile-based connection
management, an interactive REPL with SQL autocomplete + history +
`psql`-style meta-commands, a one-shot `exec` mode for scripts, and an
MCP server so LLM agents can query your catalogs through the same
plumbing.

It's a thin layer on top of DuckDB's `iceberg`, `ducklake`, and
`adbc_scanner` extensions — DuckDB does the heavy lifting (Parquet
reads, predicate pushdown, joins); `lakesh` handles the ergonomics that
the stock `duckdb` CLI doesn't:

- Multiple catalog profiles in a TOML config, switchable via `-p <name>`.
- Four profile types: **Iceberg REST** (PyIceberg-style endpoint),
  **DuckLake direct** (Postgres metadata + S3 data path), **ADBC**
  (Postgres, MySQL, Snowflake, BigQuery, SQL Server, Trino, Flight SQL,
  SQLite, … via [ADBC drivers](https://arrow.apache.org/adbc/)), or
  **Python** (a PEP 249 driver — `python-duckdb`,
  `snowflake-connector-python`, psycopg, or your own — no ADBC `.so`
  needed; see [Python backends](#python-backends--type--python)).
- Native OAuth2 per data source: **client-credentials, device-code, and
  authorization-code (PKCE)** grants, with token caching + refresh
  (`lakesh auth login/status/logout`).
- S3 / MinIO credential plumbing that avoids `duckdb-iceberg`'s known
  path-style + delegation-mode footguns.
- psql-style `\\l` / `\\d` / `\\timing` / `\\format` meta-commands.
- Result formatting as rich tables, JSON (for pipes), or CSV.
- **MCP server** (`lakesh mcp`) exposing `query`, `search_objects`,
  `list_namespaces` / `list_tables` / `describe_table`, `stage_*`, and
  `session_status` to Claude Desktop, Cline, Continue, etc. Read-only by
  default, with optional render-time PII masking.
- **Tells the source who is asking** — every session is labelled
  (`QUERY_TAG` / `application_name` / HTTP User-Agent), and that claim can
  be made *unforgeable* with a signed attestation a masking policy
  verifies. On Snowflake the python backend can even earn
  `IS_AGENT_ACTIVATED`. See [Telling the source who is
  asking](#telling-the-source-who-is-asking).

Built as the companion CLI for
[`duckicelake`](https://github.com/KellerKev/duckicelake) — a governed
Iceberg REST catalog on DuckLake with byte-level PII masking and
credential vending (see [Pairs with duckicelake](#pairs-with-duckicelake))
— and works against any Iceberg REST catalog (Polaris, Nessie,
Lakekeeper, managed REST, …) or any DuckLake catalog.

## Demo

REPL session against a local `duckicelake` catalog — profile switching,
`\d` / `\l` meta-commands, autocomplete, and a query through the
Iceberg REST → DuckDB iceberg-ext path.

![lakesh demo](demo_videos/lakesh-companion-demo.gif)

📥 Full quality:
[`lakesh-companion-demo.mp4`](demo_videos/lakesh-companion-demo.mp4)

## Pairs with duckicelake

[`duckicelake`](https://github.com/KellerKev/duckicelake) is the governed
half of this pairing: an Iceberg REST catalog on DuckLake with tag-based
RBAC, column masking and row policies **enforced down to the bytes on
object storage**, scoped credential vending, and an audit trail for every
read. `lakesh` is the front door — for humans at the REPL and for LLM
agents over MCP.

What the pairing gives you:

- **Governed queries with zero client changes.** Point an `iceberg-rest`
  profile at the proxy (`uri = "http://127.0.0.1:8181"`) and your reads
  carry your principal's masking/row policies. OAuth2 token minting is
  built in — set `[profiles.<name>.oauth]` and lakesh fetches/reuses the
  JWT per session.
- **Vended-credential sessions.** duckicelake's
  `ducklake-credentials` endpoint vends a reader DSN plus prefix-scoped
  STS credentials; lakesh's `ducklake` profile type accepts the vended
  `session_token` directly (that's why the field exists), so a governed
  DuckLake-direct session is just a profile away — masked view,
  row-level security and all.
- **Agents can't see PII — by construction.** `lakesh mcp` is read-only
  by default (writes require `LAKESH_MCP_WRITE=1`), and duckicelake masks
  and audits every read for a principal without the `unmasked-roles`
  bypass. Wire Claude Desktop to `lakesh mcp`, hand it a governed token,
  and it reads `al***` where a privileged analyst reads the real value —
  same API, every access audited. The full story:
  [duckicelake's ecosystem section](https://github.com/KellerKev/duckicelake#the-ecosystem-duckicelake--lakesh--agents).

Try the pairing end-to-end: in the duckicelake repo, `pixi run demo`
authors the policies, then `pixi run demo-lakesh` runs this exact flow —
an unmasked REST read vs the vended masked view — through lakesh (the
recording above is that demo).

## Install

With [pixi](https://pixi.sh) (recommended — manages Python for you):

```bash
pixi install            # create the env + install lakesh (editable)
pixi run lakesh         # drop into the REPL
pixi run -e dev test    # run the test suite
```

Or a quick global install — puts `lakesh` on your PATH, no env to activate:

```bash
pixi global install --path .
lakesh --help           # works from anywhere
```

Plain pip also works:

```bash
pip install -e '.[dev]'
```

Requires Python ≥ 3.11 (for `tomllib`) and DuckDB ≥ 1.4.

## Quickstart

```bash
# Write an example config at ~/.config/lakesh/config.toml
lakesh config init

# Edit it, then verify connectivity:
lakesh doctor

# Drop into the REPL:
lakesh
# or a specific profile:
lakesh -p prod

# One-shot:
lakesh exec -q 'SELECT COUNT(*) FROM analytics.events'

# JSON output for pipes:
lakesh exec -f json -q 'SHOW TABLES' | jq '.[].table_name'
```

## Config

TOML, discovered via (in order):
1. `$LAKESH_CONFIG` — full path, explicit override
2. `$XDG_CONFIG_HOME/lakesh/config.toml`
3. `~/.config/lakesh/config.toml`

### Iceberg REST profile (default)

```toml
default = "local"

[profiles.local]
uri       = "http://127.0.0.1:8181"
warehouse = "lake"

[profiles.local.s3]
endpoint   = "http://127.0.0.1:9000"
region     = "us-east-1"
access_key = "minioadmin"
secret_key = "minioadmin"
path_style = true

[profiles.local.oauth]
client_id     = "demo-client"
client_secret = "demo-secret"
```

The catalog ATTACHes as `ice`, so you query `SELECT * FROM
ice.<namespace>.<table>`.

### DuckLake direct profile

For local dev or when you want to skip the Iceberg REST layer entirely:

```toml
[profiles.lake_direct]
type         = "ducklake"
postgres_dsn = "dbname=ducklake host=/path/to/.pgsock port=55432 user=ducklake"
data_path    = "s3://lakehouse/data/"
catalog      = "lake"          # the AS <name> in ATTACH

[profiles.lake_direct.s3]
endpoint   = "http://127.0.0.1:9000"
access_key = "minioadmin"
secret_key = "minioadmin"
```

The catalog ATTACHes under the `catalog` name (default `lake`), so
queries use `SELECT * FROM lake.<schema>.<table>`. Same data as the
Iceberg REST view of `duckicelake`, but read directly via the
`ducklake` extension — useful for SQL that writes data (`INSERT` /
`UPDATE` / `DELETE`) because the iceberg-ext doesn't support those
operations through REST.

### ADBC profile — query practically anything

The `adbc_scanner` [community extension](https://duckdb.org/community_extensions/extensions/adbc_scanner)
is loaded by default in every lakesh session, so `adbc_connect()` /
`adbc_scan()` etc. are always available in the REPL. An `adbc` profile
goes further and ATTACHes the source as a catalog, so autocomplete,
`\l` / `\d`, `doctor`, and the MCP tools all work against it:

```toml
[profiles.pg]
type      = "adbc"
driver    = "postgresql"    # manifest name or full path to the driver lib
# URI shape: postgresql://USERNAME:PASSWORD@HOST:PORT/DATABASE
# (username `reporting`, password `hunter2`; URL-encode special chars)
uri       = "postgresql://reporting:hunter2@db.example.com:5432/appdb"
catalog   = "pg"            # tables appear as pg.<schema>.<table>
read_only = true            # optional
```

A complete, copy-paste-ready example (fake credentials, setup steps in
the comments) lives at
[`examples/config.postgres-adbc.toml`](examples/config.postgres-adbc.toml):

```bash
dbc install postgresql
cp examples/config.postgres-adbc.toml ~/.config/lakesh/config.toml
# edit host/db/password, then:
lakesh doctor
lakesh exec -q 'SELECT * FROM pg.public.users LIMIT 5'
```

Drivers that accept option-based auth (e.g. flightsql) can use an
`[profiles.X.options]` table instead of URI credentials — any key
supports `*_env` indirection:

```toml
[profiles.flight.options]
username     = "svc"
password_env = "LAKESH_FLIGHT_PASSWORD"
```

ADBC drivers are shared libraries installed out-of-band — easiest via
the [`dbc` CLI](https://dbc.columnar.tech): `dbc install postgresql`.
Tested drivers include postgresql, mysql, snowflake, bigquery,
sqlserver, trino, flightsql, and sqlite. `lakesh doctor -p pg` checks
the extension, the driver, and the connection, and hints at
`dbc install <driver>` when the driver can't be resolved.

Credential-shaped options (`username`, `password`, `database`,
`entrypoint`) are stored in a DuckDB `SECRET` scoped to the source URI —
they never appear in SQL text. Other driver options (dotted keys like
`adbc.snowflake.sql.account`) ride on the ATTACH. Reads support
projection/filter pushdown; `UPDATE`/`DELETE` through the attached
catalog aren't supported by the extension yet.

Driver quirks worth knowing (verified against a live Postgres):

- **Some drivers only accept credentials embedded in the URI** — the
  postgresql driver rejects `username`/`password` options. Put the full
  DSN in the config directly, or source it from an env var with
  `uri_env` if you'd rather keep the password out of the file:

  ```toml
  [profiles.pg]
  type   = "adbc"
  driver = "postgresql"
  uri    = "postgresql://reporting:s3cret@db.example.com:5432/appdb"
  # or:  uri_env = "LAKESH_PG_DSN"   (literal `uri` wins if both are set)
  ```

- **`SELECT count(*)` naming no column fails** through an attached
  catalog: the extension picks an arbitrary column and fails to cast it
  (`Could not convert string '<uuid>' to INT64` on Postgres,
  `Unimplemented type for cast (TIMESTAMP WITH TIME ZONE -> BIGINT)` on
  Snowflake). Name a column — `count(id)` — or use native mode.

- **Upstream `adbc_scanner` bug — silently wrong data.** `GROUP BY` /
  `DISTINCT` on a **VARCHAR column** read through an attached catalog
  can return corrupted group keys: the values come back as single
  garbage characters and distinct groups collapse into one, with no
  error. Reproduced on a plain local Postgres table, so it is not
  specific to any one source. Plain scans of the same table return
  correct values, and the raw `adbc_scan()` function (i.e. native mode)
  is unaffected. Not fixable in lakesh. Workarounds: prefer `--native`,
  or force a copy with `GROUP BY kind || ''`. **Sanity-check aggregate
  results against the source before trusting them.**

### Native passthrough — `--native`

For `adbc` profiles, `lakesh exec --native` sends your SQL straight to
the source in the source's own dialect, bypassing DuckDB's attached
catalog entirely:

```bash
lakesh exec -p snowflake --native -q 'SHOW DATABASES'
lakesh exec -p snowflake --native -q "
  SELECT warehouse_name, ROUND(SUM(credits_used),2) c
  FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
  WHERE start_time > DATEADD(day,-7,CURRENT_TIMESTAMP())
  GROUP BY 1 ORDER BY c DESC LIMIT 5"
```

Reach for it when the attached-catalog path can't express what you
need — `SHOW`, `QUALIFY`, `LATERAL FLATTEN`, a bare `count(*)`, a second
database — or when it's simply too slow. Catalog population through the
ATTACH is eager and serial (one `DESC TABLE` per object, nothing cached
between connections), so against a remote source the cost is round-trip
latency × object count. Measured against a live Snowflake account,
listing tables took **>240s attached vs ~5s native**.

What you give up is the cross-source join: native mode talks to one
source, so joining it against a local Parquet file needs the DuckDB
path. Credentials are still never inlined — `adbc_connect()` takes every
option value as a bound parameter.

The MCP `query` tool defaults to native for adbc profiles and reports
which mode ran; pass `native=false` to force DuckDB.

### Python backends — `type = "python"`

ADBC is the default, but a profile can be served by a **Python driver**
instead: `python-duckdb`, `pyiceberg`, psycopg, `snowflake-connector-python`,
or your own. Reach for it when your catalog speaks no SQL (`pyiceberg`,
covered below), when there's no ADBC `.so` to install, or when the native
Python driver exposes something ADBC's does not.

The interface is just **PEP 249 (Python DB-API 2.0)** — the standard
`python-duckdb`, `psycopg` and `snowflake-connector-python` already
implement — so shipped backends are thin adapters with no per-driver
code, and your own driver joins with none either.

```toml
[profiles.pg]
type    = "python"
backend = "postgres"           # duckdb | postgres | snowflake | pyiceberg | "module:callable"
dialect = "postgres"           # required: no .so to guess capabilities from

[profiles.pg.options]          # passed straight to the driver's connect()
host   = "db.internal"
dbname = "analytics"
user   = "reporting"
```

Shipped backends (`snowflake` and `postgres` are optional extras;
`duckdb` needs nothing):

```bash
pip install 'lakesh[snowflake-python]'   # snowflake-connector-python
pip install 'lakesh[postgres-python]'    # psycopg
pip install 'lakesh[iceberg-python]'     # pyiceberg (see below)
```

A python profile gets the **whole feature set** through the same code
paths as ADBC — `query`, paging, `list_tables`/`describe_table` with
freshness, `search_objects`, masking, the read-only gate, timeouts.
Writes run exactly once (no `adbc_scan` double-execution).

**Two families of source.** Most drivers speak SQL (DB-API), and lakesh
sends SQL straight to them. A few — `pyiceberg`, a custom REST catalog —
speak no SQL: the `pyiceberg` backend reads **metadata from the catalog
API** and **scans data to Arrow**, which lakesh then queries in an
in-process DuckDB. That reaches Iceberg catalogs DuckDB's own extension
can't (Glue, Hive, SQL catalogs), and gives them the same
`list_tables`/`describe_table`/`search_objects`/`query` surface:

```toml
[profiles.lake]
type      = "python"
backend   = "pyiceberg"
dialect   = "duckdb"          # SQL runs in DuckDB over the scanned Arrow
uri       = "http://catalog:8181"
warehouse = "lake"

[profiles.lake.s3]            # reused for the pyiceberg scan
endpoint   = "http://minio:9000"
access_key = "..."
secret_key = "..."
path_style = true
```

*Verified end to end against [duckicelake](https://github.com/KellerKev/duckicelake):
the same catalog answers through an `iceberg-rest` profile, a `ducklake`
profile, and this `pyiceberg` profile.*

**A custom backend** is a `"module:callable"` in `backend` — no packaging
needed. The callable takes `(profile, *, caller)` and returns either a
DB-API connection (lakesh wraps it) or a full `Session` for a non-SQL
source:

```toml
backend = "mycompany.lakesh_ext:open"
```

```python
# mycompany/lakesh_ext.py
def open(profile, *, caller=None):
    import my_rest_driver           # any PEP 249 driver
    return my_rest_driver.connect(**profile.options)
```

### Which Snowflake driver

The Apache Arrow ADBC Snowflake driver is **frozen at 1.11.0** (April
2026) and was moved out of `apache/arrow-adbc` to the **ADBC Driver
Foundry** (`adbc-drivers/snowflake`), installed with `dbc install
snowflake` — same library name, a drop-in for the `driver` path in an
`adbc` profile. Or sidestep it entirely with a `type = "python"` +
`backend = "snowflake"` profile, which uses `snowflake-connector-python`
and needs no `.so`.

### Agent activation

Snowflake can apply agent-specific masking policies when a session is
*agent-activated* (`SYS_CONTEXT('SNOWFLAKE$CURRENT','IS_AGENT_ACTIVATED')`).
That is set from the login's `application` name, and the **ADBC driver
mangles it** (it force-prepends `[ADBC][Go-…]`), so an ADBC session can
never present the value Snowflake's allowlist accepts —
`agent_activated` is always `FALSE`.

The **python `snowflake` backend can**, because
`snowflake-connector-python` sends `application` verbatim. Measured end
to end:

| driving lakesh | `application` sent | `agent_activated` |
|---|---|---|
| MCP (an agent) | `cortex_code_cli` (default) | **`TRUE`** |
| CLI (a human) | `lakesh/<version> cli` | `FALSE` |

So over MCP the backend **defaults to activating** agent-masking — the
governance-positive outcome when an agent is driving — while a human at
the CLI stays honestly labelled and is not treated as an agent.

> **The tradeoff, stated plainly.** `cortex_code_cli` is Cortex Code's
> identity, so activating this way records the session as Cortex Code
> (`AGENT_TYPE = CORTEX_LITE_AGENT`) in your account's audit trail. It
> rides an **undocumented allowlist** Snowflake can change at any time.
> To label honestly instead, set `application` yourself:
>
> ```toml
> [profiles.warehouse.options]
> application = "lakesh/mcp"    # honest; does not activate
> ```
>
> Check what a session actually is with `lakesh profiles show <p> --probe`.

### Snowflake profile

```toml
[profiles.snowflake]
type      = "adbc"
driver    = "/path/to/libadbc_driver_snowflake.so"  # pip install adbc-driver-snowflake
uri_env   = "LAKESH_SNOWFLAKE_DSN"                  # "USER:PAT@MYORG-ACCOUNT"
catalog   = "snow"
read_only = true

[profiles.snowflake.options]
"adbc.snowflake.sql.account"   = "MYORG-ACCOUNT"    # required
"adbc.snowflake.sql.warehouse" = "MY_WH"
"adbc.snowflake.sql.db"        = "SNOWFLAKE"
```

The split is forced by the driver, and each half fails with its own
opaque numeric code:

- **Credentials must be in the DSN.** `adbc_scanner` hands the ATTACH
  path to the driver as its `uri`, and the Snowflake driver parses it as
  a gosnowflake DSN — a parse that *overwrites* user and password. So
  `username` / `password` options are silently discarded. Symptom:
  `260001: user is empty` or `260002: password is empty` with both
  plainly set.
- **The account must be in `[options]`**, read only from
  `adbc.snowflake.sql.account`. Omit it and you get `260000: account is
  empty` regardless of the DSN.
- **Keep the DSN path-free.** `ACCOUNT/DB/SCHEMA?warehouse=…` breaks
  account parsing entirely.

A PAT goes in the password position. Snowflake login names are often
email addresses, so the DSN has two `@` signs
(`first.last@corp.com:PAT@MYORG-ACCOUNT`) — that's correct, the
userinfo/host boundary is the last one.

Full annotated example:
[`examples/config.snowflake-adbc.toml`](examples/config.snowflake-adbc.toml).

### OAuth2 per data source

Every profile type (except ducklake, which authenticates via its
Postgres DSN) can carry an `[profiles.X.oauth]` block. Three grants:

| Grant | Use case | Required fields |
|---|---|---|
| `client_credentials` (default) | machine-to-machine | `client_id`, `client_secret` (+ `token_endpoint` unless iceberg-rest) |
| `device_code` | CLI login, no local browser needed | `client_id`, `device_authorization_endpoint`, `token_endpoint` |
| `authorization_code` | browser SSO login with PKCE | `client_id`, `authorization_endpoint`, `token_endpoint` |

Common optional fields: `scope`, `audience`, `client_secret`,
`redirect_port` (for IdPs that require an exact pre-registered loopback
redirect URI), and an `[oauth.extra]` table of passthrough form params.
For iceberg-rest profiles the token endpoint defaults to the catalog's
own `/v1/oauth/tokens` — existing configs keep working unchanged.

```toml
[profiles.prod.oauth]
grant                         = "device_code"
client_id                     = "lakesh-cli"
device_authorization_endpoint = "https://idp.example.com/oauth2/v1/device/authorize"
token_endpoint                = "https://idp.example.com/oauth2/v1/token"
scope                         = "catalog:read offline_access"
```

Tokens (access + refresh + expiry) are cached in
`$XDG_STATE_HOME/lakesh/tokens.json` (file mode 0600, same trust level
as config-file secrets) and refreshed automatically. Interactive grants
only prompt when nothing cached is usable:

```bash
lakesh auth login -p prod      # run the flow, cache the token
lakesh auth status             # per-profile cache state
lakesh auth logout -p prod     # drop one profile's token (--all for everything)
```

Where the token lands: for iceberg-rest it goes into the `ICEBERG`
secret as before. For adbc profiles, set `token_option` to the ADBC
driver option that takes the bearer token (e.g. Snowflake's
`adbc.snowflake.sql.client_option.auth_token`), or embed a `{token}`
placeholder inside any option value (e.g.
`authorization_header = "Bearer {token}"` for Flight SQL).

Non-interactive contexts never hang on a login prompt: piped
`lakesh exec` and the MCP server fail fast with a message telling you to
run `lakesh auth login -p <name>` in a terminal. After that one-time
login, cached + refreshed tokens keep the MCP server working
indefinitely (until the refresh token dies).

### Secrets from env vars

Any `client_id` / `client_secret` / `access_key` / `secret_key` /
`postgres_dsn` — and any key inside an adbc `[profiles.X.options]`
table — can be sourced via a `*_env` sibling:

```toml
[profiles.prod.oauth]
client_id_env     = "LAKESH_PROD_CLIENT_ID"
client_secret_env = "LAKESH_PROD_CLIENT_SECRET"

[profiles.prod_lake]
type             = "ducklake"
postgres_dsn_env = "LAKESH_PROD_PG_DSN"
data_path        = "s3://prod-bucket/data/"
```

Literal values win over env lookups when both are set.

## Commands

| Command | Purpose |
|---|---|
| `lakesh` | Launch REPL against default profile |
| `lakesh run -p <name>` | Launch REPL against a named profile |
| `lakesh exec -q '<sql>'` | Run one query and exit (table output) |
| `lakesh exec -f json -q '<sql>'` | JSON output — machine-readable |
| `lakesh exec -f csv -q '<sql>'` | CSV output |
| `lakesh doctor [-p <name>]` | REST probe + auth + extension/driver checks + ATTACH smoke test |
| `lakesh mcp` | Run as an MCP server on stdio for LLM clients |
| `lakesh auth login -p <name> [--force]` | Run the profile's OAuth2 flow, cache the token |
| `lakesh auth status` | Show cached-token state per OAuth-enabled profile |
| `lakesh auth logout [-p <name> \| --all]` | Drop cached tokens |
| `lakesh profiles list` | Enumerate configured profiles |
| `lakesh profiles show <name>` | Dump one profile (secrets redacted) |
| `lakesh config path` | Print where lakesh will read config from |
| `lakesh config init [--force]` | Write an example config |
| `lakesh config show` | Dump the loaded config (secrets redacted) |

Flags that apply to `run` / `exec` / `doctor`:

| Flag | Purpose |
|---|---|
| `-p / --profile <name>` | Profile to use (defaults to `default` in config) |
| `-c / --config <path>` | Config file override |
| `--uri <url>` | Override profile's `uri` (Iceberg REST profiles only) |
| `--warehouse <name>` | Override profile's `warehouse` (Iceberg REST profiles only) |

## MCP server (for LLM agents)

`lakesh mcp` runs a [Model Context Protocol](https://modelcontextprotocol.io)
server on stdio. Configure your MCP client (Claude Desktop, Cline,
Continue, …) to spawn it, and the LLM gets these tools:

| Tool | Purpose |
|---|---|
| `list_profiles()` | Discover what catalogs are configured |
| `search_objects(pattern, profile=None, namespace=None, match="all", limit=200, all_profiles=False)` | **Find** a schema, table or column by name |
| `list_namespaces(profile=None)` | List schemas in a profile's catalog |
| `list_tables(profile=None, namespace=None)` | List tables, optionally scoped |
| `describe_table(namespace, table, profile=None, shape=None)` | Columns, plus whether this is the table to use |
| `stage_upload(local_path, target, profile=None)` | Put a local file where the source can read it |
| `stage_load(table, target, profile=None, file_format=None, create=False)` | COPY INTO an existing table from a stage |
| `stage_list(target, profile=None)` / `stage_remove(...)` | Inspect and clear a staging target |
| `session_status(profile="")` | What this session may do — and, with a profile, who the source thinks you are |
| `set_read_only()` | Give up write access for the rest of the session; cannot be undone |
| `set_masking()` | Mask recognisable PII in every result for the rest of the session |
| `query(sql, profile=None, limit=1000, offset=0, format="json", native=None, timeout_s=None, estimate=False)` | Run SQL and return results |

### Finding things — `search_objects`

The other navigation tools enumerate; this one searches. Without it an
agent that doesn't already know where revenue lives can only
`list_tables` per schema and eyeball the output.

```jsonc
search_objects("revenue")
{
  "pattern": "revenue", "like": "%revenue%", "mode": "native",
  "results": [
    {"namespace": "ANALYTICS", "table": null,          "matched_on": ["schema"]},
    {"namespace": "ANALYTICS", "table": "FCT_REVENUE", "matched_on": ["table"]},
    {"namespace": "ANALYTICS", "table": "ORDERS",      "matched_on": ["column"],
     "columns": [{"column": "REVENUE_USD", "type": "NUMBER(38,2)"}]}
  ],
  "result_count": 3, "truncated_at": null
}
```

Schemas, tables and columns are searched in **one** statement — a
three-way `UNION ALL` over the source's own `information_schema`,
filtered and capped on the source, so only matches cross the wire.
Results are grouped per object: a table matching on six columns is one
entry, not six, with at most 10 columns listed (`columns_truncated` says
when there were more).

Pattern rules:

- Matching is **always case-insensitive** (`ILIKE`). It has to be —
  Snowflake upper-cases unquoted identifiers and Postgres lower-cases
  them, so the same logical name is spelled two ways depending on which
  source you ask.
- A bare word matches anywhere: `revenue` finds `FCT_REVENUE` and
  `revenue_usd`.
- `%` is a wildcard **and** switches off the implicit wrap, so
  `revenue%` is a prefix match.
- `_` is **literal**, not a single-character wildcard. Analytic table
  names are full of underscores and nobody types `fact_revenue` meaning
  `fact<any char>revenue`.
- A backslash is rejected rather than escaped: escaping differs across
  sources (Snowflake reads it as an escape inside string literals,
  Postgres and DuckDB do not), so there is no one right answer.

`namespace` is the main lever for keeping a search fast on a large
warehouse, and `match="table"` drops the column leg entirely when you
only want object names. `all_profiles=True` fans out across every
configured profile — it costs a connection per profile inside a single
call, and returns a partial-results envelope so one profile with a cold
token reports itself in `errors` instead of failing the whole search.

`truncated_at` counts **raw matches**, not grouped entries, so it can
legitimately exceed `result_count`.

`lakesh mcp -c <path>` points the server at a specific config; so does
`$LAKESH_CONFIG`.

### Read-only by default

`query` rejects anything that doesn't start with
`SELECT` / `SHOW` / `DESCRIBE` / `WITH` / `EXPLAIN` / `PRAGMA` / `VALUES`.
Set `LAKESH_MCP_WRITE=1` in the server's environment to enable
INSERT / UPDATE / DELETE / DDL / `CALL ducklake_…` procedures. Keeps
LLM-driven SQL safe by default.

A profile with `read_only = true` refuses writes **even with
`LAKESH_MCP_WRITE=1`** — the profile is the more specific statement of
intent, and native passthrough opens its own ADBC connection that the
ATTACH's `READ_ONLY` flag never sees.

### Deadlines

`query` applies a **120-second deadline by default**. Without one a slow
query hangs the client, which reports `MCP error -32001: Request timed
out` — indistinguishable from a dead server, and a real diagnostic
time-sink. A server-side deadline returns a labelled error the model can
act on instead.

Override per call with `timeout_s` (`0` disables), for the whole server
with `LAKESH_MCP_TIMEOUT_S`, or per profile:

```toml
[profiles.lake]
query_timeout_s = 60
```

A profile's `query_timeout_s` is a **ceiling**, not a default: a caller
may ask for less but never more. Same precedent as `read_only` beating
`LAKESH_MCP_WRITE` — config is the operator's binding statement of
intent.

**How well the deadline is enforced depends on the path, and the
response says which applied** via `enforced`:

- `hard` — the DuckDB path. Measured at exactly 2.00s for a 2s deadline.
- `best_effort` — native mode. DuckDB's `interrupt()` cannot abort a
  statement blocked inside the ADBC driver waiting on the source: a 2s
  deadline on `SELECT pg_sleep(30)` returned after **30.01s**. So in
  native mode lakesh *also* asks the source to enforce its own statement
  timeout where the driver has one (`set_config('statement_timeout', …)`
  on Postgres, `ALTER SESSION` on Snowflake). On Postgres that lands at
  roughly **2×** the requested seconds, because the driver applies the
  timeout once on prepare and once on fetch. Across three identical runs
  of a 3s deadline we measured 6.0s, 6.0s and 3.0s — which is exactly why
  `enforced` and `elapsed_s` are reported per call rather than promised
  up front.

Timeout errors carry `error_type: "timeout"` so a model can branch on
them rather than pattern-match the message.

### Pagination

`offset` pages past the `limit` cap:

```jsonc
query("SELECT n FROM t ORDER BY n", limit=3, offset=3)
{"rows": [...], "row_count": 3, "offset": 3,
 "has_more": true, "next_offset": 6, "enforced": "best_effort"}
```

Follow `next_offset` until it is `null`. `has_more` is **exact** — a
sentinel row past the limit is fetched — unlike the older
`truncated_at`, which cannot distinguish "exactly `limit` rows" from
"truncated" and is kept only for compatibility.

Two things to know:

- **Each page re-executes the statement.** Tools are stateless by
  design: the connection closes when the call returns, so there is no
  cursor to resume. A second page is cheap; a fifty-page sweep is fifty
  warehouse executions. `offset` is capped at 100,000 to make that hard
  to do by accident.
- **Paging without a top-level `ORDER BY` is not stable**, and because
  every page is a separate execution that is a real risk rather than a
  theoretical one. The response adds a `warnings` entry when it doesn't
  see one.

### Sizing a query before running it

On a metered warehouse, execution is money. `estimate` answers "how big
is this" **instead of** running the statement:

```jsonc
query("SELECT * FROM t WHERE i % 3 = 0", estimate=true, native=false)
{"estimate": true, "mode": "duckdb", "method": "explain",
 "estimated_rows": 200000, "plan": "…", "note": "optimizer estimate, not a count"}
```

`estimate="count"` instead wraps the statement in `count(*)` for an
**exact** figure. That is opt-in and never an automatic fallback,
because it executes the scan on the source — spending credits the agent
did not knowingly authorise would be a bad default.

What a source can honestly report differs, so `method` says which
happened:

| Source | `method` | Numbers |
|---|---|---|
| DuckDB path | `explain` | `estimated_rows` from the optimizer, plus the plan |
| Snowflake native | `explain` | plan verbatim — the shape is unverified here, so it is not parsed into numbers we can't vouch for |
| Postgres native | `unavailable` | none — see below |

**Postgres over ADBC cannot EXPLAIN at all.** The driver wraps every
statement in `COPY (...) TO STDOUT`, and Postgres rejects
`COPY (EXPLAIN ...)` with a syntax error (the same is true of `SHOW` and
`SET`). Rather than fail opaquely, the response carries a `reason`
naming the cause and the two things that do work — `estimate="count"`,
or `native=false` to plan through DuckDB.

`estimated_rows` appears **only when a real number was extracted** —
never as a `null` or a `0`. A model reading `estimated_rows: 0`
concludes the query is free.

### Freshness and canonicality

An agent can run perfectly correct SQL against the wrong table and never
know. Mark up your tables and `list_tables`, `describe_table` and
`search_objects` will carry the warning:

```toml
[profiles.lake]
status        = "canonical"   # profile-wide default
max_staleness = "24h"

[profiles.lake.tables]
"analytics.fct_revenue"    = { status = "canonical", max_staleness = "6h", note = "billing source of truth" }
"analytics.fct_revenue_v1" = { status = "deprecated", superseded_by = "analytics.fct_revenue" }
"staging.*"                = { status = "deprecated", note = "raw landing zone; do not query" }
```

Keys **must be quoted** — an unquoted `analytics.fct_revenue` is TOML
dotted-key syntax and nests silently instead of becoming a literal key.
Globs are allowed, and the **most specific** match wins regardless of
file order: an exact key beats a glob, a longer glob beats a shorter one.
Matching is case-insensitive, because Snowflake upper-cases unquoted
identifiers and Postgres lower-cases them.

```jsonc
describe_table("ANALYTICS", "FCT_REVENUE")
{"namespace": "ANALYTICS", "table": "FCT_REVENUE",
 "status": "canonical", "note": "billing source of truth",
 "freshness": {"state": "fresh", "age_seconds": 3991,
               "last_modified": "2026-08-26T10:35:07+02:00",
               "max_staleness_seconds": 21600, "source": "LAST_ALTERED"},
 "columns": [...]}
```

Fields are **omitted when there is nothing to say**, so a catalog with no
annotations against a source with no timestamps produces the same output
it always did.

#### The `describe_table` envelope is opt-out

That envelope is a change of shape: `describe_table` used to return a
bare array of columns. The envelope is the better default, because it is
the one place an agent can be told a table is deprecated *before* it
builds a query on it — but if something already parses the array, keep
it:

```toml
describe_table_shape = "array"   # top level, not inside a profile
```

or `LAKESH_MCP_DESCRIBE_SHAPE=array` in the server's environment, or per
call with `describe_table(..., shape="array")`.

Precedence is **call → environment → config file**. Unlike the query
deadline, where a profile's `query_timeout_s` is a ceiling because it is
a safety property, this is a presentation preference, so the caller that
knows what it parses gets the last word.

The trade is explicit: a bare array has nowhere to report `status`,
`superseded_by` or `freshness`, so you lose the deprecation warning. In
exchange lakesh skips the extra round trip that would have fetched them.

#### Which sources can actually report freshness

This is asymmetric and the output is designed to make the gaps visible
rather than paper over them.

| Source | Timestamp | Reported |
|---|---|---|
| Snowflake | `LAST_ALTERED` | `last_modified`, `row_count`, `bytes` |
| Postgres | **none** | `row_count` (estimate) and `bytes` only |
| Other ADBC | none | nothing |
| Iceberg REST / DuckLake | not yet | nothing |

`freshness.state` is one of four values, and the fourth is the point:

- `fresh` / `stale` — a timestamp and a threshold both exist.
- `unrated` — the age is known, nobody said what is acceptable.
- `unknown` — **the source cannot report a last-modified time at all.**

`unknown` is never rendered as `fresh`. An agent reading a missing signal
as a passing one is the exact failure this feature exists to prevent.

Two caveats worth internalising:

- **Postgres has no honest freshness signal, and lakesh will not invent
  one.** Its ANSI `information_schema.tables` has no temporal column at
  all. The only timestamps available are
  `pg_stat_user_tables.last_analyze` / `last_autovacuum`, which are
  statistics-collector artifacts — NULL until autovacuum happens to
  fire, wiped by `pg_stat_reset()`, and decoupled from when a row last
  landed. Reporting one as `last_modified` would be worse than reporting
  nothing: "unknown" makes an agent ask, a wrong timestamp makes it
  confidently proceed. Row counts come from `pg_class.reltuples` and are
  flagged `row_count_is_estimate`; a table that has never been
  `ANALYZE`d reports no count rather than zero.
- **Snowflake's `LAST_ALTERED` is a signal, not a proof.** It moves on
  DDL and metadata operations as well as DML, and on a view it tracks the
  definition changing rather than the data. It is labelled
  `"source": "LAST_ALTERED"` so you can discount it accordingly.
  `row_count` and `bytes` are NULL for views and external tables.

On Snowflake, freshness on `list_tables` costs **zero extra round trips**
— the columns ride on the query already being sent. Measured: 203 tables
with freshness in 6.4s. `describe_table` costs one extra statement on the
already-open handle.

Annotations are **unenforced assertions**: nothing checks that
`ANALYTICS.FCT_REVENUE` still exists, so a rename silently drops its
annotation and the deprecated twin keeps looking clean.

### Staging local files

```bash
lakesh stage put  -p snowflake ./export.csv @~/exports
lakesh stage load -p snowflake MY_TABLE @~/exports      # COPY INTO
lakesh stage list -p snowflake @~/exports
lakesh stage rm   -p snowflake @~/exports
```

Over MCP: `stage_upload`, `stage_load`, `stage_list`, `stage_remove`.

The generic capability is "put a local file where this source can read
it". Snowflake internal stages are implemented; DuckLake and Iceberg
would stage to object storage, which is the same capability with a
different backend and is not written yet. An engine without it says so
rather than emitting a statement it does not have.

**Uploads are refused unless you say where from.** There is no default:

```toml
[profiles.snowflake]
upload_roots     = ["~/data/exports", "/tmp/lakesh"]
max_upload_bytes = 104857600        # optional, defaults to 100 MB
```

An unconfigured allow-list means the feature is off, not that everything
is permitted — a working directory is not a security boundary, and over
MCP the caller is a model. Symlinks are resolved *before* the containment
check, so a link inside an allowed root pointing at `/etc/passwd` is
refused. Directories, FIFOs and oversized files are refused too.

**This fence is not the filesystem sandbox, and does not depend on it.**
Measured: with `disabled_filesystems='LocalFileSystem'` active and
DuckDB's own `read_text('/etc/hosts')` refused, a `PUT` still reached the
driver and opened the local path — the ADBC driver is `dlopen`ed outside
DuckDB's filesystem layer. The sandbox binds lakesh's engine; the
allow-list binds the upload. `PUT`, `GET` and `REMOVE` are also writes to
the gate, so a read-only session refuses them; `LIST` is a read.

#### Loading a staged file

`stage load` runs `COPY INTO`, from a stage into an **existing** table:

```toml
[profiles.snowflake]
file_format       = "TYPE=CSV SKIP_HEADER=1"      # default for loads
infer_file_format = "MYDB.FMTS.CSV_INFER"         # only needed for --create
```

Three deliberate limits:

- **Load only.** `COPY INTO` runs in both directions on Snowflake, and
  the unload form (`COPY INTO @stage FROM table`) writes table contents
  out to a stage — an export path, not an import one. lakesh composes the
  statement itself and requires the source to look like a stage, so the
  direction cannot be inverted.
- **The table must exist**, unless you pass `--create`. Off by default
  because a mistyped table name would then quietly create a new table
  instead of failing, and inferred types are usually wrong in ways that
  surface much later. `--create` also needs `infer_file_format`, because
  Snowflake's `INFER_SCHEMA` takes a **named** file format object and does
  not accept an inline spec.
- **The table name is validated, not escaped.** A table name cannot be a
  bound parameter, so anything that is not `table`, `schema.table` or
  `db.schema.table` is refused.

The row count is reported from a before/after `count(*)` rather than the
statement's own output, for the same reason uploads are verified by
listing.

**Uploads are verified by listing afterwards.** A `PUT` over this path
returns its column names and no rows, so its own response cannot report
success — and Snowflake's docs separately warn that a successful status
does not mean files moved. If the file is not in the listing, the upload
is reported as failed.

### Native SQL, per platform

lakesh runs each engine's own SQL, not a lowest common denominator. What
that means concretely:

- **DuckLake / Iceberg / DuckDB** — `CALL ducklake_snapshots('lake')`,
  DuckDB's `FROM`-first syntax, `TABLE t`, `PIVOT`, `QUALIFY`.
- **Snowflake** — `SHOW`, `DESCRIBE`, `LIST @stage`, `QUALIFY`,
  `LATERAL FLATTEN`, and stored procedures via `CALL` (see below).
- **Postgres** — `DO` blocks, dollar-quoted function bodies (`$$` and the
  tagged `$BODY$` form), `EXPLAIN (ANALYZE, FORMAT JSON)`.

Engine differences live in one place, `src/lakesh/dialect.py`, as data
rather than as branches. Each capability degrades to *unavailable* rather
than to a wrong answer: an engine with no `EXPLAIN` reachable over its
path says so instead of being sent Snowflake's spelling and returning a
syntax error.

**First-class:** DuckLake/Iceberg/DuckDB, Postgres, Snowflake — the ones
that can actually be tested. Everything else (MySQL, Trino, BigQuery,
SQL Server, SQLite) gets an ANSI profile that claims nothing it cannot
deliver. Writing those profiles from documentation is how a "universal"
tool acquires quietly-wrong behaviour, so they are deliberately absent
until there is a source to test against.

If the driver-path guess is wrong for your layout, correct it:

```toml
[profiles.mysource]
dialect = "postgres"     # duckdb | postgres | snowflake | ansi
```

#### Stored procedures and `CALL`

`CALL` cannot be classified from the statement. Snowflake deprecated the
volatility keywords for procedures, a procedure body can build SQL at
runtime, and procedures are not atomic — one that fails midway can still
have written. So lakesh does not guess.

In a read-only session a `CALL` is refused unless the procedure is known
to be a read. DuckLake's read procedures are a closed set and ship as
known (`ducklake_snapshots`, `ducklake_table_info`, `ducklake_list_files`
and friends); its write procedures are not. For anything else, vouch for
it yourself:

```toml
[profiles.lake]
read_procedures = ["my_reporting_proc", "monthly_rollup"]
```

That is a **declaration, not a verification** — lakesh cannot check what
a procedure does, and this list is you saying you have.

### Telling the source who is asking

An agent driving lakesh over MCP used to look like any other ADBC client.
Now every session lakesh opens is labelled with whether a human or an
agent is behind it, so the engine's audit trail can tell them apart:

| profile | audit label | readable variable | what the source sees |
|---|---|---|---|
| Snowflake (ADBC) | `QUERY_TAG` | `LAKESH_CLIENT` | both, plus `QUERY_HISTORY` |
| Postgres (ADBC) | `application_name` | `lakesh.client` | both, in `pg_stat_activity` |
| DuckLake | — | `lakesh_client` (DuckDB) | its **Postgres metastore**, via `application_name` in the DSN |
| Iceberg REST / duckicelake | — | `lakesh_client` (DuckDB) | the HTTP **User-Agent** |
| anything else (ANSI) | — | — | nothing — reported as unavailable |

Set at connect time, because none of them survive a new connection and
lakesh opens one per call. On by default, one statement:

```toml
[profiles.lake]
session_context = false          # opt out
query_tag       = "acme-etl"     # or override the label

[profiles.lake.session_variables]
team = "data-eng"                # extra variables alongside `client`
```

**This is attribution, not access control.** The value is
client-asserted: the same credentials that set `LAKESH_CLIENT = 'mcp'`
can set it to anything, or leave it unset. A masking policy *can* read it
— `GETVARIABLE` is callable from a policy body, verified by creating one
— but such a policy is trusting the client to be honest about itself.

Two path caveats, both measured. On **DuckDB-hosted engines** the
variable is local to the process, so nothing server-side reads it; the
signals that actually cross the wire there are the metastore's
`application_name` and the HTTP User-Agent. And an **ADBC profile
reached through the attached-catalog path** (without `--native`) has no
handle to send the statement down, so it reports `stamped: false` with a
reason rather than pretending.

#### Snowflake agent activation, over ADBC vs. the Python backend

Snowflake can apply policies keyed on
`SYS_CONTEXT('SNOWFLAKE$CURRENT', 'IS_AGENT_ACTIVATED')`, derived from the
login's `application` name. There are three ways this plays out, and
`--probe` always tells you which you got:

- **Over the ADBC path it cannot be reached.** It is not a settable
  session variable (`ALTER SESSION SET IS_AGENT_ACTIVATED` and
  `SYSTEM$SET_SESSION_CONTEXT` both error), and the ADBC driver
  force-prefixes the application name with `[ADBC][Go-…]`, so Snowflake's
  allowlist never sees the bare value.
- **The Python `snowflake` backend reaches it**, because it sends
  `application` verbatim — see [Agent activation](#agent-activation).
- **Or authenticate through an agentic OAuth integration** (below), which
  earns it from the authentication itself.

Either way, lakesh **reports the truth** so you never assume a policy
applies when it does not:

```bash
lakesh profiles show snow --probe
```
```
source session
  agent_activated  = FALSE
  user_name        = ANALYST_SVC
  role_name        = ANALYST
  client           = Go 1.19.0
  lakesh_client    = cli

  NOT agent-activated: policies keyed on IS_AGENT_ACTIVATED will not fire.
```

Over MCP the same check is `session_status` with a `profile` argument.

**The OAuth route**, if you'd rather earn activation from authentication
than assert it via the Python backend's `application`. An `ACCOUNTADMIN`
creates the integration once:

```sql
CREATE SECURITY INTEGRATION lakesh_agent
  TYPE = OAUTH
  OAUTH_CLIENT = CUSTOM
  OAUTH_CLIENT_TYPE = 'CONFIDENTIAL'
  OAUTH_REDIRECT_URI = 'http://localhost:8080/callback'
  IS_AGENTIC = TRUE
  ENABLED = TRUE;
```

then point a profile at it using the OAuth support lakesh already has:

```toml
[profiles.snow]
type         = "adbc"
driver       = "snowflake"
token_option = "adbc.snowflake.sql.client_option.auth_token"

[profiles.snow.options]
"adbc.snowflake.sql.account"   = "myorg-account1"
"adbc.snowflake.sql.auth_type" = "auth_oauth"   # required — see below

[profiles.snow.oauth]
grant                 = "authorization_code"
client_id             = "..."
authorization_endpoint = "https://myorg-account1.snowflakecomputing.com/oauth/authorize"
token_endpoint         = "https://myorg-account1.snowflakecomputing.com/oauth/token-request"
```

Re-run `--probe` afterwards; `agent_activated` is how you know it worked.

#### Signed attestation — making the claim unforgeable

The stamp above is client-asserted. If you want a policy to *act* on it,
lakesh can sign it instead: a short-lived token that a UDF inside a
masking policy verifies. No valid signature, no unmasked data.

```bash
lakesh session keygen --kid agent-2026-08 -o ~/.config/lakesh/keys/agent.key
```
```toml
[profiles.snow.signing]
method   = "hmac"                                # hmac (default) | ecdsa
kid      = "agent-2026-08"
key_file = "~/.config/lakesh/keys/agent.key"     # or key_env / key_keychain
```
```bash
lakesh session install-sql -p snow --label human > verifier.sql   # review, then run as ACCOUNTADMIN
```

That installs the verifier and a masking policy. Then the same credential
gives different answers:

```console
$ lakesh exec -p snow -q 'SELECT email FROM customers LIMIT 1'   # key configured
ada@example.com
$ lakesh exec -p snow -q 'SELECT email FROM customers LIMIT 1'   # key removed
***masked***
```

Verified against a live account, the policy fails closed on every one of:
no proof, wrong secret, garbage, a truncated proof, and **a valid proof
replayed into a different session**.

That last one is not optional. `SET x = '<proof>'` is written verbatim to
`ACCOUNT_USAGE.QUERY_HISTORY`, kept for a year and readable by
`GOVERNANCE_VIEWER` — and bind variables don't help, they just move the
value to the `BIND_VALUES` column. So every proof is bound to one
`CURRENT_SESSION()`. A proof lifted out of query history is useless
anywhere else.

#### Which method

|  | client holds | in query history | cost, 1M rows | forgeable by |
|---|---|---|---|---|
| `hmac` (default) | shared secret | the proof | **0.41s** | anyone who can read the keys table |
| `ecdsa` | private key | the token | 2.75s | nobody |

Baseline with no policy at all is 0.18s, so `hmac` costs ~0.2s per query
and `ecdsa` ~2.6s. `hmac` is the default because that is a tax an agentic
tool pays on every call.

The `hmac` secret never appears in DDL — the generator puts it in a table
the policy owner alone can read. Verified: a role with only `SELECT` on
the protected table is **denied** on the keys table, on `GET_DDL` of both
the function and the policy, and on calling the verifier directly, *and*
a valid proof still unmasks. That is Snowflake evaluating the policy body
with the owner's rights. Choose `ecdsa` when the requirement is that the
source hold nothing forgeable at all.

**Two things neither method gives you.**

*The trust label comes from the key, never from anything the client
says.* Generate a **separate key per caller** — a deployment holding only
the agent key cannot mint a human proof.

*It only separates callers as far as the keys are separated.* The key
lives on the client. An agent with shell access on a machine that also
holds the human key can read it and sign as a human. Against a *different
client* with the same credential — SnowSQL, DBeaver, a leaked PAT — this
is strong, and that is the property worth building on: the tool becomes
the gate, not just the credential. Against the agent itself it is only as
good as your key isolation, which lakesh cannot enforce. Run the MCP
server as an OS user owning only its own key, or use `key_keychain`.

<details>
<summary>Why the verifier looks the way it does (measured)</summary>

Every figure is through a real masking policy over 1M rows, best of three:

| policy body | best | mean |
|---|---|---|
| no policy | 0.18s | 0.24s |
| HMAC, inlined, session-bound | **0.41s** | 0.90s |
| HMAC via a helper SQL UDF | 1.00s | 1.36s |
| `IS_AGENT_ACTIVATED` (platform signal) | 0.81s | 0.86s |
| JavaScript UDF, `IMMUTABLE` | 1.09s | 1.29s |
| Python UDF, `IMMUTABLE` | 2.13s | 2.23s |
| Python ECDSA verify | 2.75s | 3.09s |
| Java ECDSA verify | 2.85s | 3.18s |
| Python UDF, `VOLATILE` | 4.01s | 4.20s |
| Java UDF, `VOLATILE` | 5.09s | 5.33s |

- **The cost is the runtime, not the crypto.** A Python UDF returning a
  constant costs the same as one verifying ECDSA, and every figure is
  flat from 1e3 to 1e6 rows — policy bodies are evaluated once per query.
- **`IMMUTABLE` is worth ~2× on every UDF runtime.** It goes after
  `LANGUAGE`; anywhere else is a bare "syntax error line 7".
- **Java is the slowest**, matching the documented recompile-per-statement
  for an inline handler without `TARGET_PATH` — despite having the best
  crypto story (`KeyFactory`/`Signature` do work in the sandbox).
- **JavaScript is the fastest UDF and unusable here**: no crypto, no
  `require`, `eval()` disabled.
- **A scalar SQL UDF called from a policy is not free** — factoring the
  HMAC into a helper cost 0.6s, so it is inlined.
- The proof carries **no timestamp**. One was tried and removed: it cost
  another 0.6s, a replayed proof is already refused by the session
  binding, and it made correctness depend on the client clock matching
  Snowflake's — where skew masks everything and looks like a bad secret.

</details>

> **`auth_type` is required and lakesh enforces it.** Measured: with a
> bearer token supplied but `auth_type` unset, the Snowflake driver
> **silently discards the token** and authenticates with whatever else
> the DSN carries. It connects, so there is no error to debug from — the
> session just isn't the one you configured, and agent policies quietly
> never fire. A profile with `oauth` and no `auth_type` is now refused at
> config load.

### Read-only sessions

A user *or the agent itself* can give up write access for the rest of a
session, and it cannot be taken back:

```bash
lakesh exec -p pg --read-only -q '...'   # this invocation
lakesh run  -p pg --read-only            # this REPL sitting (also \readonly)
lakesh mcp  --read-only                  # every call this server serves
```

or `LAKESH_READ_ONLY=1`, or `read_only = true` on a profile. Over MCP the
agent can call `set_read_only()` itself, and `session_status()` reports
what is currently in force.

Two layers, and they only ever tighten. **Operator policy** — the profile
key, the env var, `lakesh mcp --read-only` — and **caller narrowing** —
the CLI flag, `\readonly`, `set_read_only()`, `query(read_only=True)`.
Precedence is boolean OR: a layer may add a restriction, never subtract
one, and `LAKESH_MCP_WRITE` stays subordinate to both. There is no API to
relax a restriction, which is what makes "cannot be relaxed" true rather
than merely intended.

Refusals name the verb and the layer, because a caller told only
"blocked" retries uselessly:

```jsonc
{"error": "write rejected: this statement contains `DROP` and the session is
           read-only. The restriction comes from the operator's config
           (profile 'pg' read_only) and cannot be relaxed from here …",
 "error_type": "read_only_blocked", "blocked": "DROP",
 "restriction": {"source": "policy", "relaxable": false}}
```

When a restriction is in force the write check is the stronger one, which
also catches a write smuggled inside a CTE or after a semicolon —
`WITH x AS (INSERT …) SELECT * FROM x` and `SELECT 1; DROP TABLE t` both
pass the leading-keyword check that guards writes otherwise. `ATTACH`,
`COPY` and `INSTALL` count as writes: a read-only session that can attach
a writable database is not read-only in any useful sense.

#### Read-only also blocks local files

`SELECT * FROM read_csv('/etc/passwd')` is a read, so the write gate
alone would let it through. A read-only session therefore also applies
`SET disabled_filesystems='LocalFileSystem'`, which blocks `read_csv`,
`read_text`, `read_parquet` and `glob`. **DuckDB makes that setting
self-locking** — caller SQL cannot turn it back on, which is a stronger
guarantee than anything lakesh could enforce in Python.

Measured, so you know what still works: an existing ADBC handle, a *new*
`adbc_connect` (the driver `.so` is `dlopen`ed outside DuckDB's
filesystem layer), and HTTP/S3 through httpfs — an HTTP read fails with a
network error, not a permission error. What stops working is local
files, including the local-Parquet join. Use `--allow-local-files` (or
`LAKESH_ALLOW_LOCAL_FILES=1`) if you need them.

Two cases skip the sandbox automatically and say so: a DuckLake profile
whose `data_path` is a local directory, and an Iceberg warehouse on a
local path. Locking those would produce a session that connects cleanly
and then fails on every query, which is worse than not locking.

**What it is not.** This stops lakesh's own engine reading your disk. It
does nothing about what the remote source can do — a Snowflake query
still runs with whatever that role can reach — and DuckDB's own
[security docs](https://duckdb.org/docs/stable/operations_manual/securing_duckdb/overview)
call these defence-in-depth rather than a complete boundary against
untrusted SQL. For real isolation, run lakesh in a container.

**One honest limit remains.** An agent able to spawn a *second*
`lakesh mcp` gets a fresh session, exactly as Snowflake's equivalent
feature documents. Caller narrowing is a guardrail; only the policy layer
travels to every spawn.

### Hiding sensitive data

```bash
lakesh exec -p pg --mask mask  -q 'SELECT * FROM app.customers LIMIT 5'
lakesh exec -p pg --mask audit -q '...'     # report, don't mask
```

```toml
[masking]
mode  = "mask"                      # off | mask | audit
rules = ["pii.email", "pii.phone"]  # override the default-on set
```

Also `LAKESH_MASK=mask`, a per-profile `[profiles.X.masking]`, the MCP
`query(mask="mask")` parameter, and `set_masking()` for the agent to
latch it. Like read-only it only tightens: `audit` returns unmasked rows,
so it is *weaker* than `mask` and cannot be reached from it.

Masked values keep the shape of their type — `***masked***` for strings,
`0` for integers, `0.00` for decimals (scale preserved), `9999-12-31` for
dates. The type comes from the **value**, not the column's declared type,
because Postgres `numeric(10,2)` arrives as a `DECIMAL` through the
attached catalog and as a string through native ADBC.

#### What it will and won't catch

Detection is by value pattern first and column name second, because
`SELECT email AS x` renames the output column and defeats a name rule
outright — while the value rule still fires.

| Rule | Default | |
|---|---|---|
| `pii.email` | on | |
| `pii.ssn` | on | reserved ranges excluded |
| `pii.credit_card` | on | **Luhn-checked** |
| `pii.iban` | on | **mod-97 checked** |
| `pii.phone` | on | separators required |
| `pii.name`, `pii.address`, `pii.date_of_birth` | on | column name only — no value pattern for these is honest |
| `pii.ip` | **off** | collides with version strings |

The checksums and the separator requirement are not fussiness. Without
them a naive set masks an ISO date, a build number, an IP address and a
16-digit order number as "phone", and any long digit run as a card — and
a masking feature that eats legitimate results gets switched off, after
which it protects nothing. `pii.ip` ships off because `8.5.0.1` is
simultaneously a valid address and an ordinary version string; only you
know which your data holds. Use `--mask audit` to find out what *would*
be masked before turning it on.

#### What masking is not

> lakesh masking removes recognisable PII from values **as they are
> rendered**. It defends against *incidental exposure* — an agent that
> `SELECT *`s a table and pulls a column it never needed into a model's
> context. It is **not access control**.
>
> Anything the SQL does to a value before lakesh sees it defeats it:
> `substr(email,1,5)` returns an unmatched fragment; `count(*) … WHERE
> email LIKE 'a%'` leaks through the count; `md5(ssn)` yields a stable
> re-identification key; `ORDER BY email` leaks through the ordering.
>
> If a caller **must not be able to read** a column, enforce it where the
> data lives — a Snowflake or duckicelake masking policy, or a view that
> never selects it.

lakesh cannot close those holes — masking would have to happen inside the
engine, which needs a full SQL rewriter and is impossible on the native
path where the statement is never parsed. What it does instead is make
them **visible**: when masking is active and a sensitive-looking column
appears inside a function call, a `LIKE`, an `ORDER BY` or a `GROUP BY`,
the response carries a warning saying masking may not have applied. That
is a heuristic and is trivially evaded (`SUBSTRING` for `substr`,
`email || ''`), so it warns and never refuses — refusing would block
honest queries while still missing a determined caller.

#### Your own patterns

```toml
[masking.custom."pii.employee_id"]
value    = 'EMP-[0-9]{6}'
requires = "EMP-"            # a literal the pattern cannot match without
column   = '^emp(loyee)?_(id|no)$'
```

Needs `pip install 'lakesh[mask]'`. Custom patterns are compiled with
**RE2**, not `re`: a user-supplied regex is untrusted input, and `re` can
be made to backtrack indefinitely by a pattern as short as `(a+)+$` — 26
characters of input takes 3 seconds, 32 hangs the interpreter, and a
watchdog thread cannot stop it because the matching thread holds the GIL.
RE2 compiles to a DFA and cannot backtrack at all; it runs that same
pattern against 40 characters in 0.06ms.

The price is that **RE2 has no lookaround or backreferences**, so custom
patterns cannot use them. A pattern RE2 will not compile is refused with
that as the reason — lakesh never falls back to `re`, because the
fallback would hand `re` precisely the patterns RE2 found too dangerous.
`requires` is mandatory for the same reason it is on the shipped rules.

Findings are labelled `pii.email` / `pii.phone`, the same `{namespace}.{name}`
tag shape [duckicelake](https://github.com/KellerKev/duckicelake) uses, so
an `audit` report can be fed to its object-tags endpoint unchanged.

### Credentials never reach the model

For Snowflake and Postgres profiles the connection URI *is* the
credential, and `list_profiles` output goes straight into an LLM's
context. So `list_profiles` reports a redacted URI (username and host
kept, password masked), and every tool's error payload is scrubbed of
known secret values before it leaves — driver errors like to quote the
failing statement with the DSN inline. `lakesh config show` and
`lakesh profiles show` redact the same way.

### Native passthrough for ADBC profiles

`query` and the three introspection tools send SQL to the source
directly for `adbc` profiles. Without it, `list_tables` and
`describe_table` against a remote source run past any MCP client's
timeout — the client reports `MCP error -32001: Request timed out`,
which reads like a dead server rather than a slow query. See
[Native passthrough](#native-passthrough----native) for the mechanics.

Two consequences worth knowing when reading tool output:

- Column types come back in the source's vocabulary (`TEXT`, `NUMBER`)
  rather than DuckDB's (`VARCHAR`, `BIGINT`) — which is the vocabulary
  you want when writing SQL for that source.
- `describe_table`'s `namespace` is the bare schema (`ACCOUNT_USAGE`),
  not `DATABASE.SCHEMA`; the connection is already scoped to one
  database via `adbc.snowflake.sql.db`.

`query` reports `"mode": "native" | "duckdb"` so you can confirm which
path ran, and accepts `native=false` to force DuckDB — the only way to
join a source against a local Parquet file in one statement.

### Claude Desktop config example

`~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "lakesh": {
      "command": "lakesh",
      "args": ["mcp"],
      "env": {
        "LAKESH_CONFIG": "/Users/you/.config/lakesh/config.toml"
      }
    }
  }
}
```

To enable writes (use carefully — this gives the LLM destructive
ability):

```json
"env": {
  "LAKESH_CONFIG": "/Users/you/.config/lakesh/config.toml",
  "LAKESH_MCP_WRITE": "1"
}
```

### Cline / Continue / other MCP clients

Same shape — point them at the `lakesh mcp` command. The server speaks
stdio MCP per the spec.

## REPL meta-commands

Inside the REPL, `\\`-prefixed lines don't go to SQL:

```
\?                    help
\l                    list namespaces
\d                    list tables across all namespaces
\d <ns>               list tables in one namespace
\d <ns>.<tbl>         describe a table (columns + types)
\timing [on|off]      toggle elapsed-time reporting
\format [table|json|csv]   change result format
\q                    quit
```

Terminate SQL with `;` (multi-line is fine). History persists in
`$XDG_STATE_HOME/lakesh/history`.

## Layout

```
lakesh/
├── pyproject.toml
├── README.md
├── example.config.toml
├── src/lakesh/
│   ├── config.py        # TOML loader + Profile dataclass + env indirection
│   ├── backend.py       # Session abstraction: adbc / duckdb-attached / python (PEP 249, pyiceberg)
│   ├── duck.py          # DuckDB connect + native passthrough, sandbox, deadlines, session stamp
│   ├── dialect.py       # per-engine capabilities (explain/timeout/paging/session context) as data
│   ├── attest.py        # signed session attestation (hmac / ecdsa) + Snowflake verifier SQL
│   ├── guard.py         # read-only write gate + session restriction ratchet
│   ├── mask.py          # render-time PII masking (default + custom RE2 rules)
│   ├── redact.py        # scrub secrets out of error text / logs
│   ├── freshness.py     # declared + observed table freshness / canonicality
│   ├── staging.py       # stage local files (Snowflake PUT/COPY), path allow-list
│   ├── oauth.py         # OAuth2 grants (cc / device / auth-code+PKCE) + token cache
│   ├── output.py        # rich table / json / csv formatters
│   ├── repl.py          # prompt_toolkit REPL + meta-commands
│   ├── mcp.py           # FastMCP server: query / search / list_* / describe / stage_* / session_status
│   └── cli.py           # typer entry points (exec, run, auth, stage, session, profiles)
└── tests/               # config, backend, adbc, dialect, guard, mask, redact, freshness,
    └── ...              # staging, attest, oauth, mcp, sandbox, integration (live tests auto-skip)
```

## Testing

```bash
# Unit tests only (no catalog required):
pytest tests/test_config.py

# Full suite (requires a reachable Iceberg REST catalog at $LAKESH_TEST_URI,
# default http://127.0.0.1:8181):
pytest
```

To run the integration tests against `duckicelake`, spin up its
backends + proxy in that repo first:

```bash
cd ../duckicelake
pixi run backends-up
pixi run ducklake-init
pixi run serve &
```

Then from this repo: `pytest`.

## Why not just use the DuckDB CLI?

The DuckDB shell can `ATTACH ... (TYPE ICEBERG, ...)` and run the same
SQL. What it doesn't give you:

- Connection profiles — you re-paste (or script) the ATTACH every time.
- OAuth2 token handling — you'd have to run a `curl` to mint a token and
  feed it into `CREATE SECRET (TYPE ICEBERG, TOKEN '…')` yourself.
- The known gotchas with MinIO (path-style), access-delegation-mode,
  and `allow_moved_paths` pre-configured correctly.
- One-shot scriptable queries with JSON/CSV output.
- Table / namespace autocomplete scoped to the currently-attached
  catalog.

Those are thin conveniences individually; together they make
day-to-day catalog usage meaningfully faster.

## License

MIT.
