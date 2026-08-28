"""Top-level CLI. `lakesh [run]` opens the interactive REPL against a
profile; `lakesh exec -q '…'` runs one query and exits (scriptable).
Config management under `lakesh config …`, profile inspection under
`lakesh profiles …`.
"""
from __future__ import annotations

import sys
import time
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
from . import duck as _duck
from .duck import adbc_native_scan, catalog_alias, connect, connect_native
from .oauth import AuthRequired
from .output import render_csv, render_json, render_table
from . import dialect as _dialect
from . import guard
from . import mask as _mask
from .redact import profile_secrets, redact_option, redact_uri, scrub


app = typer.Typer(
    help="DuckDB-powered SQL shell for Iceberg REST catalogs, DuckLake, "
         "and ADBC data sources.",
    no_args_is_help=False,
    add_completion=False,
)
config_app = typer.Typer(help="Manage the TOML config file.")
profiles_app = typer.Typer(help="List + inspect configured profiles.")
auth_app = typer.Typer(help="OAuth2 login + token cache management.")
app.add_typer(config_app, name="config")
app.add_typer(profiles_app, name="profiles")
app.add_typer(auth_app, name="auth")

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
    uri: Optional[str] = typer.Option(
        None, help="Override the profile's `uri` (Iceberg REST profiles only)."
    ),
    warehouse: Optional[str] = typer.Option(
        None, help="Override the profile's `warehouse` (Iceberg REST profiles only)."
    ),
    read_only: bool = typer.Option(
        False, "--read-only",
        help="Refuse writes for this session. Cannot be undone once set — "
             "start a new session to regain write access.",
    ),
    allow_local_files: bool = typer.Option(
        False, "--allow-local-files",
        help="Keep local filesystem access in a read-only session. Off by "
             "default because read_csv('/etc/passwd') is a read, so the "
             "write gate alone does not stop file exfiltration.",
    ),
):
    """Open an interactive REPL against a profile's catalog."""
    from .repl import run_repl

    if allow_local_files:
        _duck.ALLOW_LOCAL_FILES = True
    if read_only:
        guard.SESSION.narrow("--read-only")
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
        con = connect(prof, interactive=sys.stderr.isatty())
    except AuthRequired as e:
        err_console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1)
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
    uri: Optional[str] = typer.Option(
        None, help="Override the profile's `uri` (Iceberg REST profiles only)."
    ),
    warehouse: Optional[str] = typer.Option(
        None, help="Override the profile's `warehouse` (Iceberg REST profiles only)."
    ),
    native: bool = typer.Option(
        False, "--native",
        help="ADBC profiles: send SQL straight to the source in its own "
             "dialect instead of through DuckDB's attached catalog. "
             "Needed for SHOW / QUALIFY / cross-database queries and a "
             "bare count(*), and far faster against a remote source.",
    ),
    read_only: bool = typer.Option(
        False, "--read-only",
        help="Refuse writes. Applies the stronger check that also catches "
             "a write smuggled inside a CTE or after a semicolon.",
    ),
    allow_local_files: bool = typer.Option(
        False, "--allow-local-files",
        help="Keep local filesystem access in a read-only session. Off by "
             "default because read_csv('/etc/passwd') is a read, so the "
             "write gate alone does not stop file exfiltration.",
    ),
    mask: Optional[str] = typer.Option(
        None, "--mask",
        help="Mask recognisable PII in the results: `mask` to replace it, "
             "`audit` to report what would be masked without masking it. "
             "Applies at render time — it is not access control.",
    ),
):
    """Run a single SQL statement against a profile's catalog and exit.

    Example:
        lakesh exec -p prod -q 'SELECT COUNT(*) FROM analytics.events'
        echo 'SELECT 1' | lakesh exec -f json
        lakesh exec -p snowflake --native -q 'SHOW DATABASES'
    """
    if query is None:
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

    if allow_local_files:
        _duck.ALLOW_LOCAL_FILES = True
    if read_only:
        guard.SESSION.narrow("--read-only")
    guard.set_read_procedures(_dialect.read_procedures_for(prof))
    # Default-open: with no flag, no env var and no profile key this is
    # `None` and `lakesh exec -q 'INSERT ...'` behaves exactly as it always
    # has. The stronger scan runs only when a restriction is in force.
    restriction = guard.SESSION.effective(prof)
    if restriction.read_only:
        blocked = guard.blocks_write(query)
        if blocked:
            payload = guard.refusal(restriction, blocked)
            err_console.print(f"[red]{payload['error']}[/red]")
            raise typer.Exit(code=2)
        err_console.print(f"[dim]{restriction.describe()}[/dim]")

    if native and prof.type != "adbc":
        err_console.print(
            f"[red]--native requires an adbc profile "
            f"(profile {prof.name!r} is {prof.type!r})[/red]"
        )
        raise typer.Exit(code=2)

    # A driver error will happily quote the failing statement with the
    # DSN inline, so everything printed from here on gets scrubbed.
    secrets = profile_secrets(prof)

    handle: Optional[int] = None
    try:
        if native:
            con, handle = connect_native(prof, interactive=sys.stderr.isatty())
        else:
            con = connect(prof, interactive=sys.stderr.isatty())
    except AuthRequired as e:
        err_console.print(f"[red]{scrub(str(e), secrets)}[/red]")
        raise typer.Exit(code=1)
    except Exception as e:
        err_console.print(f"[red]connect failed:[/red] {scrub(str(e), secrets)}")
        raise typer.Exit(code=1)
    policy = _mask.resolve(cfg, prof, requested=mask)
    try:
        cur = adbc_native_scan(con, handle, query) if native else con.execute(query)
        columns = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall()
        rows, mask_report = _mask.mask_rows(policy, columns, rows)
    except Exception as e:
        err_console.print(f"[red]{scrub(str(e), secrets)}[/red]")
        hint = _duck.explain_sandbox_error(e)
        if hint:
            err_console.print(f"[yellow]hint:[/yellow] {hint}")
        raise typer.Exit(code=1)
    finally:
        con.close()

    if not columns:
        console.print("[dim]ok[/dim]")
        return
    if policy.active:
        found = ", ".join(
            f"{label} ({v['cells']} cells)" for label, v in mask_report.findings.items()
        ) or "nothing"
        err_console.print(f"[dim]masking {policy.mode}: {found}[/dim]")
        for warning in _mask.detect_defeats(policy, query):
            err_console.print(f"[yellow]warning:[/yellow] {warning}")
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
def mcp(
    config_path: Optional[Path] = typer.Option(None, "-c", "--config"),
    read_only: bool = typer.Option(
        False, "--read-only",
        help="Refuse writes for every call this server serves. Operator "
             "policy: a caller cannot relax it.",
    ),
    allow_local_files: bool = typer.Option(
        False, "--allow-local-files",
        help="Keep local filesystem access in a read-only session. Off by "
             "default because read_csv('/etc/passwd') is a read, so the "
             "write gate alone does not stop file exfiltration.",
    ),
):
    """Run lakesh as an MCP server on stdio.

    Exposes `list_profiles`, `list_namespaces`, `list_tables`,
    `describe_table`, and `query` tools to MCP clients (Claude Desktop,
    Cline, Continue, …). Configure your client to spawn:

        lakesh mcp

    Reads + writes use the same TOML config the rest of the CLI does;
    `-c` (or `$LAKESH_CONFIG`) points it somewhere else. Writes (INSERT
    / UPDATE / DELETE / DDL) are rejected unless the server is started
    with `LAKESH_MCP_WRITE=1` in its environment, and a profile marked
    `read_only` refuses them regardless — keeps LLM-driven SQL safe by
    default.
    """
    from .mcp import serve
    if allow_local_files:
        _duck.ALLOW_LOCAL_FILES = True
    serve(config_path, read_only=read_only)


# --------------------------------------------------------------------------
# doctor — test connectivity against a profile

def _adbc_hints(prof: Config | object, message: str) -> list[str]:
    """Turn an opaque driver failure into something actionable.

    The Snowflake driver reports auth problems as bare numeric codes,
    and each one points at a different half of the DSN/options split
    that its config shape requires (credentials in the DSN, account in
    the options — see examples/config.snowflake-adbc.toml).
    """
    if getattr(prof, "type", None) != "adbc":
        return []
    msg = message.lower()
    hints: list[str] = []
    if "driver" in msg or "manifest" in msg or "no such file" in msg:
        hints.append(
            f"the ADBC driver {getattr(prof, 'driver', '')!r} may not be "
            f"installed. Most drivers ship as a Python package that carries "
            f"the shared library — e.g. `pip install adbc-driver-snowflake` "
            f"or `pip install adbc-driver-postgresql` — and `driver` can "
            f"point straight at that libadbc_driver_*.so. `dbc install "
            f"<name>` works too if you have the driver manager CLI."
        )
    if "260000" in msg:
        hints.append(
            "Snowflake 260000 (account is empty): the account is read only "
            'from the `adbc.snowflake.sql.account` option, never from the '
            "DSN. Set it under [profiles.<name>.options]."
        )
    if "260001" in msg or "260002" in msg:
        hints.append(
            "Snowflake 260001/260002 (user/password is empty): the driver "
            "parses the ATTACH path as a gosnowflake DSN and that parse "
            "overwrites user and password, so `username`/`password` options "
            "are discarded. Put them in the URI as USER:PAT@ACCOUNT — and "
            "keep it path-free, since a trailing /DB/SCHEMA breaks account "
            "parsing."
        )
    return hints


@app.command()
def doctor(
    profile: Optional[str] = typer.Option(None, "-p", "--profile"),
    config_path: Optional[Path] = typer.Option(None, "-c", "--config"),
):
    """Verify connectivity to the catalog + backing S3.

    Runs a minimal sanity sequence: `/v1/config` REST probe for
    iceberg-rest profiles, then DuckDB ATTACH and
    `SELECT schema_name FROM information_schema.schemata`. Good first
    thing to run after editing config.
    """
    import httpx

    cfg = _load_or_die(config_path)
    try:
        prof = cfg.get(profile)
    except ConfigError as e:
        err_console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=2)

    ok = True
    secrets = profile_secrets(prof)

    # 1. REST `/v1/config` (iceberg-rest only; ducklake talks straight to PG)
    if prof.type == "iceberg-rest":
        try:
            r = httpx.get(f"{prof.uri.rstrip('/')}/v1/config", timeout=10.0)
            r.raise_for_status()
            console.print(f"[green]✓[/green] REST /v1/config: {r.status_code}")
        except Exception as e:
            console.print(f"[red]✗ REST /v1/config: {e}[/red]")
            ok = False

    # 2. OAuth token acquisition (any oauth-enabled profile)
    if prof.oauth.enabled and prof.type != "ducklake":
        from .oauth import get_token
        try:
            tok = get_token(prof, interactive=sys.stderr.isatty())
            console.print(
                f"[green]✓[/green] auth: {prof.oauth.grant} token acquired"
                if tok else "[yellow]– auth: no token endpoint resolvable[/yellow]"
            )
        except AuthRequired as e:
            console.print(f"[red]✗ auth: {e}[/red]")
            ok = False
        except Exception as e:
            console.print(f"[red]✗ auth ({prof.oauth.grant}): {e}[/red]")
            ok = False

    # 3. adbc_scanner extension + driver (adbc profiles)
    if prof.type == "adbc":
        import duckdb as _duckdb
        from .duck import load_adbc_scanner
        try:
            probe = _duckdb.connect(":memory:")
            load_adbc_scanner(probe, required=True)
            probe.close()
            console.print("[green]✓[/green] adbc_scanner extension installed + loaded")
        except Exception as e:
            console.print(
                f"[red]✗ adbc_scanner extension: {e}[/red]\n"
                f"  (community extension — needs network on first install, "
                f"DuckDB 1.4+/1.5, and a supported platform)"
            )
            ok = False

    # 4. ATTACH + namespace listing
    try:
        con = connect(prof, interactive=sys.stderr.isatty())
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
        text = scrub(str(e), secrets)
        console.print(f"[red]✗ attach + list: {text}[/red]")
        for hint in _adbc_hints(prof, text):
            console.print(f"  [yellow]hint:[/yellow] {hint}")
        ok = False

    if not ok:
        raise typer.Exit(code=1)


# --------------------------------------------------------------------------
# auth sub-commands

@auth_app.command("login")
def auth_login(
    profile: Optional[str] = typer.Option(None, "-p", "--profile"),
    config_path: Optional[Path] = typer.Option(None, "-c", "--config"),
    force: bool = typer.Option(
        False, "--force", help="Discard any cached token and re-run the flow."
    ),
):
    """Run the profile's OAuth2 flow and cache the resulting token."""
    from .oauth import TokenCache, get_token

    cfg = _load_or_die(config_path)
    try:
        prof = cfg.get(profile)
    except ConfigError as e:
        err_console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=2)
    if not prof.oauth.enabled or prof.type == "ducklake":
        err_console.print(
            f"[yellow]profile {prof.name!r} has no OAuth configured[/yellow]"
        )
        raise typer.Exit(code=2)
    try:
        tok = get_token(prof, interactive=True, force=force)
    except Exception as e:
        err_console.print(f"[red]login failed:[/red] {e}")
        raise typer.Exit(code=1)
    if not tok:
        err_console.print("[red]no token acquired[/red]")
        raise typer.Exit(code=1)
    cache = TokenCache()
    entry = cache.status().get(prof.name) or {}
    token_meta = entry.get("token") or {}
    expires_at = token_meta.get("expires_at")
    exp = (
        f"expires in {int((expires_at - time.time()) / 60)}m"
        if expires_at else "no expiry reported"
    )
    refresh = "yes" if token_meta.get("refresh_token") else "no"
    console.print(
        f"[green]✓[/green] logged in to [bold]{prof.name}[/bold] "
        f"({prof.oauth.grant}; {exp}; refresh token: {refresh})"
    )


@auth_app.command("status")
def auth_status(
    config_path: Optional[Path] = typer.Option(None, "-c", "--config"),
):
    """Show cached-token state for every OAuth-enabled profile."""
    from .oauth import TokenCache

    cfg = _load_or_die(config_path)
    cache = TokenCache()
    entries = cache.status()
    console.print(f"[dim]# token cache: {cache.path}[/dim]")
    any_oauth = False
    for name in sorted(cfg.profiles):
        p = cfg.profiles[name]
        if not p.oauth.enabled or p.type == "ducklake":
            continue
        any_oauth = True
        entry = entries.get(name)
        if not entry:
            state = "[yellow]not logged in[/yellow]"
        else:
            token_meta = entry.get("token") or {}
            expires_at = token_meta.get("expires_at")
            if expires_at is None:
                state = "[green]cached[/green] (no expiry reported)"
            elif expires_at > time.time():
                state = f"[green]cached[/green] (expires in {int((expires_at - time.time()) / 60)}m)"
            else:
                state = "[yellow]expired[/yellow]"
            if token_meta.get("refresh_token"):
                state += " +refresh"
        console.print(f"  {name}  [dim]{p.oauth.grant}[/dim]  {state}")
    if not any_oauth:
        console.print("  [dim]no OAuth-enabled profiles configured[/dim]")


@auth_app.command("logout")
def auth_logout(
    profile: Optional[str] = typer.Option(None, "-p", "--profile"),
    all_profiles: bool = typer.Option(False, "--all", help="Clear every cached token."),
):
    """Drop cached tokens (one profile with -p, or --all)."""
    from .oauth import TokenCache

    if not profile and not all_profiles:
        err_console.print("[red]pass -p <profile> or --all[/red]")
        raise typer.Exit(code=2)
    cache = TokenCache()
    cache.clear(None if all_profiles else profile)
    console.print(
        "[green]✓[/green] cleared all cached tokens"
        if all_profiles else f"[green]✓[/green] cleared cached token for {profile!r}"
    )


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
        if p.type == "adbc":
            console.print(f"  driver     = {p.driver}")
            console.print(f"  uri        = {redact_uri(p.uri) or '(options-configured)'}")
            console.print(f"  catalog    = {p.catalog}  (read_only={p.read_only})")
            for k, v in p.options.items():
                console.print(f"  options.{k} = {redact_option(k, v)}")
        else:
            console.print(f"  uri        = {redact_uri(p.uri)}")
            console.print(f"  warehouse  = {p.warehouse}")
            console.print(f"  s3         = {p.s3.region}@{p.s3.endpoint or 'default'} "
                          f"(path_style={p.s3.path_style})")
            ak = "***" if p.s3.access_key else "unset"
            console.print(f"  s3.keys    = {ak}")
        if p.oauth.enabled:
            console.print(
                f"  oauth      = {p.oauth.grant} (client_id={p.oauth.client_id!r}"
                + (f", endpoint={p.oauth.token_endpoint}" if p.oauth.token_endpoint else "")
                + ")"
            )
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
    if p.type == "adbc":
        console.print(f"  driver     = {p.driver}")
        console.print(f"  uri        = {redact_uri(p.uri) or '(options-configured)'}")
        console.print(f"  catalog    = {p.catalog}")
        console.print(f"  read_only  = {p.read_only}")
        for k, v in p.options.items():
            console.print(f"  options.{k} = {redact_option(k, v)}")
    else:
        console.print(f"  uri        = {redact_uri(p.uri)}")
        console.print(f"  warehouse  = {p.warehouse}")
        console.print(f"  s3.endpoint= {p.s3.endpoint}")
        console.print(f"  s3.region  = {p.s3.region}")
        console.print(f"  s3.keys    = {'***' if p.s3.access_key else 'unset'}")
        console.print(f"  s3.path_style = {p.s3.path_style}")
    console.print(
        f"  oauth      = {p.oauth.grant if p.oauth.enabled else 'disabled'}"
    )


if __name__ == "__main__":
    app()
