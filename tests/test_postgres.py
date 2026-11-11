import pytest

from conftest import DEMO_CONFIGS, EXAMPLE
from dqvouch.warehouse import canonical_type


@pytest.mark.parametrize("pg_type,nullable,expected", [
    ("integer", False, "Int32"),
    ("bigint", True, "Nullable(Int64)"),
    ("character varying(20)", True, "Nullable(String)"),
    ("numeric(10,2)", False, "Decimal(10, 2)"),
    ("double precision", False, "Float64"),
    ("boolean", False, "Bool"),
    ("date", True, "Nullable(Date)"),
    ("timestamp with time zone", False, "DateTime"),
    ("text[]", True, "Array(String)"),
    ("uuid", False, "UUID"),
    ("tsvector", False, "tsvector"),
])
def test_canonical_type(pg_type, nullable, expected):
    assert canonical_type(pg_type, nullable) == expected


@pytest.fixture
def demo_config_path():
    """This module tests the PostgreSQL connection only, so the demo runs on PostgreSQL alone here."""
    return EXAMPLE / DEMO_CONFIGS["postgres"]


@pytest.fixture
def pg(wh):
    return wh


def test_postgres_metadata_uses_internal_types(pg, demo_settings):
    rows = [r for r in pg.fetch_metadata(demo_settings.database.schemas)
            if (r["database"], r["table"]) == ("landing", "orders_raw")]
    types = {r["column_name"]: r["data_type"] for r in rows}
    assert types["order_id"] == "Nullable(Int64)"
    assert types["status"] == "String"
    assert types["order_date"] == "Date"
    assert all(r["engine"] == "PostgreSQL table" and r["table_description"] for r in rows)


def test_postgres_is_read_only_and_recovers(pg):
    with pytest.raises(ValueError, match="one SELECT"):  # dqvouch itself only sends one SELECT
        pg.query("INSERT INTO landing.orders_raw (status) VALUES ('x')")
    with pytest.raises(Exception, match="read-only"):  # and the server refuses a write on this connection anyway
        pg._conn.execute("INSERT INTO landing.orders_raw (status) VALUES ('x')")
    assert pg.query("SELECT 1 AS x") == (["x"], [(1,)])


def test_postgres_decimals_become_floats(pg):
    assert isinstance(pg.query("SELECT round(1.25, 1) AS x")[1][0][0], float)


def test_injected_statements_cannot_turn_off_read_only(pg):
    from dqvouch.render_sql import generate_sql_from_test_specifications, run_tests
    specs = [{"id": "t", "asset": "landing.orders_raw", "title": "x\nSET default_transaction_read_only = off;",
              "assertion": {"type": "unique", "columns": ["order_id"]},
              "threshold": {"metric": "failed_rows FROM landing.orders_raw; SET default_transaction_read_only = off; "
                                      "SELECT 0 AS failed_rows", "operator": "equals", "value": 0}}]
    run_tests(pg, generate_sql_from_test_specifications(specs), {"t": specs[0]})
    with pytest.raises(ValueError, match="one SELECT"):
        pg.query("SELECT 1; SET default_transaction_read_only = off")
    assert pg.query("SELECT current_setting('default_transaction_read_only') AS v")[1] == [("on",)]


def test_postgres_survives_a_dropped_connection(pg, demo_settings):
    from dqvouch.warehouse import connect
    pid = pg.query("SELECT pg_backend_pid() AS pid")[1][0][0]
    other = connect(demo_settings.database)
    try:
        other.query(f"SELECT pg_terminate_backend({pid}, 5000) AS ok")  # what a server restart or a pooler does
    finally:
        other.close()
    assert pg.query("SELECT 1 AS x") == (["x"], [(1,)])


def test_partitions_are_not_tables(pg, demo_settings):
    rows = pg.fetch_metadata(demo_settings.database.schemas)
    staging = [r for r in rows if r["database"] == "staging"]
    assert {r["table"] for r in staging} == {"orders_trans", "customers", "returns"}  # not orders_trans_2026_10...
    assert all(r["engine"] == "PostgreSQL partitioned table" and r["partition_key"] == "RANGE (order_date)"
               for r in staging if r["table"] == "orders_trans")
