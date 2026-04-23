"""Interactive REPL. Multi-line SQL (terminate with `;`), persistent history,
tab-completion over catalogs / schemas / tables, `\\`-prefixed meta-commands
similar to psql.
"""
from __future__ import annotations

import os
from pathlib import Path

import duckdb
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory
from prompt_toolkit.lexers import PygmentsLexer
from prompt_toolkit.styles import Style
from pygments.lexers.sql import SqlLexer
from rich.console import Console

from .config import Profile
from .duck import catalog_alias
from .output import render_table


_META_HELP = """\
Meta-commands (psql-like):
  \\q              quit
  \\?              this help
  \\l              list namespaces (schemas) in the attached catalog
  \\d              list tables in the attached catalog
  \\d <ns>         list tables in one namespace
  \\d <ns>.<tbl>   describe a table (columns + types)
  \\timing [on|off]  toggle / show per-query elapsed-time reporting
  \\format [table|json|csv]  change result format
  \\e              edit the last query in $EDITOR (not yet implemented)
"""


def _history_path() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    p = base / "lakesh" / "history"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _list_ns(con: duckdb.DuckDBPyConnection, catalog: str) -> list[str]:
    try:
        rows = con.execute(
            "SELECT DISTINCT schema_name FROM information_schema.schemata "
            "WHERE catalog_name = ? "
            "  AND schema_name NOT IN ('main','information_schema','pg_catalog') "
            "ORDER BY 1",
            [catalog],
        ).fetchall()
        return [r[0] for r in rows]
    except duckdb.Error:
        return []


def _list_tables(
    con: duckdb.DuckDBPyConnection, catalog: str, ns: str | None = None,
) -> list[tuple[str, str]]:
    q = ("SELECT table_schema, table_name FROM information_schema.tables "
         "WHERE table_catalog = ? "
         "  AND table_schema NOT IN ('main','information_schema','pg_catalog')")
    params: list = [catalog]
    if ns:
        q += " AND table_schema = ?"
        params.append(ns)
    q += " ORDER BY table_schema, table_name"
    try:
        return [tuple(r) for r in con.execute(q, params).fetchall()]
    except duckdb.Error:
        return []


def _build_completer(con: duckdb.DuckDBPyConnection, catalog: str) -> WordCompleter:
    keywords = [
        "SELECT", "FROM", "WHERE", "GROUP BY", "ORDER BY", "LIMIT",
        "JOIN", "LEFT JOIN", "RIGHT JOIN", "INNER JOIN", "ON", "AS",
        "WITH", "UNION", "INSERT INTO", "VALUES", "UPDATE", "DELETE",
        "CREATE TABLE", "DROP TABLE", "ALTER TABLE", "DESCRIBE", "SHOW",
    ]
    names: list[str] = []
    for ns in _list_ns(con, catalog):
        names.append(ns)
        for _, tbl in _list_tables(con, catalog, ns):
            names.append(f"{ns}.{tbl}")
            names.append(f"{catalog}.{ns}.{tbl}")
    return WordCompleter(keywords + names, ignore_case=True, sentence=False)


def _describe(
    con: duckdb.DuckDBPyConnection, catalog: str, ns: str, tbl: str, console: Console,
) -> None:
    try:
        rows = con.execute(
            "SELECT column_name, data_type, is_nullable, ordinal_position "
            "FROM information_schema.columns "
            "WHERE table_catalog = ? AND table_schema=? AND table_name=? "
            "ORDER BY ordinal_position",
            [catalog, ns, tbl],
        ).fetchall()
    except duckdb.Error as e:
        console.print(f"[red]describe failed: {e}[/red]")
        return
    if not rows:
        console.print(f"[yellow]no such table: {ns}.{tbl}[/yellow]")
        return
    render_table(console, ["column", "type", "nullable", "#"], rows,
                 title=f"{ns}.{tbl}")


def _handle_meta(
    con: duckdb.DuckDBPyConnection, catalog: str,
    console: Console, cmd: str, state: dict,
) -> bool:
    """Returns True if we should continue the REPL, False to quit."""
    parts = cmd.strip().split()
    head = parts[0]
    args = parts[1:]
    if head in (r"\q", r"\quit", r"\exit"):
        return False
    if head in (r"\?", r"\h", r"\help"):
        console.print(_META_HELP)
    elif head == r"\l":
        ns = _list_ns(con, catalog)
        render_table(console, ["namespace"], [(n,) for n in ns],
                     title="namespaces")
    elif head == r"\d":
        if not args:
            rows = _list_tables(con, catalog)
            render_table(console, ["namespace", "table"], rows,
                         title="tables")
        elif "." in args[0]:
            ns, tbl = args[0].split(".", 1)
            _describe(con, catalog, ns, tbl, console)
        else:
            rows = _list_tables(con, catalog, args[0])
            render_table(console, ["namespace", "table"], rows,
                         title=f"tables in {args[0]}")
    elif head == r"\timing":
        if not args:
            console.print(f"timing: [cyan]{'on' if state['timing'] else 'off'}[/cyan]")
        else:
            state["timing"] = args[0].lower() in ("on", "1", "true", "yes")
            console.print(f"timing → [cyan]{'on' if state['timing'] else 'off'}[/cyan]")
    elif head == r"\format":
        if not args:
            console.print(f"format: [cyan]{state['format']}[/cyan]")
        elif args[0] in {"table", "json", "csv"}:
            state["format"] = args[0]
            console.print(f"format → [cyan]{state['format']}[/cyan]")
        else:
            console.print(f"[red]unknown format {args[0]!r}[/red]")
    else:
        console.print(f"[red]unknown meta-command {head!r} (try \\?)[/red]")
    return True


def run_repl(profile: Profile, con: duckdb.DuckDBPyConnection, console: Console) -> int:
    """Interactive loop. Returns a process exit code."""
    import time
    catalog = catalog_alias(profile)
    banner_tail = (
        f"[cyan]{profile.uri}[/cyan], warehouse [cyan]{profile.warehouse}[/cyan]"
        if profile.type == "iceberg-rest"
        else f"DuckLake @ [cyan]{profile.data_path}[/cyan] "
             f"(catalog [cyan]{catalog}[/cyan])"
    )
    console.print(
        f"[bold green]lakesh[/bold green] connected to [bold]{profile.name}[/bold] "
        f"({banner_tail})"
    )
    console.print("Type SQL terminated with `;`, or \\? for help. \\q to quit.\n")

    session = PromptSession(
        history=FileHistory(str(_history_path())),
        lexer=PygmentsLexer(SqlLexer),
        completer=_build_completer(con, catalog),
        multiline=True,
        style=Style.from_dict({"prompt": "ansigreen"}),
    )
    state = {"timing": True, "format": "table"}
    try:
        while True:
            try:
                raw = session.prompt(f"{profile.name}> ")
            except (KeyboardInterrupt, EOFError):
                break
            text = raw.strip().rstrip(";").strip()
            if not text:
                continue
            if text.startswith("\\"):
                if not _handle_meta(con, catalog, console, text, state):
                    break
                continue
            t0 = time.perf_counter()
            try:
                cur = con.execute(text)
                columns = [d[0] for d in cur.description] if cur.description else []
                rows = cur.fetchall()
            except duckdb.Error as e:
                console.print(f"[red]{e}[/red]")
                continue
            elapsed = time.perf_counter() - t0
            if not columns:
                console.print(f"[dim]ok[/dim]")
            else:
                if state["format"] == "table":
                    n = render_table(console, columns, rows)
                elif state["format"] == "json":
                    from .output import render_json
                    console.print(render_json(columns, rows))
                    n = len(rows)
                else:
                    from .output import render_csv
                    console.print(render_csv(columns, rows))
                    n = len(rows)
                console.print(f"[dim]{n} row{'s' if n != 1 else ''}[/dim]")
            if state["timing"]:
                console.print(f"[dim]time: {elapsed*1000:.1f} ms[/dim]")
    finally:
        con.close()
    return 0
