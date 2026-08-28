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
    """Execute a stage statement against the source and return its rows."""
    from .duck import adbc_native_scan, connect_native

    con, handle = connect_native(profile, interactive=False)
    try:
        cur = adbc_native_scan(con, handle, sql)
        columns = [d[0] for d in cur.description] if cur.description else []
        return columns, cur.fetchall()
    finally:
        con.close()


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
