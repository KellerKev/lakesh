# lakesh

`lakesh` is a DuckDB-powered SQL shell for **Iceberg REST catalogs**.
Think `snow`-cli but for the lakehouse: profile-based connection
management, an interactive REPL with SQL autocomplete + history, and a
one-shot `exec` mode for scripts.

It's a thin layer on top of DuckDB's `iceberg` extension — DuckDB does
the heavy lifting (Parquet reads, predicate pushdown, joins); `lakesh`
handles the ergonomics that the stock `duckdb` CLI doesn't:

- Multiple catalog profiles in a TOML config, switchable via `-p <name>`.
- OAuth2 token fetch + reuse per session (clients don't have to see JWTs).
- S3 / MinIO credential plumbing that avoids `duckdb-iceberg`'s known
  path-style + delegation-mode footguns.
- psql-style `\\l` / `\\d` / `\\timing` / `\\format` meta-commands.
- Result formatting as rich tables, JSON (for pipes), or CSV.

Tested against [`duckicelake`](https://github.com/KellerKev/duckicelake);
should work against any Iceberg REST catalog (Polaris, Nessie,
Lakekeeper, managed REST, …).

## Install

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

Each profile declares an Iceberg REST catalog + its backing S3:

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

**Secrets from env vars** — any `client_id` / `client_secret` /
`access_key` / `secret_key` can be sourced via a `*_env` sibling:

```toml
[profiles.prod.oauth]
client_id_env     = "LAKESH_PROD_CLIENT_ID"
client_secret_env = "LAKESH_PROD_CLIENT_SECRET"
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
| `lakesh doctor [-p <name>]` | REST + ATTACH + list-namespaces smoke test |
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
| `--uri <url>` | Override profile's `uri` (handy for one-off tests) |
| `--warehouse <name>` | Override profile's `warehouse` |

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
│   ├── config.py        # TOML loader + profile dataclass + env indirection
│   ├── duck.py          # DuckDB connect + iceberg ATTACH + OAuth token fetch
│   ├── output.py        # rich table / json / csv formatters
│   ├── repl.py          # prompt_toolkit REPL + meta-commands
│   └── cli.py           # typer-based entry points
└── tests/
    ├── test_config.py        # config parsing (no network)
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
