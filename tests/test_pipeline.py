from fake_llm import FakeLLM

from dqvouch.config import DatabaseSettings, Settings
from dqvouch.pipeline import (
    earlier_decisions, signature,
    stage_context, stage_dimensions, stage_profiling, stage_results, stage_review, stage_sql_rules, stage_tests,
)
from dqvouch.profiling import generate_sql_from_dq_json, select_checks
from dqvouch.store import STAGES, Run


def test_full_pipeline_on_demo(tmp_path, demo_settings, wh):
    demo_settings.runs_dir = str(tmp_path)
    run, llm = Run.create(tmp_path, demo_settings), FakeLLM()
    stage_context(run, wh)
    stage_dimensions(run, llm)
    stage_profiling(run, wh)
    stage_sql_rules(run, llm)
    t = stage_tests(run, llm, wh)
    stage_review(run, t["test_specifications"], t["metric_thresholds"])
    rows = stage_results(run, wh)["rows"]
    verdict = {r["check_id"].split("__")[-1]: r["verdict"] for r in rows}
    assert verdict["orders_trans_order_id_unique"] == "fail"
    assert verdict["fact_orders_status_domain"] == "fail"
    assert run.completed() == list(STAGES)
    assert run.load("dimensions")["llm_calls"] == 1
    assert all(r["sql"].strip() for r in rows)


def test_stages_report_progress(tmp_path, demo_settings, wh):
    import re
    demo_settings.runs_dir = str(tmp_path)
    run, llm, messages = Run.create(tmp_path, demo_settings), FakeLLM(), []
    stage_context(run, wh)
    stage_dimensions(run, llm)
    stage_profiling(run, wh, progress=messages.append)
    stage_sql_rules(run, llm, progress=messages.append)
    t = stage_tests(run, llm, wh, progress=messages.append)
    stage_review(run, t["test_specifications"], t["metric_thresholds"])
    stage_results(run, wh, progress=messages.append)
    for prefix in ("Profiling: query ", "SQL Rule Extractor: ", "Test Specifier: ", "Running test "):
        mine = [m for m in messages if m.startswith(prefix)]
        assert mine, prefix
        i, n = re.search(r"(\d+) of (\d+)", mine[-1]).groups()
        assert i == n, mine[-1]


def test_context_dimensions_and_the_quiet_parts_report_progress(tmp_path, demo_settings, wh):
    demo_settings.runs_dir = str(tmp_path)
    run, llm, messages = Run.create(tmp_path, demo_settings), FakeLLM(), []
    stage_context(run, wh, progress=messages.append)
    assert messages[0].startswith("Reading the tables of ")
    stage_dimensions(run, llm, progress=messages.append)
    assert any(m.startswith("Dimension Selector: ") for m in messages)
    assert messages[-1] == "Validating the model's answer"
    stage_profiling(run, wh, progress=messages.append)
    assert "Choosing the join keys of the checks across tables" in messages
    stage_sql_rules(run, llm)
    stage_tests(run, llm, wh, progress=messages.append)
    assert any(m.startswith("Validating ") and m.endswith(" proposed tests") for m in messages)


def _reviewed_run(tmp_path, demo_settings, wh, thresholds=()):
    demo_settings.runs_dir = str(tmp_path)
    run, llm = Run.create(tmp_path, demo_settings), FakeLLM()
    stage_context(run, wh)
    stage_dimensions(run, llm)
    stage_profiling(run, wh)
    stage_sql_rules(run, llm)
    t = stage_tests(run, llm, wh)
    stage_review(run, t["test_specifications"], list(thresholds(run) if callable(thresholds) else thresholds))
    return run


class RecordingWarehouse:
    """The demo warehouse, remembering every query it was sent."""

    def __init__(self, wh):
        self.wh, self.sent = wh, []

    def query(self, sql):
        self.sent.append(sql)
        return self.wh.query(sql)

    def __getattr__(self, name):
        return getattr(self.wh, name)


def test_history_keeps_the_observed_values(tmp_path, demo_settings, wh):
    run = _reviewed_run(tmp_path, demo_settings, wh)
    stage_results(run, wh)
    rows = stage_results(run, wh)["rows"]
    latest = run.history()[0]
    assert len(run.history()) == 2
    assert latest["observed"] == {r["check_id"]: r["observed"] for r in rows}


def test_metric_thresholds_are_measured_again(tmp_path, demo_settings, wh):
    """A schedule must see these values change: each execution re-runs the query that measured them."""
    def one_threshold(run):
        first = run.load("profiling")["profiling_results"][0]
        metric = next(iter(first["metrics"]))
        return [{"id": "mt_again", "asset": first["check_id"].split(":")[1], "check_id": first["check_id"], "metric": metric,
                 "operator": "greater_than_or_equal", "value": -1, "status": "confirmed"}]
    run = _reviewed_run(tmp_path, demo_settings, wh, one_threshold)
    check_id = run.load("review")["metric_thresholds"][0]["check_id"]
    profiling = run.load("profiling")
    block = select_checks(generate_sql_from_dq_json(profiling["dimensions"], run.load("context")["asset_contexts"]),
                          {check_id})
    recording = RecordingWarehouse(wh)
    row = next(r for r in stage_results(run, recording)["rows"] if r["check_id"] == "mt_again")
    assert row["verdict"] == "pass", row
    assert block.split("\n", 1)[1].strip() in [sql.strip() for sql in recording.sent]


def test_a_threshold_on_a_value_the_tests_stage_measured_is_measured_again(tmp_path, demo_settings, wh):
    def on_summary_key(run):
        summary = run.load("tests")["profiling_summary"]
        key = next(k for k in summary if k.startswith("join_key_stats:") or k.endswith(":measured"))
        metric = next(iter(summary[key]["rows"][0]))
        return [{"id": "mt_summary", "asset": summary[key]["table"], "check_id": key, "metric": metric,
                 "operator": "greater_than_or_equal", "value": -1, "status": "confirmed"}]
    run = _reviewed_run(tmp_path, demo_settings, wh, on_summary_key)
    row = next(r for r in stage_results(run, wh)["rows"] if r["check_id"] == "mt_summary")
    assert row["verdict"] == "pass" and row["error"] is None, row


def test_select_checks_keeps_only_the_named_blocks():
    script = "-- CHECK_ID: a\nSELECT 1;\n\n-- CHECK_ID: b\n-- a comment\nSELECT 2;\n\n-- CHECK_ID: c\nSELECT 3;\n"
    assert select_checks(script, {"b"}) == "-- CHECK_ID: b\n-- a comment\nSELECT 2;\n\n"
    assert select_checks(script, {"a", "c"}).count("-- CHECK_ID:") == 2
    assert select_checks(script, set()) == ""


def test_signature_ignores_id_title_rationale_and_threshold():
    spec = {"id": "a__t1", "asset": "s.t", "title": "One", "rationale": "why", "assertion": {"type": "unique",
            "columns": ["id"]}, "threshold": {"metric": "failed_rows", "operator": "equals", "value": 0}}
    same = {**spec, "id": "b__t9", "title": "Other", "rationale": "because", "threshold": None, "status": "confirmed"}
    assert signature(spec) == signature(same)
    assert signature(spec) != signature({**spec, "assertion": {"type": "unique", "columns": ["id", "day"]}})
    assert signature(spec) != signature({**spec, "asset": "s.u"})


def test_earlier_decisions_come_from_the_last_interface_review_of_the_same_database(tmp_path):
    def make(schemas, review=None, port=5432):
        run = Run.create(tmp_path, Settings(database=DatabaseSettings(engine="postgres", port=port, database="w",
                                                                      schemas=schemas)))
        if review is not None:
            run.save("review", review)
        return run

    def review(status, reviewed_in="interface"):
        return {"reviewed_in": reviewed_in, "submitted": {"test_specifications": [{**spec, "status": status}]}}
    spec = {"id": "x__t1", "asset": "s.t", "assertion": {"type": "unique", "columns": ["id"]},
            "threshold": {"metric": "failed_rows", "operator": "less_than_or_equal", "value": 3}}
    make(["s"], review("excluded"))                     # older
    newer = make(["s", "r"], review("confirmed"))       # the most recent one of this database (schemas in any order)
    make(["s", "r"], review("excluded", "command"))     # a command-line review: nobody confirmed each test
    make(["other"], review("excluded"))                 # another database
    make(["r", "s"], review("excluded"), port=6543)     # another server
    current = make(["r", "s"])
    assert earlier_decisions(current, tmp_path) == {
        signature(spec): {"status": "confirmed", "threshold": spec["threshold"], "run": newer.id}}
    assert earlier_decisions(make(["nothing", "here"]), tmp_path) == {}
