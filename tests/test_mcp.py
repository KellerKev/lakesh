"""Tests for the MCP server module.

Covers the read-only safety gate, profile listing, and (when a live
catalog is reachable) end-to-end query execution. Doesn't speak the MCP
wire protocol — calls the Python tool functions directly, which is what
FastMCP dispatches to anyway.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from lakesh import mcp as lakesh_mcp


URI = os.environ.get("LAKESH_TEST_URI", "http://127.0.0.1:8181")
WAREHOUSE = os.environ.get("LAKESH_TEST_WAREHOUSE", "lake")


def _live() -> bool:
    try:
        return httpx.get(f"{URI}/v1/config", timeout=2.0).status_code == 200
    except Exception:
        return False


needs_live = pytest.mark.skipif(
    not _live(), reason=f"no Iceberg REST catalog reachable at {URI}",
)


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(f"""
default = "it"

[profiles.it]
uri       = "{URI}"
warehouse = "{WAREHOUSE}"

[profiles.it.s3]
endpoint   = "{os.environ.get('LAKESH_TEST_S3_ENDPOINT', 'http://127.0.0.1:9000')}"
access_key = "{os.environ.get('LAKESH_TEST_S3_KEY', 'minioadmin')}"
secret_key = "{os.environ.get('LAKESH_TEST_S3_SECRET', 'minioadmin')}"
""")
    monkeypatch.setenv("LAKESH_CONFIG", str(p))
    return p


# --------------------------------------------------------------------------
# read-only gate (no catalog needed)


@pytest.mark.parametrize("sql", [
    "SELECT 1",
    "  select * from t",
    "WITH x AS (SELECT 1) SELECT * FROM x",
    "show tables",
    "DESCRIBE foo",
    "EXPLAIN SELECT 1",
    "PRAGMA version",
])
def test_is_read_only_accepts_safe_statements(sql):
    assert lakesh_mcp._is_read_only(sql)


@pytest.mark.parametrize("sql", [
    "INSERT INTO t VALUES (1)",
    "UPDATE t SET x = 1",
    "DELETE FROM t",
    "DROP TABLE t",
    "CREATE TABLE t (id int)",
    "ALTER TABLE t ADD COLUMN c int",
    "TRUNCATE t",
    "CALL ducklake_merge_adjacent_files('lake', 't')",
])
def test_is_read_only_rejects_writes(sql):
    assert not lakesh_mcp._is_read_only(sql)


def test_writes_disabled_by_default(monkeypatch):
    monkeypatch.delenv("LAKESH_MCP_WRITE", raising=False)
    assert not lakesh_mcp._writes_enabled()


def test_writes_enabled_via_env(monkeypatch):
    monkeypatch.setenv("LAKESH_MCP_WRITE", "1")
    assert lakesh_mcp._writes_enabled()


def test_query_rejects_write_in_read_only_mode(monkeypatch, config_file):
    monkeypatch.delenv("LAKESH_MCP_WRITE", raising=False)
    out = json.loads(lakesh_mcp.query("DROP TABLE t"))
    assert "error" in out
    assert "read-only" in out["error"]


def test_query_rejects_empty_sql(monkeypatch, config_file):
    out = json.loads(lakesh_mcp.query("   "))
    assert out["error"] == "empty query"


def test_query_rejects_unknown_format(monkeypatch, config_file):
    out = json.loads(lakesh_mcp.query("SELECT 1", format="yaml"))
    assert "unknown format" in out["error"]


# --------------------------------------------------------------------------
# profile listing (uses config but no catalog)


def test_list_profiles_shape(config_file):
    out = json.loads(lakesh_mcp.list_profiles())
    assert isinstance(out, list)
    assert any(p["name"] == "it" and p["default"] for p in out)
    assert all(p["type"] in ("iceberg-rest", "ducklake") for p in out)


# --------------------------------------------------------------------------
# live: hit the catalog


@needs_live
def test_query_select_one(config_file):
    out = json.loads(lakesh_mcp.query("SELECT 42 AS answer"))
    assert out["columns"] == ["answer"]
    assert out["rows"] == [{"answer": 42}]
    assert out["row_count"] == 1


@needs_live
def test_query_table_format(config_file):
    txt = lakesh_mcp.query("SELECT 1 AS a, 'hi' AS b", format="table")
    assert "a" in txt and "b" in txt
    assert "1" in txt and "hi" in txt
    assert "(1 rows)" in txt or "(1 row)" in txt


@needs_live
def test_list_namespaces_and_tables(config_file):
    ns_json = lakesh_mcp.list_namespaces()
    namespaces = json.loads(ns_json)
    assert isinstance(namespaces, list)
    # `default` is always created by ducklake-init in the test catalog.
    # The list might be empty on a hermetic catalog though, so don't
    # over-assert.
    tbl_json = lakesh_mcp.list_tables()
    tables = json.loads(tbl_json)
    assert isinstance(tables, list)


@needs_live
def test_query_truncates_at_limit(config_file):
    """The `limit` arg caps row count, and we report `truncated_at`
    when the limit was hit so the LLM knows it's looking at a slice."""
    sql = "SELECT * FROM range(10)"
    out = json.loads(lakesh_mcp.query(sql, limit=3))
    assert out["row_count"] == 3
    assert out["truncated_at"] == 3


# --------------------------------------------------------------------------
# AuthRequired surfacing (no catalog needed)


def test_query_surfaces_auth_required(tmp_path: Path, monkeypatch):
    """A profile on an interactive grant with an empty token cache must
    return an actionable JSON error, not hang or crash."""
    p = tmp_path / "config.toml"
    p.write_text("""
default = "snow"

[profiles.snow]
type         = "adbc"
driver       = "snowflake"
token_option = "auth_token"

[profiles.snow.oauth]
grant                         = "device_code"
client_id                     = "cid"
device_authorization_endpoint = "https://idp.invalid/device"
token_endpoint                = "https://idp.invalid/token"
""")
    monkeypatch.setenv("LAKESH_CONFIG", str(p))
    monkeypatch.setenv("LAKESH_TOKEN_CACHE", str(tmp_path / "tokens.json"))

    out = json.loads(lakesh_mcp.query("SELECT 1"))
    assert "error" in out
    assert "lakesh auth login -p snow" in out["error"]
