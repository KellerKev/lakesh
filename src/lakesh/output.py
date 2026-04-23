"""Formatting DuckDB query results. `rich` tables by default; JSON / CSV
for scripting; `null` when we only care about side-effects (DDL).
"""
from __future__ import annotations

import csv
import io
import json
from typing import Any, Iterable

from rich.console import Console
from rich.table import Table


def _stringify(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    return str(v)


def render_table(
    console: Console, columns: list[str], rows: Iterable[tuple],
    *, max_rows: int | None = None, title: str | None = None,
) -> int:
    """Print results as a rich table. Returns the row count printed."""
    t = Table(show_header=True, header_style="bold cyan", title=title,
              title_justify="left")
    for c in columns:
        t.add_column(c, overflow="fold")
    n = 0
    for row in rows:
        if max_rows is not None and n >= max_rows:
            break
        t.add_row(*(_stringify(v) for v in row))
        n += 1
    console.print(t)
    return n


def render_json(columns: list[str], rows: Iterable[tuple]) -> str:
    """Array-of-objects JSON — handy for shell pipelines."""
    return json.dumps(
        [dict(zip(columns, (_coerce_jsonable(v) for v in row))) for row in rows],
        default=str,
        indent=2,
    )


def render_csv(columns: list[str], rows: Iterable[tuple]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(columns)
    for row in rows:
        w.writerow([_stringify(v) for v in row])
    return buf.getvalue()


def _coerce_jsonable(v: Any) -> Any:
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    # Decimal, date, datetime etc. fall through to `default=str`.
    return v
