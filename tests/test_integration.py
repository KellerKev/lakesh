"""Integration test: run actual SQL against a live duckicelake proxy.

Skips itself cleanly when no proxy is reachable at the expected URL.
Starts nothing of its own — relies on `duckicelake` being up (or any
other Iceberg REST catalog you point LAKESH_TEST_URI at).
"""
from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

from lakesh.config import load_config
from lakesh.duck import connect


URI = os.environ.get("LAKESH_TEST_URI", "http://127.0.0.1:8181")
WAREHOUSE = os.environ.get("LAKESH_TEST_WAREHOUSE", "lake")


def _catalog_reachable() -> bool:
    try:
        return httpx.get(f"{URI}/v1/config", timeout=2.0).status_code == 200
    except Exception:
        return False


needs_live = pytest.mark.skipif(
    not _catalog_reachable(),
    reason=f"no Iceberg REST catalog reachable at {URI}",
)


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(f"""
default = "it"

[profiles.it]
uri = "{URI}"
warehouse = "{WAREHOUSE}"

[profiles.it.s3]
endpoint   = "{os.environ.get('LAKESH_TEST_S3_ENDPOINT', 'http://127.0.0.1:9000')}"
region     = "us-east-1"
access_key = "{os.environ.get('LAKESH_TEST_S3_KEY', 'minioadmin')}"
secret_key = "{os.environ.get('LAKESH_TEST_S3_SECRET', 'minioadmin')}"
path_style = true
""")
    return p


@needs_live
def test_connect_and_list_namespaces(config_file: Path):
    """ATTACH + minimum information_schema query — the same path
    `lakesh doctor` uses."""
    cfg = load_config(config_file)
    con = connect(cfg.get("it"))
    try:
        rows = con.execute(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE catalog_name='ice' "
            "  AND schema_name NOT IN ('main','information_schema','pg_catalog') "
            "ORDER BY 1"
        ).fetchall()
        # No assertion on specific namespaces — the catalog may be fresh.
        # We just want the query to run without error.
        assert isinstance(rows, list)
    finally:
        con.close()


@needs_live
def test_select_one_roundtrips(config_file: Path):
    """Pure-DuckDB query (no catalog tables) — tests that the connection
    itself is healthy before ATTACH."""
    cfg = load_config(config_file)
    con = connect(cfg.get("it"))
    try:
        (n,) = con.execute("SELECT 42").fetchone()
        assert n == 42
    finally:
        con.close()


@needs_live
def test_list_tables_via_information_schema(config_file: Path):
    """Exercises the same query the REPL completer uses. The list may be
    empty on a fresh catalog but the query must execute cleanly."""
    cfg = load_config(config_file)
    con = connect(cfg.get("it"))
    try:
        rows = con.execute(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_catalog='ice' ORDER BY 1, 2"
        ).fetchall()
        assert isinstance(rows, list)
    finally:
        con.close()
