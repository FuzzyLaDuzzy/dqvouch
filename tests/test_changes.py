import copy
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fake_llm import FakeLLM

from dqvouch.changes import (
    SCHEDULE_MODEL_ATTEMPTS,
    after_execution, current, describe_changes, detect, diff_contexts, has_changes, merge_dimensions, merge_tests, propose, read_state, rule_contexts,
    table_signatures, targets, write_state, hand_over,
)
from dqvouch.config import DatabaseSettings, Settings
from dqvouch.context import build_asset_contexts
from dqvouch.pipeline import signature, stage_context, stage_dimensions, stage_profiling, stage_review, stage_sql_rules, stage_tests
from dqvouch.store import STAGES, Run

EXAMPLE_SQL = Path(__file__).resolve().parent.parent / "examples" / "orders" / "sql"


def _contexts(tables: dict, sql: dict | None = None) -> dict:
    """AssetContexts shaped exactly as stage 1 builds them, from {"db.table": [(column, type), ...]} and
    {"db.table": transformation SQL}."""
    rows = []
    for table, columns in tables.items():
        database, name = table.split(".")
        rows += [{"database": database, "table": name, "column_name": column, "data_type": data_type,
                  "column_description": None, "table_description": None, "engine": "MergeTree",
                  "partition_key": ""} for column, data_type in columns]
    transformations = {t: {"source_file": f"{t}.sql", "raw_sql": s} for t, s in (sql or {}).items()}
    return build_asset_contexts(rows, transformations, "C3")


FACT = [("order_id", "UInt64"), ("total_price", "Float64"), ("segment", "String")]
ORDERS = [("order_id", "UInt64"), ("status", "String")]


def test_same_contexts_have_no_changes():
    c = _contexts({"analytic.fact_orders": FACT, "staging.orders_trans": ORDERS})
    assert has_changes(diff_contexts(c, sorted(c), c)) is False
    assert table_signatures(c) == table_signatures(copy.deepcopy(c))


def test_an_added_dropped_and_retyped_column_are_described():
    old = _contexts({"analytic.fact_orders": FACT})
    new = _contexts({"analytic.fact_orders": [("order_id", "UInt64"), ("total_price", "Decimal(18, 2)"),
                                              ("discount", "Float64")]})
    changes = diff_contexts(old, sorted(old), new)
    assert describe_changes(changes) == [
        "analytic.fact_orders: + discount (Float64), - segment, total_price: Float64 -> Decimal(18, 2)"]
    assert targets(changes) == ["analytic.fact_orders"]


def test_edited_sql_is_described_on_its_table():
    tables = {"analytic.fact_orders": FACT, "staging.orders_trans": ORDERS}
    old = _contexts(tables, {"analytic.fact_orders": "INSERT INTO analytic.fact_orders "
                                                     "SELECT order_id FROM staging.orders_trans"})
    new = _contexts(tables, {"analytic.fact_orders": "INSERT INTO analytic.fact_orders "
                                                     "SELECT order_id FROM staging.orders_trans WHERE status != 'test'"})
    changes = diff_contexts(old, sorted(old), new)
    line = next(line for line in describe_changes(changes) if line.startswith("analytic.fact_orders: "))
    assert line.endswith("SQL changed")
    assert "analytic.fact_orders" in targets(changes)


def test_new_and_dropped_tables():
    old = _contexts({"analytic.fact_orders": FACT, "landing.old": ORDERS})
    new = _contexts({"analytic.fact_orders": FACT, "staging.events": ORDERS})
    changes = diff_contexts(old, sorted(old), new)
    assert changes["new"] == ["staging.events"]
    assert changes["dropped"] == ["landing.old"]
    assert "staging.events: new table" in describe_changes(changes)
    assert "landing.old: dropped" in describe_changes(changes)
    assert "staging.events" in targets(changes) and "landing.old" not in targets(changes)


def test_a_table_left_out_with_tables_is_not_new():
    """A run made with --tables: staging.returns existed (tables_available) but was left out of its contexts."""
    old = _contexts({"analytic.fact_orders": FACT})
    new = _contexts({"analytic.fact_orders": FACT, "staging.returns": ORDERS})
    changes = diff_contexts(old, ["analytic.fact_orders", "staging.returns"], new)
    assert changes["new"] == [] and "staging.returns" not in changes["changed"]
    assert has_changes(changes) is False



def _dims(tables: dict, cross=()) -> dict:
    """A dimensions result: {"db.table": [(dimension, [columns]), ...]} and cross-table checks."""
    return {"tables": [{"table": t, "dimensions": [{"dimension": d, "columns": c, "rationale": "r"} for d, c in items]}
                       for t, items in tables.items()],
            "cross_table_checks": list(cross)}


def _items(dims: dict, table: str) -> list:
    entry = next((t for t in dims["tables"] if t["table"] == table), {"dimensions": []})
    return [(d["dimension"], d["columns"]) for d in entry["dimensions"]]


def test_merge_dimensions_keeps_old_entries_and_adds_new_ones_for_targets():
    old = _dims({"staging.orders_trans": [("Uniqueness", ["order_id"])],
                 "analytic.fact_orders": [("Validity", ["status"])]})
    new = _dims({"analytic.fact_orders": [("Validity", ["status"]), ("Completeness", ["discount"])],
                 "staging.orders_trans": [("Completeness", ["x"])]})
    merged = merge_dimensions(old, new, {"analytic.fact_orders"}, {"analytic.fact_orders", "staging.orders_trans"})
    assert _items(merged, "analytic.fact_orders") == [("Validity", ["status"]), ("Completeness", ["discount"])]
    assert _items(merged, "staging.orders_trans") == [("Uniqueness", ["order_id"])]


def test_merge_dimensions_drops_tables_that_are_gone():
    cross = {"dimension": "Consistency", "tables_involved": ["landing.old", "staging.orders_trans"],
             "columns_involved": ["order_id"], "rationale": "r"}
    old = _dims({"landing.old": [("Completeness", ["a"])], "staging.orders_trans": [("Uniqueness", ["order_id"])]},
                [cross])
    merged = merge_dimensions(old, _dims({}), set(), {"staging.orders_trans"})
    assert [t["table"] for t in merged["tables"]] == ["staging.orders_trans"]
    assert merged["cross_table_checks"] == []


def _spec(id_, column, title="t"):
    return {"id": id_, "asset": "analytic.fact_orders", "title": title,
            "assertion": {"type": "unique", "columns": [column]},
            "threshold": {"metric": "failed_rows", "operator": "equals", "value": 0}}


def test_merge_tests_drops_a_proposal_that_checks_the_same_thing():
    kept = [_spec("kept_one", "order_id")]
    proposed = [{**_spec("other_id", "order_id", "another title"), "threshold": None}]
    assert [s["id"] for s in merge_tests(kept, proposed)] == ["kept_one"]


def test_merge_tests_renames_a_colliding_id():
    merged = merge_tests([_spec("x", "order_id")], [_spec("x", "status"), _spec("x", "segment")])
    assert [s["id"] for s in merged] == ["x", "x_2", "x_3"]


# --- against the demo database ---------------------------------------------------------------------------------

class HidingWarehouse:
    """The demo warehouse with some metadata hidden, as if a column or table were dropped: `hide` holds
    ("db.table", column) pairs, column None for the whole table."""

    def __init__(self, wh, hide: set):
        self.wh, self.hide = wh, hide

    def fetch_metadata(self, schemas):
        return [r for r in self.wh.fetch_metadata(schemas)
                if (f"{r['database']}.{r['table']}", r["column_name"]) not in self.hide
                and (f"{r['database']}.{r['table']}", None) not in self.hide]

    def __getattr__(self, name):
        return getattr(self.wh, name)


def _sql_copy(tmp_path, settings) -> Path:
    """The example's transformation SQL copied where a test may edit it; the settings point at the copy."""
    target = tmp_path / "sql"
    shutil.copytree(EXAMPLE_SQL, target)
    settings.sql_dir = str(target)
    return target


def _edit_fact_orders(sql_dir: Path) -> None:
    path = sql_dir / "04_analytic_fact_orders.sql"
    text = path.read_text(encoding="utf-8")
    assert "WHERE o.status != 'test'" in text
    path.write_text(text.replace("WHERE o.status != 'test'", "WHERE o.status NOT IN ('test', 'demo')"),
                    encoding="utf-8")


def _reviewed_run(tmp_path, settings, wh, llm=None, tables=None) -> Run:
    """A run of the demo with every proposed test confirmed on the Review screen."""
    settings.runs_dir = str(tmp_path / "runs")
    run, llm = Run.create(settings.runs_dir, settings), llm or FakeLLM()
    stage_context(run, wh, tables)
    stage_dimensions(run, llm)
    stage_profiling(run, wh)
    stage_sql_rules(run, llm)
    t = stage_tests(run, llm, wh)
    stage_review(run, [{**s, "status": "confirmed"} for s in t["test_specifications"]],
                 [{**m, "status": "confirmed"} for m in t["metric_thresholds"]], "interface")
    return run


def test_detect_finds_nothing_on_an_unchanged_database(tmp_path, demo_settings, wh):
    _sql_copy(tmp_path, demo_settings)
    run = _reviewed_run(tmp_path, demo_settings, wh)
    assert has_changes(detect(run, wh)["changes"]) is False


def test_detect_finds_an_edited_transformation(tmp_path, demo_settings, wh):
    sql_dir = _sql_copy(tmp_path, demo_settings)
    run = _reviewed_run(tmp_path, demo_settings, wh)
    _edit_fact_orders(sql_dir)
    changes = detect(run, wh)["changes"]
    assert changes["changed"]["analytic.fact_orders"]["sql_changed"] is True


def test_detect_sees_a_dropped_column(tmp_path, demo_settings, wh):
    _sql_copy(tmp_path, demo_settings)
    run = _reviewed_run(tmp_path, demo_settings, wh)
    changes = detect(run, HidingWarehouse(wh, {("analytic.fact_orders", "segment")}))["changes"]
    assert "segment" in changes["changed"]["analytic.fact_orders"]["columns_dropped"]


def test_detect_only_looks_at_the_runs_tables_and_new_ones(tmp_path, demo_settings, wh):
    _sql_copy(tmp_path, demo_settings)
    run = _reviewed_run(tmp_path, demo_settings, wh, tables=["staging.orders_trans"])
    detected = detect(run, wh)
    assert has_changes(detected["changes"]) is False
    assert list(detected["context"]["asset_contexts"]) == ["staging.orders_trans"]



class ExtraSpecLLM(FakeLLM):
    """The fake model, also proposing a test on analytic.fact_orders that the first run never had."""
    EXTRA = {"id": "fact_orders_no_test_status", "asset": "analytic.fact_orders", "dimension": "Validity",
             "assertion": {"type": "row_predicate", "condition": {"op": "neq", "left": {"column": "status"},
                                                                  "right": {"value": "test"}}},
             "threshold": {"metric": "failed_rows", "operator": "equals", "value": 0},
             "evidence": [{"path": "transformation.raw_sql"}], "rationale": "the SQL drops test orders",
             "status": "proposed"}

    def __call__(self, prompt):
        answer = super().__call__(prompt)
        data = json.loads(answer.removeprefix("```json\n").removesuffix("\n```"))
        if "test_specifications" not in data:
            return answer
        data["test_specifications"].append(dict(self.EXTRA))
        return "```json\n" + json.dumps(data) + "\n```"


def _pending(tmp_path, settings, wh, llm=None, hide=None):
    """A reviewed demo run, the fact_orders SQL edited, and the pending run proposed for that change."""
    sql_dir = _sql_copy(tmp_path, settings)
    run = _reviewed_run(tmp_path, settings, wh)
    _edit_fact_orders(sql_dir)
    seen = HidingWarehouse(wh, hide) if hide else wh
    detected, llm = detect(run, seen), llm or FakeLLM()
    return run, propose(run, seen, llm, detected), llm, detected


def test_a_pending_run_asks_the_model_only_about_the_targets(tmp_path, demo_settings, wh):
    run, new, llm, detected = _pending(tmp_path, demo_settings, wh)
    assert new.completed() == list(STAGES[:5])
    assert new.load("dimensions")["scoped_to"] == targets(detected["changes"])
    assert "staging.returns" in detected["context"]["asset_contexts"]  # no lineage link to the changed tables
    assert llm.prompts and not any("staging.returns" in p for p in llm.prompts)


def test_confirmed_tests_are_carried_and_new_ones_wait(tmp_path, demo_settings, wh):
    run, new, _, _ = _pending(tmp_path, demo_settings, wh, ExtraSpecLLM())
    tests = new.load("tests")["test_specifications"]
    by_id = {s["id"]: s for s in tests}
    for old in run.load("review")["test_specifications"]:
        assert by_id[old["id"]]["status"] == "confirmed" and by_id[old["id"]]["_kept_from"] == run.id
    kept = [s for s in tests if s.get("_kept_from")]
    added = [s for s in tests if not s.get("_kept_from")]
    assert {signature(s) for s in added}.isdisjoint({signature(s) for s in kept})
    extra = [s for s in added if s["id"].endswith("fact_orders_no_test_status")]
    assert extra and all(s["status"] == "proposed" for s in extra)


def test_a_test_on_a_dropped_column_is_set_aside_with_the_reason(tmp_path, demo_settings, wh):
    run, new, _, _ = _pending(tmp_path, demo_settings, wh, hide={("analytic.fact_orders", "status")})
    assert not any(s["id"].endswith("fact_orders_status_domain") for s in new.load("tests")["test_specifications"])
    aside = new.info()["origin"]["set_aside"]
    assert any(a["id"].endswith("fact_orders_status_domain") and "status" in a["reason"] for a in aside)


def test_a_change_that_only_drops_tables_makes_no_model_call(tmp_path, demo_settings, wh):
    _sql_copy(tmp_path, demo_settings)
    run = _reviewed_run(tmp_path, demo_settings, wh)
    detected = {"changes": {"changed": {}, "new": [], "dropped": ["x.gone"]}, "context": run.load("context")}
    new = propose(run, wh, None, detected)
    assert new.completed() == list(STAGES[:5])
    assert new.load("dimensions")["llm_calls"] == 0 and new.load("tests")["llm_calls"] == 0
    assert ({s["id"] for s in new.load("tests")["test_specifications"]}
            == {s["id"] for s in run.load("review")["test_specifications"]})


def test_a_failed_proposal_leaves_no_run_behind(tmp_path, demo_settings, wh):
    sql_dir = _sql_copy(tmp_path, demo_settings)
    run = _reviewed_run(tmp_path, demo_settings, wh)
    _edit_fact_orders(sql_dir)

    def broken(prompt):
        raise RuntimeError("model down")
    with pytest.raises(RuntimeError, match="model down"):
        propose(run, wh, broken, detect(run, wh))
    assert [p.name for p in run.path.parent.iterdir()] == [run.id]


# --- after an execution ----------------------------------------------------------------------------------------

WEBHOOK = {"DQVOUCH_ALERT_WEBHOOK_URL": "https://hooks.example/abc"}


def _sent(monkeypatch) -> list:
    sent = []
    monkeypatch.setattr("dqvouch.alerts.send_webhook", lambda url, text, timeout=10: sent.append(text))
    return sent


def _no_model():
    raise AssertionError("the model must not be needed here")


def _changed_run(tmp_path, settings, wh):
    sql_dir = _sql_copy(tmp_path, settings)
    run = _reviewed_run(tmp_path, settings, wh)
    _edit_fact_orders(sql_dir)
    return run, sql_dir


def _runs(run) -> list:
    return sorted(p.name for p in run.path.parent.iterdir())


def test_no_change_means_no_model_and_no_alert(tmp_path, demo_settings, wh, monkeypatch):
    sent = _sent(monkeypatch)
    _sql_copy(tmp_path, demo_settings)
    run = _reviewed_run(tmp_path, demo_settings, wh)
    assert after_execution(run, wh, _no_model, WEBHOOK, str)["status"] == "unchanged"
    assert sent == []


def test_a_change_proposes_once_and_alerts_once(tmp_path, demo_settings, wh, monkeypatch):
    sent = _sent(monkeypatch)
    run, _ = _changed_run(tmp_path, demo_settings, wh)
    first = after_execution(run, wh, FakeLLM, WEBHOOK, str)
    second = after_execution(run, wh, _no_model, WEBHOOK, str)
    assert first["status"] == "proposed" and second["status"] == "pending_exists"
    assert second["pending_run"] == first["pending_run"] and len(_runs(run)) == 2
    assert len(sent) == 1 and "analytic.fact_orders" in sent[0] and "to review" in sent[0]
    pending = Run.open(run.path.parent / first["pending_run"])
    assert read_state(run)["proposed"] == pending.info()["origin"]["proposed"]  # the old run's Results says how many


def test_a_new_change_before_review_replaces_the_pending_run(tmp_path, demo_settings, wh, monkeypatch):
    sent = _sent(monkeypatch)
    run, sql_dir = _changed_run(tmp_path, demo_settings, wh)
    first = after_execution(run, wh, FakeLLM, WEBHOOK, str)
    path = sql_dir / "04_analytic_fact_orders.sql"
    path.write_text(path.read_text(encoding="utf-8").replace("NOT IN ('test', 'demo')", "NOT IN ('test', 'demo', 'qa')"),
                    encoding="utf-8")
    second = after_execution(run, wh, FakeLLM, WEBHOOK, str)
    assert second["status"] == "replaced" and second["pending_run"] != first["pending_run"]
    assert Run.open(run.path.parent / first["pending_run"]).info()["replaced_by"] == second["pending_run"]
    assert len(sent) == 2


def test_proposing_failure_alerts_once_and_retries(tmp_path, demo_settings, wh, monkeypatch):
    sent = _sent(monkeypatch)
    run, _ = _changed_run(tmp_path, demo_settings, wh)

    def down():
        raise RuntimeError("model down")
    first, second = after_execution(run, wh, down, WEBHOOK, str), after_execution(run, wh, down, WEBHOOK, str)
    assert first["status"] == second["status"] == "failed" and "model down" in first["error"]
    assert len(sent) == 1 and "model down" in sent[0] and len(_runs(run)) == 1
    failed = read_state(run)["failed"]  # what the interface shows when no alert channel is set
    assert failed["lines"] == first["lines"] and "model down" in failed["error"]
    assert after_execution(run, wh, FakeLLM, WEBHOOK, str)["status"] == "proposed"
    assert "failed" not in read_state(run)


def test_a_held_proposal_lock_skips_proposing(tmp_path, demo_settings, wh, monkeypatch):
    sent = _sent(monkeypatch)
    run, _ = _changed_run(tmp_path, demo_settings, wh)
    (run.path / "proposal.lock").write_text(json.dumps({"pid": 1, "at": datetime.now(timezone.utc).isoformat()}),
                                            encoding="utf-8")
    assert after_execution(run, wh, _no_model, WEBHOOK, str)["status"] == "busy"
    assert sent == [] and len(_runs(run)) == 1


def test_propose_on_change_false_only_alerts(tmp_path, demo_settings, wh, monkeypatch):
    sent = _sent(monkeypatch)
    demo_settings.propose_on_change = False
    run, _ = _changed_run(tmp_path, demo_settings, wh)
    first, second = after_execution(run, wh, _no_model, WEBHOOK, str), after_execution(run, wh, _no_model, WEBHOOK, str)
    assert first["status"] == second["status"] == "detected_only"
    assert len(sent) == 1 and "propose_on_change" in sent[0] and len(_runs(run)) == 1
    assert read_state(run)["detected"]["lines"] == first["lines"]  # shown in the interface too


# --- hand-over --------------------------------------------------------------------------------------------------

def _plain_run(tmp_path, reviewed=True, origin=None) -> Run:
    """A run on disk only (no database): reviewed or not, made by a change from `origin` or not."""
    run = Run.create(tmp_path / "runs", Settings(database=DatabaseSettings(engine="postgres", database="d",
                                                                           schemas=["s"])))
    if origin is not None:
        run.update_info(origin={"kind": "change", "from_run": origin.id})
    if reviewed:
        run.save("review", {"test_specifications": [], "metric_thresholds": []})
    return run


def test_hand_over_moves_the_schedule_and_keeps_the_history(tmp_path):
    old = _plain_run(tmp_path)
    (old.path / "schedule.json").write_text(json.dumps({"every_minutes": 1440,
                                                        "next_at": "2026-11-17T09:00:00+00:00"}), encoding="utf-8")
    old.add_history({"at": "2026-11-15T09:00:00+00:00", "trigger": "schedule"})
    old.add_history({"at": "2026-11-16T09:00:00+00:00", "trigger": "schedule"})
    new = _plain_run(tmp_path, origin=old)
    new.add_history({"at": "2026-11-16T12:00:00+00:00", "trigger": "manual"})
    hand_over(new)
    assert not (old.path / "schedule.json").exists()
    assert json.loads((new.path / "schedule.json").read_text(encoding="utf-8"))["next_at"] == "2026-11-17T09:00:00+00:00"
    assert [h["at"] for h in new.history()] == ["2026-11-16T12:00:00+00:00", "2026-11-16T09:00:00+00:00",
                                                "2026-11-15T09:00:00+00:00"]
    assert read_state(old)["superseded_by"] == new.id and "pending_run" not in read_state(old)


def test_hand_over_twice_changes_nothing(tmp_path):
    old = _plain_run(tmp_path)
    old.add_history({"at": "2026-11-15T09:00:00+00:00", "trigger": "schedule"})
    new = _plain_run(tmp_path, origin=old)
    hand_over(new)
    hand_over(new)
    assert len(new.history()) == 1


def test_current_follows_superseded_by_to_the_newest_reviewed_run(tmp_path):
    a = _plain_run(tmp_path)
    b = _plain_run(tmp_path, origin=a)
    hand_over(b)
    c = _plain_run(tmp_path, reviewed=False, origin=b)  # proposed for a later change, not reviewed yet
    write_state(b, pending_run=c.id, superseded_by=c.id)
    assert current(a).id == b.id


def test_current_of_a_run_never_superseded_is_itself(tmp_path):
    a = _plain_run(tmp_path)
    assert current(a).id == a.id


# --- fixes from the whole-branch review --------------------------------------------------------------------------

def test_derived_facts_do_not_change_a_signature():
    """What dqvouch derives from the structure (dq_signals, semantic roles, usage facts) may change with a release;
    only the structure itself counts, or an upgrade would look like a change of every table."""
    c = _contexts({"analytic.fact_orders": FACT, "staging.orders_trans": ORDERS},
                  {"analytic.fact_orders": "INSERT INTO analytic.fact_orders SELECT order_id FROM staging.orders_trans"})
    later = copy.deepcopy(c)
    later["analytic.fact_orders"]["dq_signals"] = {"something": {"fact": "a new extraction rule"}}
    later["analytic.fact_orders"]["columns"][0]["semantic_roles"] = ["a_new_role"]
    later["analytic.fact_orders"]["columns"].reverse()
    assert table_signatures(later) == table_signatures(c)
    assert has_changes(diff_contexts(c, sorted(c), later)) is False


def test_a_carried_test_keeps_its_threshold_when_the_data_now_contradicts_it(tmp_path, demo_settings, wh):
    """A confirmed test that started failing on a real defect must keep failing after a change, not lose its limit:
    carried tests come back exactly as reviewed; only the tables' structure can set one aside."""
    sql_dir = _sql_copy(tmp_path, demo_settings)
    run = _reviewed_run(tmp_path, demo_settings, wh)
    review = run.load("review")
    drifted = {"id": "drifted", "asset": "analytic.fact_orders", "dimension": "Validity", "title": "only pending",
               "assertion": {"type": "row_predicate",
                             "condition": {"op": "in", "left": {"column": "status"}, "values": ["pending"]}},
               "threshold": {"metric": "failed_rows", "operator": "equals", "value": 0},
               "evidence": [{"path": "transformation.raw_sql"}], "rationale": "r", "status": "confirmed"}
    review["test_specifications"].append(drifted)  # reviewed when the data still agreed with it
    run.save("review", review)
    _edit_fact_orders(sql_dir)
    new = propose(run, wh, FakeLLM(), detect(run, wh))
    kept = next(s for s in new.load("tests")["test_specifications"] if s["id"] == "drifted")
    assert kept["threshold"] == drifted["threshold"]
    assert "suspended_threshold" not in kept and not kept.get("review_warnings")


def test_detect_refuses_when_the_sql_folder_is_gone(tmp_path, demo_settings, wh):
    """A moved repository or an unmounted share is not every table's SQL changing."""
    sql_dir = _sql_copy(tmp_path, demo_settings)
    run = _reviewed_run(tmp_path, demo_settings, wh)
    sql_dir.rename(tmp_path / "sql-elsewhere")
    with pytest.raises(ValueError, match="SQL folder"):
        detect(run, wh)
    sql_dir.mkdir()  # there, but without any .sql file
    with pytest.raises(ValueError, match="SQL folder"):
        detect(run, wh)


def test_a_replaced_pending_run_is_not_handed_over(tmp_path):
    """The structure changed again before review: reviewing the first, replaced, run must leave the schedule where it
    is and keep pointing at the run that replaced it."""
    old = _plain_run(tmp_path)
    (old.path / "schedule.json").write_text(json.dumps({"every_minutes": 60,
                                                        "next_at": "2026-11-17T09:00:00+00:00"}), encoding="utf-8")
    first = _plain_run(tmp_path, origin=old)
    second = _plain_run(tmp_path, reviewed=False, origin=old)
    first.update_info(replaced_by=second.id)
    write_state(old, pending_run=second.id)
    assert hand_over(first) is False
    assert (old.path / "schedule.json").exists() and read_state(old)["pending_run"] == second.id
    assert "superseded_by" not in read_state(old)


def test_sql_guarantees_from_unread_transformations_are_kept():
    """Guarantees live on the table they are about (an input requirement on the input table) and are named after
    the transformation they come from: a re-read transformation's old ones go, every other one stays."""
    sql = {"analytic.fact_orders": "INSERT INTO analytic.fact_orders SELECT order_id FROM staging.orders_trans",
           "analytic.daily_sales": "INSERT INTO analytic.daily_sales SELECT d FROM analytic.fact_orders",
           "staging.orders_trans": "INSERT INTO staging.orders_trans SELECT 1"}
    fresh = _contexts({"analytic.fact_orders": FACT, "staging.orders_trans": ORDERS,
                       "analytic.daily_sales": [("d", "Date")]}, sql)
    old = copy.deepcopy(fresh)
    old["staging.orders_trans"]["sql_invariants"] = {
        "analytic_fact_orders_1": {"id": "analytic_fact_orders_1", "category": "presence"},  # from fact_orders' SQL
        "staging_orders_trans_1": {"id": "staging_orders_trans_1", "category": "presence"}}  # from its own SQL
    old["analytic.fact_orders"]["sql_invariants"] = {
        "analytic_daily_sales_1": {"id": "analytic_daily_sales_1", "category": "presence"}}  # from daily_sales' SQL
    out = rule_contexts(fresh, old, {"analytic.fact_orders"})
    assert set(out["staging.orders_trans"]["sql_invariants"]) == {"staging_orders_trans_1"}
    assert set(out["analytic.fact_orders"]["sql_invariants"]) == {"analytic_daily_sales_1"}
    assert "sql_invariants" not in fresh["analytic.fact_orders"]


def test_proposing_after_a_scheduled_execution_limits_the_model_retries(tmp_path, demo_settings, wh):
    """The clients retry a busy model forever (right for a person waiting on a screen); from a schedule an outage
    must end in "failed" so the execution finishes, the exit code is delivered and the job slot is freed."""
    run, _ = _changed_run(tmp_path, demo_settings, wh)

    class LimitedLLM(FakeLLM):
        max_attempts = None
    llm = LimitedLLM()
    assert after_execution(run, wh, lambda: llm, {}, str)["status"] == "proposed"
    assert llm.max_attempts == SCHEDULE_MODEL_ATTEMPTS
