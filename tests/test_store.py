import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from dqvouch.config import DatabaseSettings, Settings
from dqvouch.pipeline import stage_context, stage_dimensions, stage_sql_rules
from dqvouch.profiling import run_profiling
from dqvouch.render_sql import check_metric_thresholds
from dqvouch.store import Run, RunBusyError


def settings(**kw):
    return Settings(database=DatabaseSettings(schemas=["db"], password="s3cret"), **kw)


def test_save_load_latest_and_truncation(tmp_path):
    run = Run.create(tmp_path, settings())
    run.save("context", {"asset_contexts": {"a": 1}})
    run.save("dimensions", {"dimensions": {"d": 1}})
    run.save("profiling", {"dimensions": {"d": 2}})
    assert run.latest("dimensions") == {"d": 2}
    assert run.completed() == ["context", "dimensions", "profiling"]
    run.save("dimensions", {"dimensions": {"d": 3}})
    assert run.completed() == ["context", "dimensions"]
    assert run.latest("nope", None) is None
    with pytest.raises(KeyError):
        run.latest("nope")


def test_json_default_handles_decimal_and_dates(tmp_path):
    run = Run.create(tmp_path, settings())
    run.save("context", {"x": Decimal("1.5"), "d": date(2026, 11, 8)})
    assert run.load("context") == {"x": 1.5, "d": "2026-11-08"}


def test_run_json_has_no_password(tmp_path):
    run = Run.create(tmp_path, settings())
    assert "s3cret" not in (run.path / "run.json").read_text(encoding="utf-8")
    assert Run.open(run.path).info()["settings"]["database"]["schemas"] == ["db"]


def test_run_ids_do_not_collide(tmp_path):
    assert Run.create(tmp_path, settings()).id != Run.create(tmp_path, settings()).id


class FakeWarehouse:
    def __init__(self, metadata=()):
        self.metadata = list(metadata)

    def fetch_metadata(self, schemas):
        return self.metadata

    def query(self, sql):
        if "FAILME" in sql:
            raise RuntimeError("boom")
        return ["total_rows"], [(5,)]


def test_run_profiling_keeps_going_after_a_failure():
    sql = "-- CHECK_ID: completeness:db.t:a\nSELECT 1;\n-- CHECK_ID: completeness:db.t:b\nSELECT FAILME;\n"
    results, errors = run_profiling(FakeWarehouse(), sql)
    assert [r["check_id"] for r in results] == ["completeness:db.t:a"]
    assert errors[0]["check_id"] == "completeness:db.t:b" and "boom" in errors[0]["error"]


def test_check_metric_thresholds_verdicts():
    profiling_results = [{"check_id": "c", "metrics": {"m": 5}}]
    threshold = {"id": "t", "asset": "db.t", "check_id": "c", "metric": "m", "operator": "less_than", "value": 10}
    assert check_metric_thresholds(profiling_results, [threshold])[0]["verdict"] == "pass"
    assert check_metric_thresholds(profiling_results, [dict(threshold, check_id="zz")])[0]["verdict"] == "error"


def test_stage_context_without_tables_raises(tmp_path):
    run = Run.create(tmp_path, settings())
    with pytest.raises(ValueError, match="no tables found"):
        stage_context(run, FakeWarehouse([]))


CTX = {"db.t": {"asset": {"name": "db.t"}, "columns": [], "transformation": {"raw_sql": "unknown"}}}


def test_stage_dimensions_bad_json_is_not_saved(tmp_path):
    run = Run.create(tmp_path, settings())
    run.save("context", {"asset_contexts": CTX})
    with pytest.raises(ValueError):
        stage_dimensions(run, lambda prompt: "garbage")
    assert "dimensions" not in run.completed()


def test_stage_sql_rules_skips_without_sql(tmp_path):
    run = Run.create(tmp_path, settings())
    run.save("context", {"asset_contexts": CTX})
    run.save("dimensions", {"dimensions": {"tables": [], "cross_table_checks": []}})

    def llm(prompt):
        raise AssertionError("the LLM must not be called without SQL")

    assert "skipped" in stage_sql_rules(run, llm)


def test_stage_tests_requires_sql_rules_stage(tmp_path):
    from dqvouch.pipeline import stage_tests
    run = Run.create(tmp_path, settings())
    run.save("context", {"asset_contexts": CTX})
    run.save("dimensions", {"dimensions": {"tables": [], "cross_table_checks": []}})
    run.save("profiling", {"dimensions": {"tables": [], "cross_table_checks": []}, "profiling_summary": {},
                           "profiling_results": []})
    with pytest.raises(ValueError, match="sql_rules"):
        stage_tests(run, lambda prompt: "", FakeWarehouse())


# --- one execution of a run at a time, across processes (the interface, a schedule, `dqvouch rerun` from cron)

def _lock_run(tmp_path):
    return Run.create(tmp_path, Settings(database=DatabaseSettings(schemas=["landing"])))


def test_one_execution_at_a_time(tmp_path):
    run = _lock_run(tmp_path)
    assert not run.locked()
    with run.execution_lock():
        assert run.locked()
        with pytest.raises(RunBusyError, match="execution.lock"):
            with run.execution_lock():
                pass
    assert not run.locked()
    with run.execution_lock():  # free again
        pass


def test_a_lock_older_than_a_day_is_replaced(tmp_path):
    """A process killed in the middle leaves its lock behind: it blocks for a day at most."""
    run = _lock_run(tmp_path)
    old = datetime.now(timezone.utc) - timedelta(hours=25)
    (run.path / "execution.lock").write_text(json.dumps({"pid": 1, "at": old.isoformat()}), encoding="utf-8")
    assert not run.locked()
    with run.execution_lock():
        assert run.locked()
