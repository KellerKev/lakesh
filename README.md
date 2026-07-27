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

- Upstream `adbc_scanner` bug: `GROUP BY` / `DISTINCT` on **short
  (≤12-byte) VARCHAR columns** read through an attached catalog can
  return corrupted group keys. Longer strings, joins, filters, plain
  scans, and the raw `adbc_scan()` function are unaffected. Workaround
  until fixed upstream: force a copy, e.g. `GROUP BY kind || ''`.

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
| `list_namespaces(profile=None)` | List schemas in a profile's catalog |
| `list_tables(profile=None, namespace=None)` | List tables, optionally scoped |
| `describe_table(namespace, table, profile=None)` | Columns + types + nullability |
| `query(sql, profile=None, limit=1000, format="json")` | Run SQL and return results |

### Read-only by default

`query` rejects anything that doesn't start with
`SELECT` / `SHOW` / `DESCRIBE` / `WITH` / `EXPLAIN` / `PRAGMA` / `VALUES`.
Set `LAKESH_MCP_WRITE=1` in the server's environment to enable
INSERT / UPDATE / DELETE / DDL / `CALL ducklake_…` procedures. Keeps
LLM-driven SQL safe by default.

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
