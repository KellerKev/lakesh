"""Getting a local file to where the source engine can read it.

The generic capability is "stage a file"; a Snowflake internal stage is
one implementation, an S3 prefix for DuckLake/Iceberg would be another.
Which engines can do it lives in `dialect.StageOps`; this module owns the
part that is the same everywhere — deciding whether lakesh is willing to
read the local file at all.

### Why the path check lives here and is not optional

This is the first caller-supplied filesystem path lakesh has ever
accepted. Everything else — `--config`, the driver `.so`, a DuckLake
`data_path` — comes from an operator's config, not from whoever is
driving the tool. An upload takes a path from a CLI user or, over MCP,
from a model, and sends the bytes to a remote account. That is an
exfiltration primitive if it is not fenced.

**It cannot lean on the filesystem sandbox.** A read-only session sets
`disabled_filesystems='LocalFileSystem'`, but that binds DuckDB's engine,
not the process: measured, with the sandbox active and DuckDB's own
`read_text('/etc/hosts')` refused, a `PUT` still reached the driver and
opened the local path. The ADBC driver is `dlopen`ed outside DuckDB's
filesystem layer. So the fence has to be here, and it applies whatever
the session's read-only state is — the allow-list is about *where the
file comes from*, which is a separate question from whether the session
may write.

### The rule that matters

Resolve first, then check containment. Checking a path and resolving it
afterwards is the classic bypass: `~/allowed/link-to-etc/passwd` passes a
prefix test on the unresolved string and reads `/etc/passwd`.
"""
from __future__ import annotations

import stat
from pathlib import Path

from .config import Profile

# Generous enough for a real export, small enough that an agent cannot
# stage a 40GB file by accident.
DEFAULT_MAX_UPLOAD_BYTES = 100 * 1024 * 1024


class StagingError(Exception):
    """A local file lakesh will not upload, with the reason."""


def upload_roots(profile: Profile) -> list[Path]:
    """The directories this profile may upload from, resolved.

    Empty means uploads are refused. There is deliberately no default:
    an unconfigured allow-list means the feature is off, not that
    everything is permitted, and a process's working directory is not a
    security boundary.
    """
    roots = []
    for raw in getattr(profile, "upload_roots", ()) or ():
        try:
            roots.append(Path(raw).expanduser().resolve())
        except OSError:
            continue          # an unresolvable root simply allows nothing
    return roots


def max_upload_bytes(profile: Profile) -> int:
    configured = getattr(profile, "max_upload_bytes", 0) or 0
    return int(configured) if configured > 0 else DEFAULT_MAX_UPLOAD_BYTES


def resolve_upload_path(profile: Profile, raw: str) -> Path:
    """The real path for `raw`, or raise `StagingError` saying why not.

    Every check fails closed, and the error names the reason rather than
    a generic refusal — an operator who has to guess why their upload was
    rejected will turn the allow-list off.
    """
    roots = upload_roots(profile)
    if not roots:
        raise StagingError(
            f"profile {profile.name!r} has no `upload_roots` configured, so "
            f"uploads are refused. Name the directories you are willing to "
            f"upload from, e.g. upload_roots = [\"~/data/exports\"]."
        )

    try:
        # Resolve BEFORE any containment check: a symlink inside an
        # allowed root pointing outside it must not pass a prefix test on
        # the unresolved string.
        path = Path(raw).expanduser().resolve(strict=True)
    except FileNotFoundError:
        raise StagingError(f"no such file: {raw}") from None
    except OSError as e:
        raise StagingError(f"cannot read {raw}: {e}") from None

    if not any(_within(path, root) for root in roots):
        allowed = ", ".join(str(r) for r in roots)
        raise StagingError(
            f"{path} is outside this profile's upload_roots ({allowed}). "
            f"If the path looked allowed, note that symlinks are resolved "
            f"before the check."
        )

    mode = path.lstat().st_mode
    if not stat.S_ISREG(mode):
        raise StagingError(
            f"{path} is not a regular file — directories, FIFOs and device "
            f"nodes are refused."
        )

    size = path.stat().st_size
    cap = max_upload_bytes(profile)
    if size > cap:
        raise StagingError(
            f"{path} is {size} bytes, over this profile's "
            f"max_upload_bytes ({cap})."
        )
    return path


def _within(path: Path, root: Path) -> bool:
    try:
        return path.is_relative_to(root)
    except (AttributeError, ValueError):        # pragma: no cover
        return False


# --------------------------------------------------------------------------
# the operations themselves
#
# One implementation shared by the CLI and the MCP tools, so the two
# cannot drift on the part that matters — the path check and the
# post-upload verification.


def _run(profile, sql: str):
    """Execute a stage statement against the source and return its rows.

    Routed by statement kind: `LIST` is a read and returns rows, while
    `PUT`, `REMOVE` and `COPY INTO` are writes and run exactly once,
    returning nothing. That loses COPY's per-file report, which was never
    load-bearing here — the row-count delta is the signal `load` trusts,
    for the same reason `upload` verifies by listing.
    """
    from .backend import open_session

    session = open_session(profile, interactive=False, native=True)
    try:
        return session.run(sql)
    finally:
        session.close()


def _ops_or_raise(profile: Profile):
    from . import dialect as _dialect

    ops = _dialect.stage_ops(profile)
    if ops is None:
        raise StagingError(
            f"profile {profile.name!r} ({_dialect.for_profile(profile).name}) "
            f"has no file staging reachable over this path. Snowflake stages "
            f"are supported; DuckLake and Iceberg would stage to object "
            f"storage, which is not implemented yet."
        )
    return ops


def upload(profile: Profile, local: str, target: str) -> dict:
    """Stage a local file, and confirm it arrived.

    The confirmation is not optional. Measured: a PUT through `adbc_scan`
    returns its column names and no rows, so the response cannot tell you
    whether the transfer happened — and Snowflake's own docs warn that a
    successful status does not mean files moved either.
    """
    ops = _ops_or_raise(profile)
    path = resolve_upload_path(profile, local)
    size = path.stat().st_size

    _run(profile, ops.put(str(path), target))

    result = {
        "uploaded": str(path),
        "target": target,
        "local_bytes": size,
    }
    if not ops.verify_after_put:
        result["verified"] = False
        return result

    columns, rows = _run(profile, ops.list(target))
    staged = [dict(zip(columns, row)) for row in rows]
    match = [f for f in staged if str(f.get("name", "")).endswith(path.name)]
    result["verified"] = bool(match)
    result["staged"] = match or staged
    if not match:
        raise StagingError(
            f"{path.name} is not in {target} after the upload. The PUT "
            f"response carries no rows, so this listing is the only "
            f"evidence either way — treat the upload as failed."
        )
    return result


def listing(profile: Profile, target: str) -> list[dict]:
    ops = _ops_or_raise(profile)
    columns, rows = _run(profile, ops.list(target))
    return [dict(zip(columns, row)) for row in rows]


def remove(profile: Profile, target: str) -> dict:
    ops = _ops_or_raise(profile)
    before = len(listing(profile, target))
    _run(profile, ops.remove(target))
    after = len(listing(profile, target))
    return {"target": target, "removed": max(0, before - after), "remaining": after}


# --------------------------------------------------------------------------
# loading a staged file into a table
#
# `COPY INTO` runs in both directions on Snowflake: stage -> table
# (loading) and table -> stage (unloading). lakesh only builds the
# loading direction. The unload form writes table contents out to a
# stage, which is an export path rather than an import one, and it is not
# what this feature is for — so the SQL is composed here rather than
# accepted from the caller, and the target is validated as a stage.


def _valid_table(name: str) -> str:
    """A table name safe to interpolate, or raise.

    A table name cannot be a bound parameter, so it goes into the
    statement as text and has to be validated rather than escaped.
    """
    from .dialect import QUALIFIED_NAME_RE

    cleaned = str(name).strip()
    if not QUALIFIED_NAME_RE.match(cleaned):
        raise StagingError(
            f"{name!r} is not a plain table name. Use `table`, "
            f"`schema.table` or `db.schema.table` — a name cannot be "
            f"parameterised, so anything else is refused rather than quoted."
        )
    return cleaned


def _valid_stage(target: str) -> str:
    """A stage reference, not a table.

    Guards the direction: `COPY INTO t FROM @s` loads, `COPY INTO @s FROM t`
    unloads. Only the first is built here, and requiring the source to
    look like a stage keeps a caller from inverting it.
    """
    cleaned = str(target).strip()
    if not cleaned.startswith("@"):
        raise StagingError(
            f"{target!r} is not a stage reference — it must start with `@`, "
            f"e.g. @~/exports. lakesh loads FROM a stage INTO a table; the "
            f"unload direction is not supported."
        )
    if any(c in cleaned for c in "'\";"):
        raise StagingError(f"{target!r} contains characters not valid in a stage path")
    return cleaned


def load(
    profile: Profile, table: str, target: str, *,
    file_format: str = "", create: bool = False,
) -> dict:
    """Load a staged file into an existing table.

    `create=True` asks the engine to create the table from the staged
    file's inferred schema first. It is off by default because a typo in
    a table name then silently creates a new table instead of failing,
    and inferred types are usually wrong in ways that surface much later.
    """
    ops = _ops_or_raise(profile)
    if ops.load is None:
        raise StagingError(
            f"profile {profile.name!r} cannot load a staged file into a table "
            f"over this path."
        )
    table = _valid_table(table)
    target = _valid_stage(target)
    fmt = file_format or getattr(profile, "file_format", "") or ops.default_format

    if create:
        named = getattr(profile, "infer_file_format", "") or ""
        if not ops.infer_create or not named:
            raise StagingError(
                "auto-create needs a NAMED file format object, because "
                "INFER_SCHEMA does not accept an inline format. Create one "
                "in the source and name it in the profile, e.g. "
                "infer_file_format = \"MYDB.FMTS.CSV_INFER\" — or create the "
                "table yourself and load without --create."
            )
        _valid_table(named)
        _run(profile, ops.infer_create(table, target, named))

    before = _count(profile, table)
    columns, rows = _run(profile, ops.load(table, target, fmt))
    after = _count(profile, table)

    result = {
        "table": table,
        "from": target,
        "file_format": fmt,
        "created": bool(create),
        "rows_after": after,
    }
    if rows:
        # Snowflake reports per-file status when it returns rows at all.
        result["report"] = [dict(zip(columns, row)) for row in rows]
    if before is not None and after is not None:
        # The count delta is the reliable signal: a PUT over this path
        # returns no rows, so a COPY may not either, and its own status
        # would not prove the data landed regardless.
        result["rows_loaded"] = max(0, after - before)
    return result


def _count(profile: Profile, table: str) -> int | None:
    try:
        _cols, rows = _run(profile, f"SELECT count(*) AS n FROM {table}")
        return int(rows[0][0]) if rows else None
    except Exception:
        return None            # table may not exist yet; the load will say so
