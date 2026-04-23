"""Unit tests for config loading. Runs without any external services."""
from __future__ import annotations

from pathlib import Path

import pytest

from lakesh.config import (
    ConfigError,
    default_config_path,
    load_config,
    write_example_config,
)


def _write(tmp: Path, body: str) -> Path:
    p = tmp / "config.toml"
    p.write_text(body)
    return p


def test_minimal_profile_parses(tmp_path: Path):
    p = _write(tmp_path, """
default = "a"

[profiles.a]
uri = "http://localhost:8181"
warehouse = "lake"
""")
    cfg = load_config(p)
    assert cfg.default == "a"
    assert cfg.get("a").uri == "http://localhost:8181"
    assert cfg.get("a").warehouse == "lake"
    assert cfg.get(None).name == "a"


def test_s3_and_oauth_blocks(tmp_path: Path):
    p = _write(tmp_path, """
[profiles.p]
uri = "http://h:1"
warehouse = "w"

[profiles.p.s3]
endpoint   = "http://s3:9000"
region     = "eu-west-1"
access_key = "ak"
secret_key = "sk"
path_style = false

[profiles.p.oauth]
client_id = "cid"
client_secret = "cs"
""")
    prof = load_config(p).get("p")
    assert prof.s3.endpoint == "http://s3:9000"
    assert prof.s3.region == "eu-west-1"
    assert prof.s3.access_key == "ak"
    assert prof.s3.path_style is False
    assert prof.oauth.enabled
    assert prof.oauth.client_id == "cid"


def test_env_indirection(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LAKESH_TEST_CID", "env_client")
    monkeypatch.setenv("LAKESH_TEST_SEC", "env_secret")
    p = _write(tmp_path, """
[profiles.p]
uri = "http://h:1"
warehouse = "w"

[profiles.p.oauth]
client_id_env     = "LAKESH_TEST_CID"
client_secret_env = "LAKESH_TEST_SEC"
""")
    prof = load_config(p).get("p")
    assert prof.oauth.client_id == "env_client"
    assert prof.oauth.client_secret == "env_secret"


def test_literal_wins_over_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LAKESH_TEST_CID", "env_client")
    p = _write(tmp_path, """
[profiles.p]
uri = "http://h:1"
warehouse = "w"

[profiles.p.oauth]
client_id     = "literal"
client_id_env = "LAKESH_TEST_CID"
client_secret = "whatever"
""")
    prof = load_config(p).get("p")
    assert prof.oauth.client_id == "literal"


def test_missing_uri_errors(tmp_path: Path):
    p = _write(tmp_path, """
[profiles.p]
warehouse = "w"
""")
    with pytest.raises(ConfigError, match="missing `uri`"):
        load_config(p)


def test_missing_warehouse_errors(tmp_path: Path):
    p = _write(tmp_path, """
[profiles.p]
uri = "http://h:1"
""")
    with pytest.raises(ConfigError, match="missing `warehouse`"):
        load_config(p)


def test_unknown_type_errors(tmp_path: Path):
    p = _write(tmp_path, """
[profiles.p]
type = "hive"
uri = "http://h:1"
warehouse = "w"
""")
    with pytest.raises(ConfigError, match="unknown type"):
        load_config(p)


def test_default_pointing_at_missing_profile_errors(tmp_path: Path):
    p = _write(tmp_path, """
default = "nope"

[profiles.p]
uri = "http://h:1"
warehouse = "w"
""")
    with pytest.raises(ConfigError, match="no such profile"):
        load_config(p)


def test_unknown_profile_lookup(tmp_path: Path):
    p = _write(tmp_path, """
[profiles.p]
uri = "http://h:1"
warehouse = "w"
""")
    cfg = load_config(p)
    with pytest.raises(ConfigError, match="not found"):
        cfg.get("missing")


def test_get_without_default_errors(tmp_path: Path):
    p = _write(tmp_path, """
[profiles.p]
uri = "http://h:1"
warehouse = "w"
""")
    cfg = load_config(p)
    with pytest.raises(ConfigError, match="no profile specified"):
        cfg.get(None)


def test_config_missing_path_errors(tmp_path: Path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")


def test_malformed_toml_errors(tmp_path: Path):
    p = _write(tmp_path, "this is not = valid = toml ===")
    with pytest.raises(ConfigError, match="failed to parse"):
        load_config(p)


def test_write_example_config_is_parseable(tmp_path: Path):
    """The example we emit via `lakesh config init` must itself be valid."""
    p = tmp_path / "nested" / "config.toml"
    write_example_config(p)
    assert p.exists()
    cfg = load_config(p)
    # Example default is "local" — must exist + parse cleanly.
    assert cfg.default == "local"
    local = cfg.get("local")
    assert local.uri.startswith("http://127.0.0.1")
    assert local.warehouse == "lake"


def test_default_path_honors_env(monkeypatch):
    monkeypatch.setenv("LAKESH_CONFIG", "/etc/lakesh/override.toml")
    assert str(default_config_path()) == "/etc/lakesh/override.toml"


def test_default_path_falls_back_to_xdg(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("LAKESH_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert default_config_path() == tmp_path / "lakesh" / "config.toml"


# --------------------------------------------------------------------------
# DuckLake profile type


def test_ducklake_profile_parses(tmp_path: Path):
    p = _write(tmp_path, """
[profiles.lake]
type         = "ducklake"
postgres_dsn = "dbname=ducklake host=/tmp/.pgsock port=55432 user=ducklake"
data_path    = "s3://lakehouse/data/"
catalog      = "lake"

[profiles.lake.s3]
endpoint   = "http://127.0.0.1:9000"
access_key = "minioadmin"
secret_key = "minioadmin"
""")
    prof = load_config(p).get("lake")
    assert prof.type == "ducklake"
    assert prof.postgres_dsn.startswith("dbname=ducklake")
    assert prof.data_path == "s3://lakehouse/data/"
    assert prof.catalog == "lake"
    assert prof.s3.endpoint == "http://127.0.0.1:9000"


def test_ducklake_missing_dsn_errors(tmp_path: Path):
    p = _write(tmp_path, """
[profiles.lake]
type      = "ducklake"
data_path = "s3://b/p/"
""")
    with pytest.raises(ConfigError, match="postgres_dsn"):
        load_config(p)


def test_ducklake_missing_data_path_errors(tmp_path: Path):
    p = _write(tmp_path, """
[profiles.lake]
type         = "ducklake"
postgres_dsn = "dbname=x host=/tmp user=u"
""")
    with pytest.raises(ConfigError, match="data_path"):
        load_config(p)


def test_ducklake_catalog_defaults_to_lake(tmp_path: Path):
    p = _write(tmp_path, """
[profiles.p]
type         = "ducklake"
postgres_dsn = "dbname=x host=/tmp user=u"
data_path    = "s3://b/p/"
""")
    assert load_config(p).get("p").catalog == "lake"


def test_ducklake_dsn_via_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LAKESH_TEST_DSN", "dbname=env_db host=/tmp user=env_u")
    p = _write(tmp_path, """
[profiles.p]
type             = "ducklake"
postgres_dsn_env = "LAKESH_TEST_DSN"
data_path        = "s3://b/p/"
""")
    assert load_config(p).get("p").postgres_dsn.startswith("dbname=env_db")


def test_unknown_type_still_errors(tmp_path: Path):
    """Regression guard: adding ducklake didn't open the door to any
    other type silently being accepted."""
    p = _write(tmp_path, """
[profiles.p]
type = "snowflake"
uri  = "https://x.snowflakecomputing.com"
warehouse = "w"
""")
    with pytest.raises(ConfigError, match="unknown type"):
        load_config(p)
