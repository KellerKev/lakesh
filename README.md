# lakesh

<p align="center">
  <img src="assets/lakesh-logo.svg" alt="lakesh — duck captain steering a tugboat across the duckicelake" width="640"/>
</p>

`lakesh` is a DuckDB-powered SQL shell for **Iceberg REST catalogs,
DuckLake metastores, and any database with an ADBC driver**.
Profile-based connection management, an interactive REPL with SQL
autocomplete + history + `psql`-style meta-commands, a one-shot `exec`
mode for scripts, and an MCP server so LLM agents can query your
catalogs through the same plumbing.

It's a thin layer on top of DuckDB's `iceberg`, `ducklake`, and
`adbc_scanner` extensions — DuckDB does the heavy lifting (Parquet
reads, predicate pushdown, joins); `lakesh` handles the ergonomics that
the stock `duckdb` CLI doesn't:

- Multiple catalog profiles in a TOML config, switchable via `-p <name>`.
- Three profile types: **Iceberg REST** (PyIceberg-style endpoint),
  **DuckLake direct** (Postgres metadata + S3 data path), or **ADBC**
  (Postgres, MySQL, Snowflake, BigQuery, SQL Server, Trino, Flight SQL,
  SQLite, … via [ADBC drivers](https://arrow.apache.org/adbc/)).
- Native OAuth2 per data source: **client-credentials, device-code, and
  authorization-code (PKCE)** grants, with token caching + refresh
  (`lakesh auth login/status/logout`).
- S3 / MinIO credential plumbing that avoids `duckdb-iceberg`'s known
  path-style + delegation-mode footguns.
- psql-style `\\l` / `\\d` / `\\timing` / `\\format` meta-commands.
- Result formatting as rich tables, JSON (for pipes), or CSV.
- **MCP server** (`lakesh mcp`) exposing `query` / `list_namespaces` /
  `list_tables` / `describe_table` / `list_profiles` to Claude Desktop,
  Cline, Continue, etc. Read-only by default for LLM-safety.

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
[profiles.snow.oauth]
grant                         = "device_code"
client_id                     = "lakesh-cli"
device_authorization_endpoint = "https://idp.example.com/oauth2/v1/device/authorize"
token_endpoint                = "https://idp.example.com/oauth2/v1/token"
scope                         = "session:role:ANALYST offline_access"
```

Tokens (access + refresh + expiry) are cached in
`$XDG_STATE_HOME/lakesh/tokens.json` (file mode 0600, same trust level
as config-file secrets) and refreshed automatically. Interactive grants
only prompt when nothing cached is usable:

```bash
lakesh auth login -p snow      # run the flow, cache the token
lakesh auth status             # per-profile cache state
lakesh auth logout -p snow     # drop one profile's token (--all for everything)
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
[profiles.snowflake]
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
[profiles.snow]
status        = "canonical"   # profile-wide default
max_staleness = "24h"

[profiles.snow.tables]
"ANALYTICS.FCT_REVENUE"    = { status = "canonical", max_staleness = "6h", note = "billing source of truth" }
"ANALYTICS.FCT_REVENUE_V1" = { status = "deprecated", superseded_by = "ANALYTICS.FCT_REVENUE" }
"STAGING.*"                = { status = "deprecated", note = "raw landing zone; do not query" }
```

Keys **must be quoted** — an unquoted `ANALYTICS.FCT_REVENUE` is TOML
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
│   ├── duck.py          # DuckDB connect: iceberg-rest / ducklake / adbc
│   ├── oauth.py         # OAuth2 grants (cc / device / auth-code+PKCE) + token cache
│   ├── output.py        # rich table / json / csv formatters
│   ├── repl.py          # prompt_toolkit REPL + meta-commands
│   ├── mcp.py           # FastMCP server: query / list_* / describe_table tools
│   └── cli.py           # typer-based entry points (incl. `lakesh auth …`)
└── tests/
    ├── test_config.py        # config parsing (iceberg-rest + ducklake + adbc + oauth)
    ├── test_oauth.py         # all three grants + refresh + token cache (mocked HTTP)
    ├── test_adbc.py          # adbc SQL builders + live sqlite e2e (auto-skips)
    ├── test_mcp.py           # MCP tools + read-only safety gate + AuthRequired
    └── test_integration.py   # live query against a running catalog (auto-skips)
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
