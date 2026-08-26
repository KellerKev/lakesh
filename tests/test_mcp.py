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

    # Four, not three: on a dialect that can report freshness,
    # describe_table also asks for it — on the already-open handle, so
    # one extra round trip rather than a reconnect.
    assert len(fake_native.statements) == 4
    schemata, tables, columns, table_meta = fake_native.statements
    assert "information_schema.schemata" in schemata
    assert "information_schema.tables" in tables
    assert "'ACCOUNT_USAGE'" in tables
    assert "information_schema.columns" in columns
    assert "'WAREHOUSE_METERING_HISTORY'" in columns
    assert "last_altered" in table_meta
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


# --------------------------------------------------------------------------
# search_objects
#
# The discovery tool: an agent that doesn't know where revenue lives can
# otherwise only list_tables per schema and eyeball the output.


_SEARCH_COLUMNS = ("matched_on", "object_schema", "object_table",
                   "object_column", "data_type")


@pytest.fixture
def fake_search(monkeypatch):
    """A native connection returning search-shaped (5-column) rows."""
    con = _FakeNativeConnection(columns=_SEARCH_COLUMNS, rows=[
        ("schema", "REVENUE", None, None, None),
        ("table", "ANALYTICS", "FCT_REVENUE", None, None),
        ("column", "ANALYTICS", "ORDERS", "REVENUE_USD", "NUMBER"),
        ("column", "ANALYTICS", "ORDERS", "REVENUE_LOCAL", "NUMBER"),
    ])
    monkeypatch.setattr(lakesh_mcp, "connect_native", lambda prof, **kw: (con, 1))
    return con


def test_search_objects_is_one_statement(adbc_config, fake_search):
    """The load-bearing property. Three separate queries would triple the
    latency on exactly the source where latency is the problem."""
    lakesh_mcp.search_objects("revenue", profile="snow")

    assert len(fake_search.statements) == 1
    sql = fake_search.statements[0]
    for view in ("information_schema.schemata", "information_schema.tables",
                 "information_schema.columns"):
        assert view in sql
    assert sql.count("UNION ALL") == 2
    assert "ILIKE '%revenue%' ESCAPE '!'" in sql


def test_search_objects_every_leg_is_aliased(adbc_config, fake_search):
    """A UNION takes its column names from whichever branch leads, and
    dropping the schema leg promotes one whose NULL casts would otherwise
    both come back named "varchar" — which the adbc_scan binder rejects
    as duplicate columns. Regression test for exactly that."""
    lakesh_mcp.search_objects("revenue", profile="snow", match="table")
    sql = fake_search.statements[0]
    assert "CAST(NULL AS VARCHAR) AS object_column" in sql
    assert "CAST(NULL AS VARCHAR) AS data_type" in sql
    # No bare, unaliased cast survives anywhere.
    assert "CAST(NULL AS VARCHAR)," not in sql


def test_search_objects_escapes_quote_and_underscore(adbc_config, fake_search):
    lakesh_mcp.search_objects("it's", profile="snow")
    assert "'%it''s%'" in fake_search.statements[0]

    fake_search.statements.clear()
    # `_` is literal: nobody types fact_revenue meaning fact<any char>revenue.
    lakesh_mcp.search_objects("fact_revenue", profile="snow")
    assert "'%fact!_revenue%'" in fake_search.statements[0]


def test_search_objects_honours_explicit_percent(adbc_config, fake_search):
    """An explicit wildcard suppresses the implicit %…% wrap, so
    `revenue%` is a prefix match rather than a contains match."""
    out = json.loads(lakesh_mcp.search_objects("revenue%", profile="snow"))
    assert out["like"] == "revenue%"
    assert "'revenue%'" in fake_search.statements[0]


def test_search_objects_rejects_backslash(adbc_config, fake_search):
    """`_lit` doubles quotes but not backslashes, and Snowflake reads a
    backslash inside a literal as an escape — so a trailing one would
    swallow the closing quote there and nowhere else."""
    out = json.loads(lakesh_mcp.search_objects(r"back\slash", profile="snow"))
    assert "backslash" in out["error"]
    assert fake_search.statements == []


def test_search_objects_rejects_empty_pattern(adbc_config, fake_search):
    assert "empty" in json.loads(lakesh_mcp.search_objects("   ", profile="snow"))["error"]
    assert fake_search.statements == []


def test_search_objects_scopes_to_namespace(adbc_config, fake_search):
    """The main lever for keeping a search fast on a large warehouse."""
    lakesh_mcp.search_objects("revenue", profile="snow", namespace="ANALYTICS")
    sql = fake_search.statements[0]
    assert sql.count("= 'ANALYTICS'") == 2          # tables + columns legs
    # Searching schema names inside one schema is meaningless.
    assert "information_schema.schemata" not in sql


def test_search_objects_match_drops_legs(adbc_config, fake_search):
    lakesh_mcp.search_objects("revenue", profile="snow", match="column")
    sql = fake_search.statements[0]
    assert "information_schema.columns" in sql
    assert "information_schema.tables" not in sql
    assert "UNION ALL" not in sql


def test_search_objects_rejects_contradictory_scope(adbc_config, fake_search):
    out = json.loads(lakesh_mcp.search_objects(
        "x", profile="snow", match="schema", namespace="ANALYTICS"))
    assert "cannot apply" in out["error"]
    assert fake_search.statements == []


def test_search_objects_groups_per_object(adbc_config, fake_search):
    """One entry per matching object, not per matching column — a
    "revenue" search can hit 60 columns across 8 tables and the agent
    wants the 8 tables."""
    out = json.loads(lakesh_mcp.search_objects("revenue", profile="snow"))

    assert out["result_count"] == 3
    by_table = {r["table"]: r for r in out["results"]}
    assert by_table[None]["matched_on"] == ["schema"]
    assert by_table["FCT_REVENUE"]["matched_on"] == ["table"]
    orders = by_table["ORDERS"]
    assert orders["matched_on"] == ["column"]
    assert [c["column"] for c in orders["columns"]] == ["REVENUE_USD", "REVENUE_LOCAL"]
    assert out["mode"] == "native"


def test_search_objects_matched_on_is_a_sorted_list(adbc_config, monkeypatch):
    """An object can match by name *and* by column; a single string would
    force duplicate entries for the commonest case."""
    con = _FakeNativeConnection(columns=_SEARCH_COLUMNS, rows=[
        ("table", "ANALYTICS", "FCT_REVENUE", None, None),
        ("column", "ANALYTICS", "FCT_REVENUE", "REVENUE_USD", "NUMBER"),
    ])
    monkeypatch.setattr(lakesh_mcp, "connect_native", lambda prof, **kw: (con, 1))
    out = json.loads(lakesh_mcp.search_objects("revenue", profile="snow"))
    assert out["result_count"] == 1
    assert out["results"][0]["matched_on"] == ["column", "table"]


def test_search_objects_caps_columns_per_object(adbc_config, monkeypatch):
    con = _FakeNativeConnection(columns=_SEARCH_COLUMNS, rows=[
        ("column", "S", "WIDE", f"ID_{i}", "NUMBER") for i in range(25)
    ])
    monkeypatch.setattr(lakesh_mcp, "connect_native", lambda prof, **kw: (con, 1))
    out = json.loads(lakesh_mcp.search_objects("id", profile="snow"))
    result = out["results"][0]
    assert len(result["columns"]) == lakesh_mcp._MAX_COLUMNS_PER_OBJECT
    assert result["columns_truncated"] is True
    assert result["column_match_count"] == 25


def test_search_objects_truncates_with_a_sentinel_row(adbc_config, monkeypatch):
    """`limit + 1` is fetched so "exactly limit matches" is
    distinguishable from "more than limit" without guessing. Note
    truncated_at counts raw matches, not grouped results."""
    con = _FakeNativeConnection(columns=_SEARCH_COLUMNS, rows=[
        ("table", "S", f"T{i}", None, None) for i in range(5)
    ])
    monkeypatch.setattr(lakesh_mcp, "connect_native", lambda prof, **kw: (con, 1))

    out = json.loads(lakesh_mcp.search_objects("t", profile="snow", limit=4))
    assert "LIMIT 5" in con.statements[0]
    assert out["truncated_at"] == 4
    assert out["result_count"] == 4

    con.statements.clear()
    out = json.loads(lakesh_mcp.search_objects("t", profile="snow", limit=10))
    assert out["truncated_at"] is None


def test_search_objects_all_profiles_isolates_failures(adbc_config, monkeypatch):
    """One profile with a cold token must not fail the whole search."""
    rows = [("table", "PUBLIC", "ORDERS", None, None)]

    def _connect_native(prof, **kw):
        raise RuntimeError("260001: user is empty")

    class _FakeDuckConnection:
        """`lake` is a ducklake profile, so it takes the DuckDB arm —
        which binds parameters instead of going through adbc_scan."""
        def execute(self, sql, params=None):
            return _FakeCursor(_SEARCH_COLUMNS, rows)

        def close(self):
            pass

    monkeypatch.setattr(lakesh_mcp, "connect_native", _connect_native)
    monkeypatch.setattr(lakesh_mcp, "connect", lambda prof, **kw: _FakeDuckConnection())

    out = json.loads(lakesh_mcp.search_objects("orders", all_profiles=True))
    assert [e["profile"] for e in out["errors"]] == ["snow"]
    assert out["searched_profiles"] == ["lake"]
    assert out["results"] and out["results"][0]["profile"] == "lake"


def test_search_objects_duckdb_path_binds_parameters(tmp_path, monkeypatch):
    """The non-adbc arm runs against a real in-memory DuckDB — the SQL is
    built once and must actually execute, not merely look right."""
    import duckdb as _duckdb

    con = _duckdb.connect()
    con.execute("CREATE SCHEMA revenue")
    con.execute("CREATE TABLE revenue.fct_orders(order_id BIGINT, revenue_usd DECIMAL(18,2))")
    catalog = con.execute("SELECT current_database()").fetchone()[0]

    body, params = lakesh_mcp._search_sql_duckdb("%revenue%", None, "all", catalog)
    rows = con.execute(lakesh_mcp._wrap_search(body, 100), params).fetchall()

    grouped = lakesh_mcp._group_matches(rows)
    by_table = {r["table"]: r for r in grouped}
    assert by_table[None]["matched_on"] == ["schema"]           # the revenue schema
    assert by_table["fct_orders"]["matched_on"] == ["column"]   # revenue_usd


# --------------------------------------------------------------------------
# deadlines and paging


def test_effective_timeout_precedence(adbc_config, monkeypatch):
    """A profile's timeout is a ceiling, not a default — the same
    precedent as `read_only` beating LAKESH_MCP_WRITE."""
    from lakesh.config import Profile

    monkeypatch.delenv("LAKESH_MCP_TIMEOUT_S", raising=False)
    bare = Profile(name="p")
    capped = Profile(name="p", query_timeout_s=10.0)

    assert lakesh_mcp._effective_timeout(bare, None) == lakesh_mcp._DEFAULT_TIMEOUT_S
    assert lakesh_mcp._effective_timeout(bare, 5) == 5
    assert lakesh_mcp._effective_timeout(bare, 0) is None       # 0 disables

    assert lakesh_mcp._effective_timeout(capped, 5) == 5        # may narrow
    assert lakesh_mcp._effective_timeout(capped, 999) == 10.0   # may not widen
    assert lakesh_mcp._effective_timeout(capped, 0) == 10.0     # nor disable

    monkeypatch.setenv("LAKESH_MCP_TIMEOUT_S", "42")
    assert lakesh_mcp._effective_timeout(bare, None) == 42
    assert lakesh_mcp._effective_timeout(bare, 7) == 7          # call beats env
    monkeypatch.setenv("LAKESH_MCP_TIMEOUT_S", "not-a-number")
    assert lakesh_mcp._effective_timeout(bare, None) == lakesh_mcp._DEFAULT_TIMEOUT_S


def test_paginate_leaves_offset_zero_untouched():
    """The common path must be byte-identical to what it was before
    pagination existed."""
    assert lakesh_mcp._paginate("SELECT 1", 100, 0) == ("SELECT 1", False)


def test_paginate_wraps_and_fetches_a_sentinel():
    """The SQL LIMIT is limit+1 to match the caller's fetch. Capping it
    at `limit` makes every full page look like the last one."""
    sql, wrapped = lakesh_mcp._paginate("SELECT n FROM t", 3, 3)
    assert wrapped is True
    assert sql == "SELECT * FROM (SELECT n FROM t) AS _lakesh_page LIMIT 4 OFFSET 3"


@pytest.mark.parametrize("sql", [
    "SHOW DATABASES", "EXPLAIN SELECT 1", "PRAGMA version", "DESCRIBE t",
])
def test_paginate_skips_statements_that_are_not_relations(sql):
    """DuckDB tolerates a wrapped SHOW, but the same string goes to
    Snowflake where SHOW is not selectable — so it stays on the
    client-side skip path for both."""
    assert lakesh_mcp._paginate(sql, 10, 5) == (sql, False)


def test_query_offset_wraps_the_statement(adbc_config, fake_native):
    lakesh_mcp.query("SELECT 1", profile="snow", limit=10, offset=20)
    assert fake_native.statements == [
        "SELECT * FROM (SELECT 1) AS _lakesh_page LIMIT 11 OFFSET 20"
    ]


def test_query_offset_zero_sends_sql_verbatim(adbc_config, fake_native):
    lakesh_mcp.query("SELECT 1", profile="snow")
    assert fake_native.statements == ["SELECT 1"]


def test_query_rejects_offset_past_the_cap(adbc_config, fake_native):
    out = json.loads(lakesh_mcp.query("SELECT 1", profile="snow", offset=10**9))
    assert "exceeds" in out["error"]
    assert fake_native.statements == []


def test_query_warns_when_paging_without_order_by(adbc_config, fake_native):
    """Each page is a separate execution, so unordered paging genuinely
    duplicates and skips rows — not a theoretical risk."""
    out = json.loads(lakesh_mcp.query("SELECT 1", profile="snow", offset=5))
    assert any("ORDER BY" in w for w in out["warnings"])

    out = json.loads(lakesh_mcp.query(
        "SELECT 1 ORDER BY 1", profile="snow", offset=5))
    assert "warnings" not in out

    # No offset, no warning — the statement is run once, order is moot.
    out = json.loads(lakesh_mcp.query("SELECT 1", profile="snow"))
    assert "warnings" not in out


def test_query_has_more_is_exact(adbc_config, monkeypatch):
    """`truncated_at` cannot tell "exactly limit rows" from "truncated";
    `has_more` can, because a sentinel row is fetched."""
    con = _FakeNativeConnection(columns=("n",), rows=[(i,) for i in range(3)])
    monkeypatch.setattr(lakesh_mcp, "connect_native", lambda prof, **kw: (con, 1))

    out = json.loads(lakesh_mcp.query("SELECT n FROM t", profile="snow", limit=2))
    assert out["row_count"] == 2
    assert out["has_more"] is True and out["next_offset"] == 2
    assert out["truncated_at"] == 2

    out = json.loads(lakesh_mcp.query("SELECT n FROM t", profile="snow", limit=3))
    assert out["has_more"] is False and out["next_offset"] is None
    # ...whereas the legacy field still cries wolf on an exactly-full page.
    assert out["truncated_at"] == 3


def test_query_reports_how_the_deadline_was_enforced(adbc_config, fake_native):
    """Native mode cannot promise a hard deadline, so it must not claim
    one: a statement blocked inside the driver does not observe an
    interrupt until the driver returns."""
    out = json.loads(lakesh_mcp.query("SELECT 1", profile="snow"))
    assert out["enforced"] == "best_effort"

    out = json.loads(lakesh_mcp.query("SELECT 1", profile="snow", timeout_s=0))
    assert out["enforced"] == "none"


def test_query_timeout_payload_is_typed(adbc_config, monkeypatch):
    from lakesh.duck import QueryTimeout

    def _boom(prof, **kw):
        raise QueryTimeout(3.0, 30.0, hard=False)

    monkeypatch.setattr(lakesh_mcp, "connect_native", _boom)
    out = json.loads(lakesh_mcp.query("SELECT 1", profile="snow", timeout_s=3))
    assert out["error_type"] == "timeout"
    assert out["timeout_s"] == 3.0 and out["elapsed_s"] == 30.0
    assert out["enforced"] == "best_effort"
    assert "could not abort" in out["hint"]


def test_deadline_hard_aborts_the_duckdb_path():
    """The one path where the deadline is real. Deterministic: measured
    at 2.00s for a 2s deadline."""
    import duckdb as _duckdb
    from lakesh.duck import QueryTimeout, deadline

    con = _duckdb.connect()
    with pytest.raises(QueryTimeout) as excinfo:
        with deadline(con, 1.0):
            con.execute(
                "SELECT count(*) FROM range(30000000000) t(i) WHERE i%7=3"
            ).fetchall()
    assert excinfo.value.hard is True
    # The connection survives an interrupt and is still closeable, which
    # is why _open/_open_native need no change.
    assert con.execute("SELECT 1").fetchone() == (1,)
    con.close()


def test_deadline_is_a_noop_without_seconds():
    import duckdb as _duckdb
    from lakesh.duck import deadline

    con = _duckdb.connect()
    for seconds in (None, 0):
        with deadline(con, seconds):
            assert con.execute("SELECT 1").fetchone() == (1,)
    con.close()


def test_idle_interrupt_does_not_poison_the_connection():
    """The watchdog can fire in the race between the final fetch and
    timer.cancel(); that has to be harmless or the design needs a lock."""
    import duckdb as _duckdb

    con = _duckdb.connect()
    con.interrupt()
    assert con.execute("SELECT 1").fetchone() == (1,)
    assert con.execute("SELECT 2").fetchone() == (2,)
    con.close()


def test_driver_timeout_sql_is_per_driver():
    from lakesh.duck import _driver_timeout_sql

    pg = _driver_timeout_sql("/x/libadbc_driver_postgresql.so", 3)
    assert "set_config('statement_timeout', '3000', false)" in pg
    sf = _driver_timeout_sql("snowflake", 3)
    assert "STATEMENT_TIMEOUT_IN_SECONDS = 3" in sf
    assert _driver_timeout_sql("sqlite", 3) is None


# --------------------------------------------------------------------------
# pre-flight estimates
#
# The rule these protect: emit a number only when one was genuinely
# extracted. A model that reads `estimated_rows: 0` concludes the query
# is free.


def test_estimate_unavailable_on_postgres_native(tmp_path, monkeypatch):
    """The Postgres ADBC driver wraps every statement in COPY (...) TO
    STDOUT, which rejects EXPLAIN. The routing advice is the whole value
    of the answer on this source, so it must not just fail."""
    p = tmp_path / "config.toml"
    p.write_text("""
default = "pg"

[profiles.pg]
type    = "adbc"
driver  = "/x/libadbc_driver_postgresql.so"
uri     = "postgresql://u@h/db"
catalog = "pg"
""")
    monkeypatch.setenv("LAKESH_CONFIG", str(p))
    con = _FakeNativeConnection()
    monkeypatch.setattr(lakesh_mcp, "connect_native", lambda prof, **kw: (con, 1))

    out = json.loads(lakesh_mcp.query("SELECT * FROM t", estimate=True))
    assert out["method"] == "unavailable"
    assert "COPY" in out["reason"] and 'estimate="count"' in out["reason"]
    assert "estimated_rows" not in out
    # And crucially: it did not run the statement to find that out.
    assert con.statements == []


def test_estimate_count_builds_a_probe(adbc_config, monkeypatch):
    con = _FakeNativeConnection(columns=("n",), rows=[(4013113,)])
    monkeypatch.setattr(lakesh_mcp, "connect_native", lambda prof, **kw: (con, 1))

    out = json.loads(lakesh_mcp.query("SELECT * FROM t", estimate="count"))
    assert con.statements == ["SELECT count(*) AS n FROM (SELECT * FROM t) AS _lakesh_est"]
    assert out["method"] == "count"
    assert out["exact_rows"] == 4013113
    # It executed a scan on the source; say so rather than implying it
    # was a free lookup.
    assert "not free" in out["note"]


def test_estimate_rejects_unknown_mode(adbc_config, fake_native):
    out = json.loads(lakesh_mcp.query("SELECT 1", estimate="guess"))
    assert "unknown estimate" in out["error"]
    assert fake_native.statements == []


def test_estimate_and_offset_are_exclusive(adbc_config, fake_native):
    out = json.loads(lakesh_mcp.query("SELECT 1", estimate=True, offset=10))
    assert "mutually exclusive" in out["error"]
    assert fake_native.statements == []


def test_estimate_false_runs_the_query_normally(adbc_config, fake_native):
    out = json.loads(lakesh_mcp.query("SELECT 1", estimate=False))
    assert "estimate" not in out
    assert fake_native.statements == ["SELECT 1"]


def test_duckdb_cardinality_parses_a_real_plan():
    """`Estimated Cardinality` is an unpinned DuckDB internal, and the
    plan arrives as a JSON *list* holding the root node — assuming a dict
    silently yields None. Worth a real connection rather than a fixture."""
    import duckdb as _duckdb

    con = _duckdb.connect()
    con.execute("CREATE TABLE t AS SELECT i FROM range(100000) t(i)")
    rows = con.execute("EXPLAIN (FORMAT json) SELECT * FROM t WHERE i % 3 = 0").fetchall()
    estimate = lakesh_mcp._duckdb_cardinality(rows)
    assert isinstance(estimate, int) and estimate > 0
    con.close()


@pytest.mark.parametrize("plan", [
    [("physical_plan", "not json at all")],
    [("physical_plan", '[{"name":"X","children":[],"extra_info":{}}]')],
    [],
])
def test_duckdb_cardinality_returns_none_rather_than_guessing(plan):
    assert lakesh_mcp._duckdb_cardinality(plan) is None
