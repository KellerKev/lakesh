"""The Session abstraction over query backends.

lakesh had two connection shapes — native ADBC `(con, handle)` and an
attached DuckDB connection — with a `handle is None` sentinel threaded
through every consumer. `Session` collapses that to one interface so a
third backend (Python drivers) is a third implementation, not a third
branch everywhere. These pin the contract every implementation must meet.
"""
from __future__ import annotations

import duckdb
import pytest

from lakesh import backend
from lakesh.config import ConfigError, Profile


def _duck_profile():
    # iceberg-rest type -> DuckDB dialect, attached path
    return Profile(name="t", uri="u", warehouse="w")


# --------------------------------------------------------------------------
# DuckSession — the attached path

def test_run_returns_columns_and_rows():
    s = backend.DuckSession(duckdb.connect(":memory:"), _duck_profile())
    cols, rows = s.run("SELECT 1 AS a, 2 AS b")
    assert cols == ["a", "b"] and rows == [(1, 2)]
    s.close()


def test_a_write_applies_once_on_the_duck_path():
    """DuckDB has no adbc_scan double-execution, so a write through run()
    lands exactly once — the property the whole routing exists to protect."""
    s = backend.DuckSession(duckdb.connect(":memory:"), _duck_profile())
    s.run("CREATE TABLE t (n INT)")
    s.run("INSERT INTO t VALUES (1)")
    assert s.run("SELECT count(*) FROM t")[1] == [(1,)]
    s.close()


def test_execute_takes_bound_params():
    """The attached path's metadata queries use bound params."""
    s = backend.DuckSession(duckdb.connect(":memory:"), _duck_profile())
    cur = s.execute("SELECT ? AS a", [7])
    assert [d[0] for d in cur.description] == ["a"]
    assert cur.fetchall() == [(7,)]
    s.close()


def test_execute_supports_incremental_paging():
    """mcp's query tool pages via fetchmany(offset) then fetchmany(want)."""
    s = backend.DuckSession(duckdb.connect(":memory:"), _duck_profile())
    cur = s.execute("SELECT * FROM range(10) t(n)")
    assert cur.fetchmany(3) == [(0,), (1,), (2,)]
    assert cur.fetchmany(2) == [(3,), (4,)]
    s.close()


def test_duck_session_conforms_to_the_protocols():
    s = backend.DuckSession(duckdb.connect(":memory:"), _duck_profile())
    for m in ("execute", "run", "cancel", "close", "probe"):
        assert callable(getattr(s, m))
    assert s.dialect_name == "duckdb"
    assert isinstance(s.execute("SELECT 1"), backend.Cursor)
    s.close()


# --------------------------------------------------------------------------
# AdbcSession — the native path, delegating to duck primitives

def test_adbc_session_routes_run_through_the_native_stmt(monkeypatch):
    from lakesh import duck

    seen = {}

    def fake_stmt(con, h, sql):
        seen["stmt"] = (h, sql)
        return (["c"], [(1,)])

    monkeypatch.setattr(duck, "adbc_native_stmt", fake_stmt)
    s = backend.AdbcSession(con=object(), handle=99, profile=_adbc())
    assert s.run("SELECT 1") == (["c"], [(1,)])
    assert seen["stmt"] == (99, "SELECT 1")


def test_adbc_session_refuses_bound_params():
    """The native path interpolates literals; it has no bound-param form,
    and silently ignoring params would send an unfilled statement."""
    s = backend.AdbcSession(con=object(), handle=1, profile=_adbc())
    with pytest.raises(ValueError, match="bound parameters"):
        s.execute("SELECT ?", [1])


def _adbc():
    return Profile(name="p", type="adbc", driver="snowflake", uri="x")


# --------------------------------------------------------------------------
# the factory

def test_open_session_defaults_native_for_adbc(monkeypatch):
    from lakesh import duck

    monkeypatch.setattr(duck, "connect_native",
                        lambda profile, **k: (object(), 7))
    s = backend.open_session(_adbc())
    assert isinstance(s, backend.AdbcSession)


def test_open_session_uses_attached_path_for_non_adbc(monkeypatch):
    from lakesh import duck

    monkeypatch.setattr(duck, "connect", lambda profile, **k: duckdb.connect(":memory:"))
    s = backend.open_session(_duck_profile())
    assert isinstance(s, backend.DuckSession)


def test_native_requested_on_a_non_adbc_profile_is_refused():
    with pytest.raises(ConfigError, match="native passthrough requires"):
        backend.open_session(_duck_profile(), native=True)


# --------------------------------------------------------------------------
# DBAPISession — the Python-driver path, over any PEP 249 connection
#
# Exercised against python-duckdb, which really is DB-API 2.0, so this is
# the reference proof that one adapter serves any conforming driver.

def _pyduck_profile():
    return Profile(name="p", type="python", backend="duckdb", dialect="duckdb",
                   options={"database": ":memory:"})


def test_dbapi_session_reads_via_a_cursor():
    s = backend.open_session(_pyduck_profile())
    cols, rows = s.run("SELECT 1 AS a, 2 AS b")
    assert cols == ["a", "b"] and rows == [(1, 2)]
    s.close()


def test_dbapi_write_applies_once():
    """DB-API has no adbc_scan double-exec; a write lands exactly once."""
    s = backend.open_session(_pyduck_profile())
    s.run("CREATE TABLE t (n INT)")
    s.run("INSERT INTO t VALUES (1)")
    assert s.run("SELECT count(*) FROM t")[1] == [(1,)]
    s.close()


def test_dbapi_paging_via_fetchmany():
    s = backend.open_session(_pyduck_profile())
    cur = s.execute("SELECT * FROM range(6) t(n)")
    assert cur.fetchmany(2) == [(0,), (1,)]
    assert cur.fetchmany(2) == [(2,), (3,)]
    s.close()


def test_dbapi_dialect_comes_from_the_override():
    s = backend.open_session(_pyduck_profile())
    assert s.dialect_name == "duckdb"
    s.close()


# --------------------------------------------------------------------------
# the backend registry

def test_shipped_backend_names_resolve():
    for name in ("duckdb", "snowflake", "postgres"):
        assert callable(backend._resolve_backend(name))


def test_an_unknown_backend_name_is_refused():
    with pytest.raises(ConfigError, match="unknown backend"):
        backend._resolve_backend("mystery")


def test_a_module_path_backend_is_imported():
    """A user factory joins via 'module:callable' with no packaging."""
    factory = backend._resolve_backend("duckdb:connect")   # any importable callable
    assert callable(factory)


def test_a_module_path_with_no_such_callable_is_refused():
    with pytest.raises(ConfigError, match="no callable"):
        backend._resolve_backend("duckdb:no_such_function")


def test_a_custom_factory_may_return_a_full_session(monkeypatch):
    """If a factory returns something Session-shaped, it is used as-is —
    the escape hatch for custom governance or non-SQL sources."""
    sentinel = backend.DuckSession(__import__("duckdb").connect(":memory:"),
                                   _pyduck_profile())
    import sys, types
    mod = types.ModuleType("_lakesh_test_backend")
    mod.make = lambda profile, caller=None: sentinel
    sys.modules["_lakesh_test_backend"] = mod
    prof = Profile(name="p", type="python",
                   backend="_lakesh_test_backend:make", dialect="duckdb")
    try:
        s = backend.open_session(prof)
        assert s is sentinel
    finally:
        del sys.modules["_lakesh_test_backend"]
        sentinel.close()


# --------------------------------------------------------------------------
# the Snowflake application string (agent activation is opt-in)
#
# The only strings Snowflake accepts as agent-active are its own Cortex
# Code identifiers (cortex_code_cli / cortex_code_desktop, measured), so
# activating is impersonation. lakesh is honest by default and only sends
# the marker when the operator explicitly opts in AND an agent is driving.

def _sf(**kw):
    return Profile(name="s", type="python", backend="snowflake",
                   dialect="snowflake", **kw)


def test_honest_by_default_over_mcp():
    """No opt-in: even over MCP lakesh does NOT impersonate Cortex Code."""
    app = backend._snowflake_application(_sf(), "mcp")
    assert app.startswith("lakesh/") and "cortex" not in app


def test_opt_in_activates_over_mcp():
    app = backend._snowflake_application(_sf(agent_activation=True), "mcp")
    assert app == "cortex_code_cli"


def test_env_var_opts_in_server_wide(monkeypatch):
    monkeypatch.setenv("LAKESH_SNOWFLAKE_AGENT_ACTIVATION", "1")
    assert backend._snowflake_application(_sf(), "mcp") == "cortex_code_cli"


def test_a_human_at_the_cli_is_never_impersonated():
    """Even opted in, the CLI caller stays honest — activation is about an
    agent driving, and a human marked as an agent would be masked as one."""
    app = backend._snowflake_application(_sf(agent_activation=True), "cli")
    assert app.startswith("lakesh/") and "cortex" not in app


def test_agent_activation_opt_in_reads_flag_and_env(monkeypatch):
    monkeypatch.delenv("LAKESH_SNOWFLAKE_AGENT_ACTIVATION", raising=False)
    assert backend.agent_activation_opted_in(_sf()) is False
    assert backend.agent_activation_opted_in(_sf(agent_activation=True)) is True
    monkeypatch.setenv("LAKESH_SNOWFLAKE_AGENT_ACTIVATION", "true")
    assert backend.agent_activation_opted_in(_sf()) is True


# --------------------------------------------------------------------------
# the pyiceberg backend — data-pull family
#
# Metadata from a catalog API, data scanned to Arrow and queried in a
# local DuckDB. Verified end to end against real duckicelake; these unit
# tests pin the parts that don't need a live catalog.

def test_sql_sessions_have_no_catalog_metadata():
    """The three SQL backends read metadata from information_schema, so
    they report no catalog API — the tools take the SQL path for them."""
    duck = backend.DuckSession(duckdb.connect(":memory:"), _duck_profile())
    assert duck.metadata() is None
    duck.close()
    assert backend.AdbcSession(object(), 1, _adbc()).metadata() is None
    assert backend.DBAPISession(
        duckdb.connect(":memory:"), _pyduck_profile(), dialect_name="duckdb"
    ).metadata() is None


def test_pyiceberg_is_a_shipped_backend():
    assert callable(backend._resolve_backend("pyiceberg"))


class _FakeIcebergCatalog:
    """Just enough pyiceberg Catalog surface for the metadata provider."""
    def list_namespaces(self, *a):
        return [("analytics",), ("default",)]

    def list_tables(self, ns):
        return {"analytics": [("analytics", "customers"), ("analytics", "orders")],
                "default": []}[".".join(ns)]

    def load_table(self, ident):
        import types as _t
        Field = _t.SimpleNamespace
        schema = _t.SimpleNamespace(fields=[
            Field(name="id", field_type="long", required=True),
            Field(name="email", field_type="string", required=False),
        ])
        return _t.SimpleNamespace(schema=lambda: schema)


def test_metadata_provider_reads_the_catalog_api():
    md = backend._PyicebergMetadata(_FakeIcebergCatalog())
    assert md.namespaces() == ["analytics", "default"]
    assert md.tables("analytics") == [("analytics", "customers"),
                                      ("analytics", "orders")]
    cols = md.columns("analytics", "customers")
    assert cols[0] == ("id", "long", False, 1)      # required -> not nullable
    assert cols[1] == ("email", "string", True, 2)


def test_metadata_namespaces_are_cached():
    cat = _FakeIcebergCatalog()
    md = backend._PyicebergMetadata(cat)
    calls = []
    orig = cat.list_namespaces
    cat.list_namespaces = lambda *a: (calls.append(1), orig())[1]
    md.namespaces(); md.namespaces()
    assert len(calls) == 1
