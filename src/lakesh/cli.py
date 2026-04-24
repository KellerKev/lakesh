"""Top-level CLI. `lakesh [run]` opens the interactive REPL against a
profile; `lakesh exec -q '…'` runs one query and exits (scriptable).
Config management under `lakesh config …`, profile inspection under
`lakesh profiles …`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from . import __version__
from .config import (
    Config,
    ConfigError,
    default_config_path,
    load_config,
    write_example_config,
)
from .duck import catalog_alias, connect
from .output import render_csv, render_json, render_table


app = typer.Typer(
    help="DuckDB-powered SQL shell for Iceberg REST catalogs.",
    no_args_is_help=False,
    add_completion=False,
)
config_app = typer.Typer(help="Manage the TOML config file.")
profiles_app = typer.Typer(help="List + inspect configured profiles.")
app.add_typer(config_app, name="config")
app.add_typer(profiles_app, name="profiles")

console = Console()
err_console = Console(stderr=True)


def _load_or_die(config_path: Optional[Path]) -> Config:
    try:
        return load_config(config_path)
    except ConfigError as e:
        err_console.print(f"[red]config error:[/red] {e}")
        raise typer.Exit(code=2)


def _version_callback(value: bool):
    if value:
        console.print(f"lakesh {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def _main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True,
        help="Print version and exit.",
    ),
):
    """When invoked without a subcommand, drop straight into `run` with
    the default profile — matches what `psql`, `duckdb`, `mysql` do."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(run)


# --------------------------------------------------------------------------
# run — interactive REPL

@app.command()
def run(
    profile: Optional[str] = typer.Option(
        None, "-p", "--profile", help="Profile name (defaults to `default` in config)."
    ),
    config_path: Optional[Path] = typer.Option(
        None, "-c", "--config", help="Config TOML path (override default discovery)."
    ),
    uri: Optional[str] = typer.Option(None, help="Override the profile's `uri`."),
    warehouse: Optional[str] = typer.Option(None, help="Override the profile's `warehouse`."),
):
    """Open an interactive REPL against a profile's catalog."""
    from .repl import run_repl

    cfg = _load_or_die(config_path)
    try:
        prof = cfg.get(profile)
    except ConfigError as e:
        err_console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=2)
    if uri:
        prof.uri = uri
    if warehouse:
        prof.warehouse = warehouse
    try:
        con = connect(prof)
    except Exception as e:
        err_console.print(f"[red]connect failed:[/red] {e}")
        raise typer.Exit(code=1)
    raise typer.Exit(code=run_repl(prof, con, console))


# --------------------------------------------------------------------------
# exec — one-shot query

@app.command()
def exec(
    query: Optional[str] = typer.Option(
        None, "-q", "--query", help="SQL to execute. If omitted, read from stdin."
    ),
    profile: Optional[str] = typer.Option(None, "-p", "--profile"),
    config_path: Optional[Path] = typer.Option(None, "-c", "--config"),
    format: str = typer.Option(
        "table", "-f", "--format",
        help="Output format: table (default) | json | csv.",
    ),
    uri: Optional[str] = typer.Option(None),
    warehouse: Optional[str] = typer.Option(None),
):
    """Run a single SQL statement against a profile's catalog and exit.

    Example:
        lakesh exec -p prod -q 'SELECT COUNT(*) FROM analytics.events'
        echo 'SELECT 1' | lakesh exec -f json
    """
    if query is None:
        import sys
        query = sys.stdin.read()
    query = query.strip().rstrip(";").strip()
    if not query:
        err_console.print("[red]empty query[/red]")
        raise typer.Exit(code=2)
    if format not in {"table", "json", "csv"}:
        err_console.print(f"[red]unknown --format {format!r}[/red]")
        raise typer.Exit(code=2)

    cfg = _load_or_die(config_path)
    try:
        prof = cfg.get(profile)
    except ConfigError as e:
        err_console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=2)
    if uri:
        prof.uri = uri
    if warehouse:
        prof.warehouse = warehouse

    try:
        con = connect(prof)
    except Exception as e:
        err_console.print(f"[red]connect failed:[/red] {e}")
        raise typer.Exit(code=1)
    try:
        cur = con.execute(query)
        columns = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall()
    except Exception as e:
        err_console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1)
    finally:
        con.close()

    if not columns:
        console.print("[dim]ok[/dim]")
        return
    if format == "table":
        render_table(console, columns, rows)
        console.print(f"[dim]{len(rows)} row{'s' if len(rows) != 1 else ''}[/dim]")
    elif format == "json":
        print(render_json(columns, rows))
    else:
        print(render_csv(columns, rows), end="")


# --------------------------------------------------------------------------
# mcp — run as an MCP server on stdio

@app.command()
def mcp():
    """Run lakesh as an MCP server on stdio.

    Exposes `list_profiles`, `list_namespaces`, `list_tables`,
    `describe_table`, and `query` tools to MCP clients (Claude Desktop,
    Cline, Continue, …). Configure your client to spawn:

        lakesh mcp

    Reads + writes use the same TOML config the rest of the CLI does.
    Writes (INSERT / UPDATE / DELETE / DDL) are rejected unless the
    server is started with `LAKESH_MCP_WRITE=1` in its environment —
    keeps LLM-driven SQL safe by default.
    """
    from .mcp import serve
    serve()


# --------------------------------------------------------------------------
# doctor — test connectivity against a profile

@app.command()
def doctor(
    profile: Optional[str] = typer.Option(None, "-p", "--profile"),
    config_path: Optional[Path] = typer.Option(None, "-c", "--config"),
):
    """Verify connectivity to the catalog + backing S3.

    Runs a minimal sanity sequence: `/v1/config` REST probe → DuckDB
    ATTACH → `SELECT schema_name FROM information_schema.schemata`. Good
    first thing to run after editing config.
    """
    import httpx

    cfg = _load_or_die(config_path)
    try:
        prof = cfg.get(profile)
    except ConfigError as e:
        err_console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=2)

    ok = True

    # 1. REST `/v1/config` (iceberg-rest only; ducklake talks straight to PG)
    if prof.type == "iceberg-rest":
        try:
            r = httpx.get(f"{prof.uri.rstrip('/')}/v1/config", timeout=10.0)
            r.raise_for_status()
            console.print(f"[green]✓[/green] REST /v1/config: {r.status_code}")
        except Exception as e:
            console.print(f"[red]✗ REST /v1/config: {e}[/red]")
            ok = False

    # 2. ATTACH + namespace listing
    try:
        con = connect(prof)
        catalog = catalog_alias(prof)
        rows = con.execute(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE catalog_name = ? "
            "  AND schema_name NOT IN ('main','information_schema','pg_catalog') "
            "ORDER BY 1",
            [catalog],
        ).fetchall()
        console.print(
            f"[green]✓[/green] attach + list: {len(rows)} namespaces "
            f"{[r[0] for r in rows]}  (catalog={catalog!r})"
        )
        con.close()
    except Exception as e:
        console.print(f"[red]✗ attach + list: {e}[/red]")
        ok = False

    if not ok:
        raise typer.Exit(code=1)


# --------------------------------------------------------------------------
# config sub-commands

@config_app.command("path")
def config_path_cmd(
    config_path: Optional[Path] = typer.Option(None, "-c", "--config"),
):
    """Print the path lakesh will read from."""
    print(str(config_path or default_config_path()))


@config_app.command("init")
def config_init(
    config_path: Optional[Path] = typer.Option(None, "-c", "--config"),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing file."),
):
    """Write an example config file at the default (or explicit) path."""
    path = config_path or default_config_path()
    if path.exists() and not force:
        err_console.print(
            f"[yellow]{path} already exists — pass --force to overwrite.[/yellow]"
        )
        raise typer.Exit(code=1)
    write_example_config(path)
    console.print(f"[green]wrote example config to {path}[/green]")


@config_app.command("show")
def config_show(
    config_path: Optional[Path] = typer.Option(None, "-c", "--config"),
):
    """Dump the loaded config (secrets redacted)."""
    cfg = _load_or_die(config_path)
    console.print(f"[dim]# {cfg.source_path}[/dim]")
    console.print(f"default = [cyan]{cfg.default!r}[/cyan]")
    for name, p in cfg.profiles.items():
        console.print(f"\n[bold]{name}[/bold]  ({p.type})")
        console.print(f"  uri        = {p.uri}")
        console.print(f"  warehouse  = {p.warehouse}")
        console.print(f"  s3         = {p.s3.region}@{p.s3.endpoint or 'default'} "
                      f"(path_style={p.s3.path_style})")
        ak = "***" if p.s3.access_key else "unset"
        console.print(f"  s3.keys    = {ak}")
        if p.oauth.enabled:
            console.print(f"  oauth      = client_id={p.oauth.client_id!r}, secret=***")
        else:
            console.print(f"  oauth      = disabled")


# --------------------------------------------------------------------------
# profiles sub-commands

@profiles_app.command("list")
def profiles_list(
    config_path: Optional[Path] = typer.Option(None, "-c", "--config"),
):
    """List configured profile names."""
    cfg = _load_or_die(config_path)
    for name in sorted(cfg.profiles):
        marker = " [cyan](default)[/cyan]" if name == cfg.default else ""
        console.print(f"  {name}{marker}")


@profiles_app.command("show")
def profiles_show(
    name: str = typer.Argument(...),
    config_path: Optional[Path] = typer.Option(None, "-c", "--config"),
):
    """Dump one profile with secrets redacted."""
    cfg = _load_or_die(config_path)
    try:
        p = cfg.get(name)
    except ConfigError as e:
        err_console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=2)
    console.print(f"[bold]{p.name}[/bold]")
    console.print(f"  type       = {p.type}")
    console.print(f"  uri        = {p.uri}")
    console.print(f"  warehouse  = {p.warehouse}")
    console.print(f"  s3.endpoint= {p.s3.endpoint}")
    console.print(f"  s3.region  = {p.s3.region}")
    console.print(f"  s3.keys    = {'***' if p.s3.access_key else 'unset'}")
    console.print(f"  s3.path_style = {p.s3.path_style}")
    console.print(f"  oauth      = {'enabled' if p.oauth.enabled else 'disabled'}")


if __name__ == "__main__":
    app()
