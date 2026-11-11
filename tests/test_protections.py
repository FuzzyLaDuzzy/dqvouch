"""What keeps dqvouch from harming the analysed database: a time limit per query, read-only queries enforced by the
database itself, and a warning when the user could write."""
import dataclasses

import pytest

from dqvouch.translate import check_one_select
from dqvouch.warehouse import clickhouse_query_settings, connect


def test_query_settings_follow_the_users_readonly_level():
    assert clickhouse_query_settings(0, 600) == {"readonly": 2, "max_execution_time": 600}
    assert clickhouse_query_settings(0, 0) == {"readonly": 2}
    assert clickhouse_query_settings(2, 600) == {"max_execution_time": 600}
    assert clickhouse_query_settings(2, 0) == {}
    assert clickhouse_query_settings(1, 600) == {}  # a readonly=1 user cannot receive any setting


def test_clickhouse_refuses_anything_but_one_select():
    for bad in ("DROP TABLE landing.orders_raw", "SELECT 1; SELECT 2", "INSERT INTO t SELECT 1"):
        with pytest.raises(ValueError):
            check_one_select(bad)
    check_one_select("SELECT 1")
    check_one_select("WITH a AS (SELECT 1 AS x) SELECT x FROM a")
    check_one_select("SELECT ¤¤ this is not sql sqlglot can parse")  # the database decides; it is read-only


def test_a_slow_query_hits_the_time_limit_and_the_next_one_works(demo_settings, wh):
    db = dataclasses.replace(demo_settings.database, query_timeout_seconds=1)
    slow = "SELECT pg_sleep(3)" if db.engine == "postgres" else "SELECT sleepEachRow(1) FROM numbers(3)"
    limited = connect(db)
    try:
        with pytest.raises(Exception):
            limited.query(slow)
        assert limited.query("SELECT 1")[1] == [(1,)]
    finally:
        limited.close()


def test_clickhouse_queries_run_read_only(demo_settings, wh):
    if demo_settings.database.engine != "clickhouse":
        pytest.skip("PostgreSQL sessions are read-only already (test_postgres.py)")
    assert int(wh.query("SELECT getSetting('readonly')")[1][0][0]) == 2


def test_write_access_is_detected(demo_settings, wh, reader_wh):
    schemas = demo_settings.database.schemas
    assert wh.can_write(schemas) is True
    assert reader_wh.can_write(schemas) is False
    assert reader_wh.query("SELECT count() FROM landing.orders_raw" if demo_settings.database.engine == "clickhouse"
                           else "SELECT count(*) FROM landing.orders_raw")[1][0][0] > 0
