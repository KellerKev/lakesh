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


# --------------------------------------------------------------------------
# adbc profiles

def test_adbc_profile_parses(tmp_path: Path):
    p = _write(tmp_path, """
[profiles.pg]
type      = "adbc"
driver    = "postgresql"
uri       = "postgresql://db:5432/app"
catalog   = "pg"
read_only = true
token_option = "auth_token"

[profiles.pg.options]
username = "u"
password = "pw"
"adbc.postgresql.some_key" = "v"
""")
    prof = load_config(p).get("pg")
    assert prof.type == "adbc"
    assert prof.driver == "postgresql"
    assert prof.uri == "postgresql://db:5432/app"
    assert prof.catalog == "pg"
    assert prof.read_only is True
    assert prof.token_option == "auth_token"
    assert prof.options == {
        "username": "u",
        "password": "pw",
        "adbc.postgresql.some_key": "v",
    }


def test_adbc_catalog_defaults_to_src(tmp_path: Path):
    p = _write(tmp_path, """
[profiles.a]
type   = "adbc"
driver = "sqlite"
uri    = "/tmp/x.db"
""")
    prof = load_config(p).get("a")
    assert prof.catalog == "src"
    assert prof.read_only is False


def test_adbc_missing_driver_errors(tmp_path: Path):
    p = _write(tmp_path, """
[profiles.a]
type = "adbc"
uri  = "/tmp/x.db"
""")
    with pytest.raises(ConfigError, match="requires `driver`"):
        load_config(p)


def test_adbc_bad_catalog_identifier_errors(tmp_path: Path):
    p = _write(tmp_path, """
[profiles.a]
type    = "adbc"
driver  = "sqlite"
uri     = "/tmp/x.db"
catalog = "bad-name; DROP"
""")
    with pytest.raises(ConfigError, match="plain identifier"):
        load_config(p)


def test_adbc_options_env_indirection(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LAKESH_TEST_PW", "s3cret")
    p = _write(tmp_path, """
[profiles.a]
type   = "adbc"
driver = "postgresql"
uri    = "postgresql://h:5432/db"

[profiles.a.options]
username     = "u"
password_env = "LAKESH_TEST_PW"
""")
    prof = load_config(p).get("a")
    assert prof.options["password"] == "s3cret"
    assert "password_env" not in prof.options


def test_adbc_options_literal_wins_over_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LAKESH_TEST_PW", "from_env")
    p = _write(tmp_path, """
[profiles.a]
type   = "adbc"
driver = "postgresql"
uri    = "postgresql://h:5432/db"

[profiles.a.options]
password     = "literal"
password_env = "LAKESH_TEST_PW"
""")
    assert load_config(p).get("a").options["password"] == "literal"


def test_adbc_oauth_without_token_option_errors(tmp_path: Path):
    p = _write(tmp_path, """
[profiles.a]
type   = "adbc"
driver = "snowflake"

[profiles.a.oauth]
grant                         = "device_code"
client_id                     = "cid"
device_authorization_endpoint = "https://idp/device"
token_endpoint                = "https://idp/token"
""")
    with pytest.raises(ConfigError, match="token_option"):
        load_config(p)


def test_adbc_oauth_with_token_placeholder_ok(tmp_path: Path):
    p = _write(tmp_path, """
[profiles.a]
type   = "adbc"
driver = "flightsql"
uri    = "grpc://h:31337"

[profiles.a.options]
"adbc.flight.sql.authorization_header" = "Bearer {token}"

[profiles.a.oauth]
grant                         = "device_code"
client_id                     = "cid"
device_authorization_endpoint = "https://idp/device"
token_endpoint                = "https://idp/token"
""")
    prof = load_config(p).get("a")
    assert prof.oauth.grant == "device_code"


# --------------------------------------------------------------------------
# oauth grants

def test_oauth_backwards_compat_two_field_block(tmp_path: Path):
    """Old configs with only client_id + client_secret keep working:
    grant defaults to client_credentials and enabled stays True."""
    p = _write(tmp_path, """
[profiles.p]
uri = "http://h:1"
warehouse = "w"

[profiles.p.oauth]
client_id = "cid"
client_secret = "cs"
""")
    o = load_config(p).get("p").oauth
    assert o.grant == "client_credentials"
    assert o.enabled
    assert o.token_endpoint is None


def test_oauth_client_id_only_stays_disabled_for_cc(tmp_path: Path):
    p = _write(tmp_path, """
[profiles.p]
uri = "http://h:1"
warehouse = "w"

[profiles.p.oauth]
client_id = "cid"
""")
    assert not load_config(p).get("p").oauth.enabled


def test_oauth_full_fields_parse(tmp_path: Path):
    p = _write(tmp_path, """
[profiles.p]
uri = "http://h:1"
warehouse = "w"

[profiles.p.oauth]
grant          = "authorization_code"
client_id      = "cid"
authorization_endpoint = "https://idp/authorize"
token_endpoint = "https://idp/token"
scope          = "openid offline_access"
audience       = "https://api"
redirect_port  = 8912

[profiles.p.oauth.extra]
resource = "urn:x"
""")
    o = load_config(p).get("p").oauth
    assert o.grant == "authorization_code"
    assert o.enabled
    assert o.scope == "openid offline_access"
    assert o.audience == "https://api"
    assert o.redirect_port == 8912
    assert o.extra == {"resource": "urn:x"}


def test_oauth_unknown_grant_errors(tmp_path: Path):
    p = _write(tmp_path, """
[profiles.p]
uri = "http://h:1"
warehouse = "w"

[profiles.p.oauth]
grant     = "implicit"
client_id = "cid"
""")
    with pytest.raises(ConfigError, match="unknown oauth grant"):
        load_config(p)


def test_device_code_requires_device_endpoint(tmp_path: Path):
    p = _write(tmp_path, """
[profiles.p]
uri = "http://h:1"
warehouse = "w"

[profiles.p.oauth]
grant          = "device_code"
client_id      = "cid"
token_endpoint = "https://idp/token"
""")
    with pytest.raises(ConfigError, match="device_authorization_endpoint"):
        load_config(p)


def test_authorization_code_requires_auth_endpoint(tmp_path: Path):
    p = _write(tmp_path, """
[profiles.p]
uri = "http://h:1"
warehouse = "w"

[profiles.p.oauth]
grant          = "authorization_code"
client_id      = "cid"
token_endpoint = "https://idp/token"
""")
    with pytest.raises(ConfigError, match="authorization_endpoint"):
        load_config(p)


def test_interactive_grant_requires_token_endpoint(tmp_path: Path):
    p = _write(tmp_path, """
[profiles.p]
uri = "http://h:1"
warehouse = "w"

[profiles.p.oauth]
grant                         = "device_code"
client_id                     = "cid"
device_authorization_endpoint = "https://idp/device"
""")
    with pytest.raises(ConfigError, match="token_endpoint"):
        load_config(p)


def test_cc_iceberg_rest_token_endpoint_optional(tmp_path: Path):
    """client_credentials on iceberg-rest keeps defaulting to the
    catalog's own /v1/oauth/tokens — no token_endpoint needed."""
    p = _write(tmp_path, """
[profiles.p]
uri = "http://h:1"
warehouse = "w"

[profiles.p.oauth]
client_id     = "cid"
client_secret = "cs"
""")
    assert load_config(p).get("p").oauth.enabled


def test_adbc_uri_via_env(tmp_path: Path, monkeypatch):
    """Drivers like postgresql only accept credentials embedded in the
    URI — uri_env keeps the password-bearing DSN out of the file."""
    monkeypatch.setenv("LAKESH_TEST_URI_DSN", "postgresql://u:pw@h:5432/db")
    p = _write(tmp_path, """
[profiles.a]
type    = "adbc"
driver  = "postgresql"
uri_env = "LAKESH_TEST_URI_DSN"
""")
    assert load_config(p).get("a").uri == "postgresql://u:pw@h:5432/db"


# --------------------------------------------------------------------------
# query_timeout_s


def test_query_timeout_parses(tmp_path):
    p = _write(tmp_path, """
default = "pg"

[profiles.pg]
type            = "adbc"
driver          = "postgresql"
uri             = "postgresql://u@h/db"
query_timeout_s = 45
""")
    assert load_config(p).get("pg").query_timeout_s == 45.0


def test_query_timeout_defaults_to_unset(tmp_path):
    """Unset means "accept the server's default", not "no deadline" —
    the two have to stay distinguishable for the ceiling rule to work."""
    p = _write(tmp_path, """
default = "pg"

[profiles.pg]
type   = "adbc"
driver = "postgresql"
uri    = "postgresql://u@h/db"
""")
    assert load_config(p).get("pg").query_timeout_s is None


@pytest.mark.parametrize("value", ["0", "-1"])
def test_query_timeout_rejects_non_positive(tmp_path, value):
    p = _write(tmp_path, f"""
default = "pg"

[profiles.pg]
type            = "adbc"
driver          = "postgresql"
uri             = "postgresql://u@h/db"
query_timeout_s = {value}
""")
    with pytest.raises(ConfigError) as e:
        load_config(p)
    assert "query_timeout_s" in str(e.value) and "pg" in str(e.value)


def test_query_timeout_rejects_non_numeric(tmp_path):
    """Coerced at parse time so the error names the profile, rather than
    a bare ValueError escaping from the dataclass construction."""
    p = _write(tmp_path, """
default = "pg"

[profiles.pg]
type            = "adbc"
driver          = "postgresql"
uri             = "postgresql://u@h/db"
query_timeout_s = "half an hour"
""")
    with pytest.raises(ConfigError) as e:
        load_config(p)
    assert "must be a number" in str(e.value)


# --------------------------------------------------------------------------
# table annotations


_ANNOTATED = """
default = "snow"

[profiles.snow]
type          = "adbc"
driver        = "snowflake"
uri           = "u:p@ACC"
catalog       = "snow"
status        = "canonical"
max_staleness = "24h"

[profiles.snow.tables]
"ANALYTICS.FCT_REVENUE"    = {{ status = "canonical", max_staleness = "6h", note = "truth" }}
"ANALYTICS.FCT_REVENUE_V1" = {{ status = "deprecated", superseded_by = "ANALYTICS.FCT_REVENUE" }}
"ANALYTICS.*"              = {{ max_staleness = "2d" }}
"ANALYTICS.DIM_*"          = {{ max_staleness = "7d" }}
"""


def test_table_annotations_parse(tmp_path):
    prof = load_config(_write(tmp_path, _ANNOTATED.format())).get("snow")
    assert prof.status == "canonical"
    assert prof.max_staleness_seconds == 86400
    ann = prof.tables["ANALYTICS.FCT_REVENUE"]
    assert ann.status == "canonical"
    assert ann.max_staleness_seconds == 21600
    assert ann.note == "truth"


def test_annotation_lookup_prefers_the_most_specific(tmp_path):
    """Specificity, not file order: an exact key beats a glob and a
    longer glob beats a shorter one, so ANALYTICS.DIM_* wins over
    ANALYTICS.* however they were written."""
    prof = load_config(_write(tmp_path, _ANNOTATED.format())).get("snow")

    assert prof.annotation_for("ANALYTICS", "FCT_REVENUE").pattern == "ANALYTICS.FCT_REVENUE"
    assert prof.annotation_for("ANALYTICS", "DIM_CUSTOMER").pattern == "ANALYTICS.DIM_*"
    assert prof.annotation_for("ANALYTICS", "OTHER").pattern == "ANALYTICS.*"
    assert prof.annotation_for("STAGING", "X") is None


def test_annotation_lookup_is_case_insensitive_both_ways(tmp_path):
    """Snowflake upper-cases unquoted identifiers and Postgres
    lower-cases them, so the same logical table is spelled two ways
    depending on which source you ask."""
    prof = load_config(_write(tmp_path, _ANNOTATED.format())).get("snow")
    assert prof.annotation_for("analytics", "fct_revenue").status == "canonical"

    lower = load_config(_write(tmp_path, """
default = "pg"

[profiles.pg]
type   = "adbc"
driver = "postgresql"
uri    = "postgresql://u@h/db"

[profiles.pg.tables]
"public.orders" = { status = "canonical" }
""")).get("pg")
    assert lower.annotation_for("PUBLIC", "ORDERS").status == "canonical"


def test_unknown_status_is_rejected(tmp_path):
    p = _write(tmp_path, """
default = "pg"

[profiles.pg]
type   = "adbc"
driver = "postgresql"
uri    = "postgresql://u@h/db"

[profiles.pg.tables]
"public.orders" = { status = "blessed" }
""")
    with pytest.raises(ConfigError) as e:
        load_config(p)
    assert "blessed" in str(e.value) and "public.orders" in str(e.value)


def test_unknown_annotation_key_is_rejected(tmp_path):
    """The worst failure mode for a governance feature is a typo that
    parses clean: the operator believes the table is marked and it never
    matches anything."""
    p = _write(tmp_path, """
default = "pg"

[profiles.pg]
type   = "adbc"
driver = "postgresql"
uri    = "postgresql://u@h/db"

[profiles.pg.tables]
"public.orders" = { max_stalenes = "24h" }
""")
    with pytest.raises(ConfigError) as e:
        load_config(p)
    assert "max_stalenes" in str(e.value)


def test_table_key_must_be_qualified(tmp_path):
    """An unquoted ANALYTICS.FCT_ORDERS is TOML dotted-key syntax and
    nests instead of becoming a literal key, so the resulting annotation
    would silently never match."""
    p = _write(tmp_path, """
default = "pg"

[profiles.pg]
type   = "adbc"
driver = "postgresql"
uri    = "postgresql://u@h/db"

[profiles.pg.tables]
orders = { status = "canonical" }
""")
    with pytest.raises(ConfigError) as e:
        load_config(p)
    assert "SCHEMA.TABLE" in str(e.value)


def test_bad_duration_in_an_annotation_names_the_table(tmp_path):
    p = _write(tmp_path, """
default = "pg"

[profiles.pg]
type   = "adbc"
driver = "postgresql"
uri    = "postgresql://u@h/db"

[profiles.pg.tables]
"public.orders" = { max_staleness = "24" }
""")
    with pytest.raises(ConfigError) as e:
        load_config(p)
    assert "public.orders" in str(e.value) and "duration" in str(e.value)


def test_profiles_without_annotations_are_unchanged(tmp_path):
    prof = load_config(_write(tmp_path, """
default = "pg"

[profiles.pg]
type   = "adbc"
driver = "postgresql"
uri    = "postgresql://u@h/db"
""")).get("pg")
    assert prof.tables == {}
    assert prof.status == "unknown"
    assert prof.max_staleness_seconds is None
    assert prof.annotation_for("public", "orders") is None


def test_describe_table_shape_defaults_to_object(tmp_path):
    cfg = load_config(_write(tmp_path, """
default = "pg"

[profiles.pg]
type   = "adbc"
driver = "postgresql"
uri    = "postgresql://u@h/db"
"""))
    assert cfg.describe_table_shape == "object"


def test_describe_table_shape_parses(tmp_path):
    cfg = load_config(_write(tmp_path, """
default = "pg"
describe_table_shape = "array"

[profiles.pg]
type   = "adbc"
driver = "postgresql"
uri    = "postgresql://u@h/db"
"""))
    assert cfg.describe_table_shape == "array"


def test_describe_table_shape_rejects_unknown(tmp_path):
    p = _write(tmp_path, """
default = "pg"
describe_table_shape = "tuple"

[profiles.pg]
type   = "adbc"
driver = "postgresql"
uri    = "postgresql://u@h/db"
""")
    with pytest.raises(ConfigError) as e:
        load_config(p)
    assert "tuple" in str(e.value) and "object, array" in str(e.value)


# --------------------------------------------------------------------------
# masking config


def test_masking_parses(tmp_path):
    cfg = load_config(_write(tmp_path, """
default = "pg"

[masking]
mode  = "mask"
rules = ["pii.email", "pii.ip"]

[profiles.pg]
type   = "adbc"
driver = "postgresql"
uri    = "postgresql://u@h/db"
"""))
    assert cfg.masking_mode == "mask"
    assert cfg.masking_rules == ("pii.email", "pii.ip")


def test_masking_defaults_to_off(tmp_path):
    cfg = load_config(_write(tmp_path, """
default = "pg"

[profiles.pg]
type   = "adbc"
driver = "postgresql"
uri    = "postgresql://u@h/db"
"""))
    assert cfg.masking_mode == "off"
    assert cfg.masking_rules is None


def test_unknown_masking_key_is_rejected(tmp_path):
    """Same reasoning as the table-annotation guard: for a governance
    feature a silently-ignored typo is the worst failure, because the
    operator believes the protection is on."""
    with pytest.raises(ConfigError) as e:
        load_config(_write(tmp_path, """
default = "pg"

[masking]
mode  = "mask"
ruels = ["pii.email"]

[profiles.pg]
type   = "adbc"
driver = "postgresql"
uri    = "postgresql://u@h/db"
"""))
    assert "ruels" in str(e.value)


def test_unknown_masking_mode_is_rejected(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_config(_write(tmp_path, """
default = "pg"

[masking]
mode = "redact"

[profiles.pg]
type   = "adbc"
driver = "postgresql"
uri    = "postgresql://u@h/db"
"""))
    assert "redact" in str(e.value) and "off, mask, audit" in str(e.value)


def test_unknown_rule_label_is_rejected(tmp_path):
    """A rule you thought you enabled and didn't is the same failure as a
    typo'd key."""
    with pytest.raises(ConfigError) as e:
        load_config(_write(tmp_path, """
default = "pg"

[masking]
rules = ["pii.emai"]

[profiles.pg]
type   = "adbc"
driver = "postgresql"
uri    = "postgresql://u@h/db"
"""))
    assert "pii.emai" in str(e.value)


def test_profile_masking_overrides_global(tmp_path):
    cfg = load_config(_write(tmp_path, """
default = "pg"

[masking]
mode = "audit"

[profiles.pg]
type   = "adbc"
driver = "postgresql"
uri    = "postgresql://u@h/db"

[profiles.pg.masking]
mode = "mask"
"""))
    assert cfg.masking_mode == "audit"
    assert cfg.get("pg").masking_mode == "mask"
