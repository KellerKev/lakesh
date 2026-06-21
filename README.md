# lakesh

<p align="center">
  <img src="assets/lakesh-logo.svg" alt="lakesh — duck captain steering a tugboat across the duckicelake" width="640"/>
</p>

`lakesh` is a DuckDB-powered SQL shell for **Iceberg REST catalogs and
DuckLake metastores**. Profile-based connection management, an
interactive REPL with SQL autocomplete + history + `psql`-style
meta-commands, a one-shot `exec` mode for scripts, and an MCP server so
LLM agents can query your catalogs through the same plumbing.

It's a thin layer on top of DuckDB's `iceberg` and `ducklake` extensions
— DuckDB does the heavy lifting (Parquet reads, predicate pushdown,
joins); `lakesh` handles the ergonomics that the stock `duckdb` CLI
doesn't:

- Multiple catalog profiles in a TOML config, switchable via `-p <name>`.
- Two profile types: **Iceberg REST** (PyIceberg-style endpoint) or
  **DuckLake direct** (Postgres metadata + S3 data path).
- OAuth2 token fetch + reuse per session (clients don't have to see JWTs).
- S3 / MinIO credential plumbing that avoids `duckdb-iceberg`'s known
  path-style + delegation-mode footguns.
- psql-style `\\l` / `\\d` / `\\timing` / `\\format` meta-commands.
- Result formatting as rich tables, JSON (for pipes), or CSV.
- **MCP server** (`lakesh mcp`) exposing `query` / `list_namespaces` /
  `list_tables` / `describe_table` / `list_profiles` to Claude Desktop,
  Cline, Continue, etc. Read-only by default for LLM-safety.

Tested against [`duckicelake`](https://github.com/KellerKev/duckicelake);
should work against any Iceberg REST catalog (Polaris, Nessie,
Lakekeeper, managed REST, …) or any DuckLake catalog.

## Demo

REPL session against a local `duckicelake` catalog — profile switching,
`\d` / `\l` meta-commands, autocomplete, and a query through the
Iceberg REST → DuckDB iceberg-ext path.

![lakesh demo](demo_videos/lakesh-companion-demo.gif)

📥 Full quality:
[`lakesh-companion-demo.mp4`](demo_videos/lakesh-companion-demo.mp4)

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

### Secrets from env vars

Any `client_id` / `client_secret` / `access_key` / `secret_key` /
`postgres_dsn` can be sourced via a `*_env` sibling:

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
| `lakesh doctor [-p <name>]` | REST (iceberg-rest only) + ATTACH + list-namespaces smoke test |
| `lakesh mcp` | Run as an MCP server on stdio for LLM clients |
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
│   ├── duck.py          # DuckDB connect: dispatches iceberg-rest vs ducklake
│   ├── output.py        # rich table / json / csv formatters
│   ├── repl.py          # prompt_toolkit REPL + meta-commands
│   ├── mcp.py           # FastMCP server: query / list_* / describe_table tools
│   └── cli.py           # typer-based entry points
└── tests/
    ├── test_config.py        # config parsing (iceberg-rest + ducklake)
    ├── test_mcp.py           # MCP tools + read-only safety gate
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
