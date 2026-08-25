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


# --------------------------------------------------------------------------
# native passthrough for adbc profiles
#
# These use a fake connection rather than a live source: what needs
# pinning is *which statement lakesh sends*, not what a driver does with
# it. The behaviour they protect (introspection through the source
# instead of DuckDB's catalog) is a 240s-timeout-vs-6s difference
# against a remote account, so a silent regression is expensive.


class _FakeCursor:
    def __init__(self, columns, rows):
        self.description = [(c,) for c in columns] if columns else None
        self._rows = rows

    def fetchall(self):
        return list(self._rows)

    def fetchmany(self, n):
        return list(self._rows)[:n]


class _FakeNativeConnection:
    """Records every statement handed to adbc_scan()."""

    # The default row is wide enough for the widest introspection shape
    # (describe_table's four columns); narrower callers just index less.
    def __init__(self, columns=("a", "b", "c", "d"), rows=(("v", "w", "YES", 1),)):
        self.statements: list[str] = []
        self._columns, self._rows = list(columns), list(rows)

    def execute(self, sql, params=None):
        if "adbc_scan" in sql:
            self.statements.append(params[1])
            return _FakeCursor(self._columns, self._rows)
        raise AssertionError(f"unexpected statement on the native path: {sql}")

    def close(self):
        pass


@pytest.fixture
def adbc_config(tmp_path: Path, monkeypatch) -> Path:
    p = tmp_path / "config.toml"
    p.write_text("""
default = "snow"

[profiles.snow]
type    = "adbc"
driver  = "/x/libadbc_driver_snowflake.so"
uri     = "user:pw-not-real-just-long@ACCOUNT"
catalog = "snow"

[profiles.lake]
type         = "ducklake"
postgres_dsn = "dbname=lake host=/tmp"
data_path    = "s3://b/p/"
""")
    monkeypatch.setenv("LAKESH_CONFIG", str(p))
    return p


@pytest.fixture
def fake_native(monkeypatch):
    con = _FakeNativeConnection()
    monkeypatch.setattr(
        lakesh_mcp, "connect_native", lambda prof, **kw: (con, 1234)
    )
    return con


def test_prefer_native_only_for_adbc(adbc_config):
    cfg = lakesh_mcp._load_or_raise()
    assert lakesh_mcp._prefer_native(cfg.get("snow")) is True
    assert lakesh_mcp._prefer_native(cfg.get("lake")) is False


def test_query_defaults_to_native_for_adbc(adbc_config, fake_native):
    out = json.loads(lakesh_mcp.query("SHOW DATABASES"))
    assert out["mode"] == "native"
    # Sent verbatim — DuckDB never parses it, which is the whole point:
    # SHOW / QUALIFY / cross-database references can't survive the
    # attached-catalog path.
    assert fake_native.statements == ["SHOW DATABASES"]


def test_query_native_false_goes_back_through_duckdb(adbc_config, fake_native):
    """`native=false` is the only way to join a source against a local
    Parquet file in one statement, so it has to keep working."""
    out = json.loads(lakesh_mcp.query("SELECT 1", native=False))
    # No live driver here, so this fails at ATTACH — the point is that it
    # took the DuckDB path at all rather than adbc_scan.
    assert fake_native.statements == []
    assert "mode" not in out or out["mode"] == "duckdb"


def test_introspection_goes_to_the_source_for_adbc(adbc_config, fake_native):
    lakesh_mcp.list_namespaces()
    lakesh_mcp.list_tables(namespace="ACCOUNT_USAGE")
    lakesh_mcp.describe_table("ACCOUNT_USAGE", "WAREHOUSE_METERING_HISTORY")

    assert len(fake_native.statements) == 3
    schemata, tables, columns = fake_native.statements
    assert "information_schema.schemata" in schemata
    assert "information_schema.tables" in tables
    assert "'ACCOUNT_USAGE'" in tables
    assert "information_schema.columns" in columns
    assert "'WAREHOUSE_METERING_HISTORY'" in columns
    # No table_catalog predicate: the native connection is already scoped
    # to one database, and DuckDB's catalog alias is meaningless there.
    assert "table_catalog" not in tables


def test_native_introspection_escapes_interpolated_values(adbc_config, fake_native):
    """adbc_scan takes the statement as a string, so values coming from
    the model are escaped rather than bound. Verify the escaping."""
    lakesh_mcp.list_tables(namespace="it's")
    assert "'it''s'" in fake_native.statements[0]


def test_read_only_profile_beats_write_env(tmp_path: Path, monkeypatch):
    """Native passthrough opens its own ADBC connection, which the
    ATTACH's READ_ONLY flag never sees — so the tool has to enforce it,
    and the profile is the more specific statement of intent."""
    p = tmp_path / "config.toml"
    p.write_text("""
default = "ro"

[profiles.ro]
type      = "adbc"
driver    = "/x/driver.so"
uri       = "user@ACCOUNT"
read_only = true
""")
    monkeypatch.setenv("LAKESH_CONFIG", str(p))
    monkeypatch.setenv("LAKESH_MCP_WRITE", "1")

    out = json.loads(lakesh_mcp.query("DROP TABLE t"))
    assert "read_only" in out["error"]
    assert "LAKESH_MCP_WRITE does not override" in out["error"]


# --------------------------------------------------------------------------
# JSON coercion of source-native types


def test_jsonable_coerces_decimal_and_temporals():
    import datetime as dt
    import decimal
    import uuid

    j = lakesh_mcp._jsonable
    assert j(decimal.Decimal("170.54")) == pytest.approx(170.54)
    assert j(dt.date(2026, 1, 2)) == "2026-01-02"
    assert j(uuid.UUID("14c62ee3-390d-4b29-9dc2-c595593faa39")).startswith("14c62ee3")
    assert j(b"\x00\xff") == "00ff"


def test_integral_decimal_becomes_int_not_float():
    """A NUMBER(38,0) key is exactly what an agent joins on, and float
    silently rounds it past 2^53."""
    import decimal

    big = decimal.Decimal("12345678901234567890")
    out = lakesh_mcp._jsonable(big)
    assert isinstance(out, int)
    assert out == 12345678901234567890


def test_query_serializes_decimal_rows(adbc_config, monkeypatch):
    """Snowflake returns Decimal for every NUMBER column; without
    coercion the whole tool call aborts with "Object of type Decimal is
    not JSON serializable"."""
    import decimal

    con = _FakeNativeConnection(
        columns=("warehouse_name", "credits"),
        rows=[("ANALYTICS_WH", decimal.Decimal("170.54"))],
    )
    monkeypatch.setattr(lakesh_mcp, "connect_native", lambda prof, **kw: (con, 1))
    out = json.loads(lakesh_mcp.query("SELECT warehouse_name, credits FROM x"))
    assert out["rows"] == [{"warehouse_name": "ANALYTICS_WH", "credits": 170.54}]
