"""The seven stages of a run. Each one reads what the earlier stages saved in the run folder, saves its own result
and returns it. The CLI calls them in order; a UI can call the same functions one screen at a time, and redo any
stage (saving a stage deletes the later ones)."""
import copy
import datetime
import json
import time
from pathlib import Path

from dqvouch.config import ENGINE_LABELS
from dqvouch.context import build_asset_contexts, load_transformations_by_asset
from dqvouch.profiling import (
    DQ_DIMENSIONS, add_join_key_stats, build_profiling_summary, choose_cross_table_join_keys,
    generate_sql_from_dq_json, render_script, run_profiling, select_checks,
)
from dqvouch.render_sql import (
    ASSERTION_RENDERERS, check_metric_thresholds, generate_sql_from_test_specifications, run_tests,
)
from dqvouch.rules import run_invariant_extractor, with_invariant_items
from dqvouch.selector import select_dimensions
from dqvouch.specifier import run_repair_pass, run_test_specifier_per_asset
from dqvouch.store import Run
from dqvouch.warehouse import renderer
from dqvouch.validator import (
    validate_dimension_output, validate_metric_thresholds, validate_reviewed_metric_thresholds,
    validate_reviewed_test_specifications, validate_test_specifications,
)

_ASSERTION_TYPES = list(ASSERTION_RENDERERS.keys())


def _settings(run: Run) -> dict:
    return run.info()["settings"]


def _engine(settings: dict) -> str:
    """The engine's name as the prompts say it: ClickHouse or PostgreSQL."""
    return ENGINE_LABELS[settings["database"]["engine"]]


def _need(run: Run, stage: str) -> dict:
    data = run.load(stage)
    if data is None:
        raise ValueError(f"run the '{stage}' stage first")
    return data


def _seconds(t0: float) -> float:
    return round(time.monotonic() - t0, 2)


def _counted(llm):
    """Wraps llm so the stage can report how many calls it made."""
    calls = []

    def call(prompt):
        calls.append(1)
        return llm(prompt)
    return call, calls


def _contexts(run: Run) -> dict:
    """The AssetContexts as the Test Specifier sees them: with the SQL guarantees when that stage ran."""
    return (run.load("sql_rules") or {}).get("asset_contexts") or _need(run, "context")["asset_contexts"]


def _quiet(progress):
    return progress or (lambda message: None)


def stage_context(run: Run, wh, tables: list[str] | None = None, progress=None) -> dict:
    """Stage 1 (code): one AssetContext per table of the configured schemas, optionally only `tables`."""
    data = build_context(_settings(run), wh, tables, progress)
    run.save("context", data)
    return data


def build_context(settings: dict, wh, tables: list[str] | None = None, progress=None) -> dict:
    """Stage 1's result without saving it: also how change detection reads the database's structure now."""
    t0, s, progress = time.monotonic(), settings, _quiet(progress)
    schemas = s["database"]["schemas"]
    progress(f"Reading the tables of {', '.join(schemas)}")
    rows = wh.fetch_metadata(schemas)
    if not rows:
        raise ValueError(f"no tables found in schemas {schemas}")
    available = sorted({f"{r['database']}.{r['table']}" for r in rows})
    if tables is not None:
        unknown = sorted(set(tables) - set(available))
        if unknown or not tables:
            raise ValueError(f"choose at least one of the available tables; unknown: {unknown}")
        rows = [r for r in rows if f"{r['database']}.{r['table']}" in set(tables)]
    level = s["context_level"]
    progress(f"Matching the SQL to {len({(r['database'], r['table']) for r in rows})} tables and reading its facts")
    transformations = load_transformations_by_asset(s["sql_dir"]) if level == "C3" else {}
    contexts = build_asset_contexts(rows, transformations, level, s["signals"], s["sql_rules"])
    return {"asset_contexts": contexts, "tables_available": available, "seconds": _seconds(t0)}


def stage_dimensions(run: Run, llm, progress=None) -> dict:
    """Stage 2 (LLM, 1 call): which DQ dimensions to measure where; the validator keeps what exists."""
    t0, s, progress = time.monotonic(), _settings(run), _quiet(progress)
    contexts = _need(run, "context")["asset_contexts"]
    counted, calls = _counted(llm)
    progress(f"Dimension Selector: {s['model']} is reading {len(contexts)} tables (one call, the longest wait of a "
             f"run)")
    dq_json, raw, prompt = select_dimensions(contexts, s["context_level"], counted, _engine(s))
    progress("Validating the model's answer")
    report = validate_dimension_output(dq_json, contexts, DQ_DIMENSIONS)
    data = {"prompt": prompt, "raw_response": raw, "dimensions": report.accepted, "rejected": report.rejected,
            "llm_calls": len(calls), "seconds": _seconds(t0)}
    run.save("dimensions", data)
    return data


def stage_profiling(run: Run, wh, progress=None) -> dict:
    """Stage 3 (code): metric-only SQL for the accepted dimensions, run read-only on the database."""
    t0, progress = time.monotonic(), _quiet(progress)
    contexts = _need(run, "context")["asset_contexts"]
    dimensions = copy.deepcopy(_need(run, "dimensions")["dimensions"])
    progress("Choosing the join keys of the checks across tables")
    choose_cross_table_join_keys(dimensions, contexts, wh)
    sql = generate_sql_from_dq_json(dimensions, contexts)
    results, errors = run_profiling(wh, sql, progress)
    data = {"dimensions": dimensions, "sql": render_script(sql, wh.render), "profiling_results": results,
            "errors": errors,
            "profiling_summary": build_profiling_summary(results), "seconds": _seconds(t0)}
    run.save("profiling", data)
    return data


def stage_sql_rules(run: Run, llm, progress=None) -> dict:
    """Stage 4 (LLM, 2 calls per transformation): what each transformation's SQL guarantees and requires.
    Skipped, without calling the LLM, below C3, when turned off, or when no table has transformation SQL."""
    t0, s = time.monotonic(), _settings(run)
    contexts = copy.deepcopy(_need(run, "context")["asset_contexts"])
    reason = sql_rules_skip_reason(s, contexts)
    if reason:
        data = {"skipped": reason, "seconds": _seconds(t0)}
        run.save("sql_rules", data)
        return data
    dimensions = _need(run, "profiling")["dimensions"]
    counted, calls = _counted(llm)
    record = run_invariant_extractor(contexts, counted, progress)
    data = {"asset_contexts": contexts, "checklist_dimensions": with_invariant_items(dimensions, contexts),
            "calls": record["calls"], "accepted": record["accepted"], "rejected": record["rejected"],
            "llm_calls": len(calls), "seconds": _seconds(t0)}
    run.save("sql_rules", data)
    return data


def sql_rules_skip_reason(settings: dict, contexts: dict) -> str | None:
    """Why the SQL Rule Extractor does not run for these settings and tables; None when it does."""
    if settings["context_level"] != "C3":
        return "the SQL Rule Extractor needs context level C3 (the transformation SQL)"
    if not settings["sql_rules"]:
        return "the SQL Rule Extractor is turned off"
    if not any((c.get("transformation") or {}).get("raw_sql") not in (None, "unknown") for c in contexts.values()):
        return "no transformation SQL was matched to any table"
    return None


def stage_tests(run: Run, llm, wh, progress=None) -> dict:
    """Stage 5 (LLM, 1 call per table and per table pair, plus repairs): TestSpecifications, then the validator."""
    t0, s = time.monotonic(), _settings(run)
    sql_rules = _need(run, "sql_rules")  # ran or skipped: without it the SQL guarantees would be silently missing
    profiling = _need(run, "profiling")
    checklist = sql_rules.get("checklist_dimensions") or profiling["dimensions"]
    data = propose_tests(_contexts(run), checklist, profiling["profiling_summary"], llm, wh, s, progress)
    data["seconds"] = _seconds(t0)
    run.save("tests", data)
    return data


def propose_tests(contexts: dict, checklist: dict, summary: dict, llm, wh, settings: dict, progress=None) -> dict:
    """The Test Specifier calls for `checklist` (one per table, one per pair), the validator and the repair pass:
    everything stage 5 saves but its duration. Also how a change proposes tests for the changed tables only."""
    s, progress = settings, _quiet(progress)
    summary = copy.deepcopy(summary)
    counted, calls = _counted(llm)
    specs, thresholds, ts_calls = run_test_specifier_per_asset(contexts, checklist, summary, counted,
                                                                _engine(s), progress)
    progress(f"Validating {len(specs)} proposed tests")
    add_join_key_stats(summary, specs, contexts, wh)
    validation = validate_test_specifications(specs, contexts, _ASSERTION_TYPES, profiling_summary=summary)
    if s["repair"]:
        fixed, fixed_thresholds, fixed_calls = run_repair_pass(contexts, checklist, summary, validation.rejected,
                                                               specs, counted, _engine(s), progress)
        if fixed_calls:
            specs += fixed
            thresholds += fixed_thresholds
            ts_calls += fixed_calls
            progress(f"Validating {len(specs)} proposed tests")
            add_join_key_stats(summary, fixed, contexts, wh)
            validation = validate_test_specifications(specs, contexts, _ASSERTION_TYPES, profiling_summary=summary)
    metric_validation = validate_metric_thresholds(thresholds, contexts, summary)
    return {"calls": ts_calls, "test_specifications": validation.accepted, "rejected": validation.rejected,
            "metric_thresholds": metric_validation.accepted, "metric_thresholds_rejected": metric_validation.rejected,
            "profiling_summary": summary, "llm_calls": len(calls)}


def stage_review(run: Run, test_specifications: list, metric_thresholds: list, reviewed_in: str = "command",
                 undecided: list | None = None) -> dict:
    """Stage 6 (person): the tests the person confirmed, edited or excluded (`"status": "excluded"`), validated
    again. Only what passes here can be executed. `reviewed_in` says where: "interface" (each test decided on the
    Review screen) or "command" (the command line, where untouched tests are not decisions); `undecided` lists the
    ids the person left without a decision (sent as excluded, so they do not run)."""
    t0 = time.monotonic()
    if not isinstance(test_specifications, list) or not isinstance(metric_thresholds, list):
        raise ValueError("review expects two lists: test_specifications and metric_thresholds")
    contexts = _contexts(run)
    summary = _need(run, "tests")["profiling_summary"]
    reviewed = validate_reviewed_test_specifications(copy.deepcopy(test_specifications), contexts, _ASSERTION_TYPES,
                                                     profiling_summary=summary)
    reviewed_thresholds = validate_reviewed_metric_thresholds(copy.deepcopy(metric_thresholds), contexts, summary)
    data = {"reviewed_in": reviewed_in, "undecided": list(undecided or []),
            "submitted": {"test_specifications": test_specifications, "metric_thresholds": metric_thresholds},
            "test_specifications": reviewed.accepted, "rejected": reviewed.rejected,
            "metric_thresholds": reviewed_thresholds.accepted,
            "metric_thresholds_rejected": reviewed_thresholds.rejected, "seconds": _seconds(t0)}
    run.save("review", data)
    return data


def _measured_by_tests_stage(check_id: str) -> bool:
    """Values the tests stage measures itself (add_join_key_stats), not the profiling stage's checks."""
    return check_id.startswith("join_key_stats:") or ":measured" in check_id


def remeasure(run: Run, wh, metric_thresholds: list) -> tuple[list[dict], dict]:
    """Measures again, with the queries that measured them first, the values the metric thresholds compare against:
    rows shaped like profiling_results, and {check_id: error} for the ones that failed."""
    ids = {t["check_id"] for t in metric_thresholds}
    if not ids:
        return [], {}
    profiling = _need(run, "profiling")
    sql = select_checks(generate_sql_from_dq_json(profiling["dimensions"], _need(run, "context")["asset_contexts"]),
                        {i for i in ids if not _measured_by_tests_stage(i)})
    rows, errors = run_profiling(wh, sql) if sql else ([], [])
    others = {i for i in ids if _measured_by_tests_stage(i)}
    if others:
        fresh = {}
        add_join_key_stats(fresh, _need(run, "tests")["test_specifications"], _contexts(run), wh)
        rows += [{"check_id": k, "metrics": fresh[k]["rows"][0]} for k in sorted(others) if fresh.get(k, {}).get("rows")]
    return rows, {e["check_id"]: e["error"] for e in errors}


def export_sql(run: Run) -> str:
    """The run's confirmed tests as one SQL script in its database's dialect, to run without dqvouch: per test a
    comment block (what it checks, its threshold) and a SELECT that returns the observed value; then the metric
    thresholds with the profiling query that measures them. Only reads; needs no connection."""
    review = _need(run, "review")
    info = run.info()
    db = info["settings"]["database"]
    render = renderer(db["engine"])
    specs, thresholds = review["test_specifications"], review["metric_thresholds"]
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    parts = [f"-- dqvouch export of run {info['id']}\n"
             f"-- {ENGINE_LABELS.get(db['engine'], db['engine'])}, schemas {', '.join(db['schemas'])}; exported {now}\n"
             f"-- {len(specs)} confirmed test(s) and {len(thresholds)} metric threshold(s). Each query only reads and\n"
             f"-- returns the value dqvouch observes: compare it with the THRESHOLD or EXPECTED line above it.\n"]
    if specs:
        parts.append(render_script(generate_sql_from_test_specifications(specs), render))
    profile_sql = generate_sql_from_dq_json(_need(run, "profiling")["dimensions"],
                                            _need(run, "context")["asset_contexts"]) if thresholds else ""
    for t in thresholds:
        expected = (f"-- EXPECTED: {t['metric']} {t['operator']} {t['value']} "
                    f"(metric threshold {t['id']} on {t['asset']})")
        block = "" if _measured_by_tests_stage(t["check_id"]) else select_checks(profile_sql, {t["check_id"]})
        if not block:
            parts.append(f"{expected}\n-- {t['check_id']} is measured by dqvouch's tests stage itself; there is no "
                         f"standalone query for it.\n")
            continue
        first, rest = block.split("\n", 1)
        parts.append(render_script(f"{first}\n{expected}\n{rest}", render))
    return "\n".join(parts)


def record_execution(run: Run, entry: dict, notify=None, rows=None) -> dict:
    """Adds one execution to the run's history. `notify(entry, history, rows)` (the alerts) runs first, with the
    history before this execution, newest first; what it returns is kept in the entry."""
    if notify:
        history = run.history()
        if not any(h.get("verdicts") for h in history):  # a long outage: look further back
            last = run.last_with_verdicts()
            history += [last] if last else []
        entry.update(notify(entry, history, rows or []) or {})
    run.add_history(entry)
    return entry


def signature(spec: dict) -> str:
    """What a test checks, whatever its id, title, rationale or threshold: its table and its assertion."""
    return json.dumps([spec.get("asset"), spec.get("assertion")], sort_keys=True, default=str)


def _same_database(a: dict, b: dict) -> bool:
    return (all(a.get(k) == b.get(k) for k in ("engine", "host", "port", "database"))
            and sorted(a.get("schemas") or []) == sorted(b.get("schemas") or []))


def earlier_decisions(run: Run, runs_dir) -> dict:
    """signature -> {"status", "threshold", "run"}: the decisions of the most recent earlier run of the same
    database (engine, host, port, database, schemas) whose review a person submitted on the Review screen. The
    threshold is the confirmed one, edits included. Command-line reviews are never a source."""
    mine = run.info()["settings"]["database"]
    folders = [p for p in Path(runs_dir).iterdir() if p.name < run.id and (p / "run.json").is_file()]
    for path in sorted(folders, key=lambda p: p.name, reverse=True):
        earlier = Run.open(path)
        review = earlier.load("review")
        if not review or review.get("reviewed_in") != "interface":
            continue
        if not _same_database(earlier.info()["settings"]["database"], mine):
            continue
        decisions, undecided = {}, set(review.get("undecided") or [])
        for spec in (review.get("submitted") or {}).get("test_specifications") or []:
            if (isinstance(spec, dict) and spec.get("status") in ("confirmed", "excluded") and spec.get("assertion")
                    and spec.get("id") not in undecided):
                decisions.setdefault(signature(spec), {
                    "status": spec["status"], "run": earlier.id,
                    "threshold": spec.get("threshold") if spec["status"] == "confirmed" else None})
        return decisions
    return {}


def stage_results(run: Run, wh, progress=None, trigger: str = "manual", notify=None) -> dict:
    """Stage 7 (code): renders the reviewed tests into SQL, runs them read-only and checks the metric thresholds
    against values measured again now. Each execution is also added to the run's history with its verdicts and
    observed values (`trigger`: "manual", "schedule" or "command")."""
    t0, s = time.monotonic(), _settings(run)
    review = _need(run, "review")
    specs = review["test_specifications"]
    sql = generate_sql_from_test_specifications(specs) if specs else ""
    rows = run_tests(wh, sql, {spec["id"]: spec for spec in specs}, s["context_level"], progress)
    _quiet(progress)("Checking the metric thresholds")
    measured, failed = remeasure(run, wh, review["metric_thresholds"])
    threshold_rows = check_metric_thresholds(measured, review["metric_thresholds"], s["context_level"])
    for spec, row in zip(review["metric_thresholds"], threshold_rows):
        if spec["check_id"] in failed:
            row["error"] = f"measuring {spec['check_id']} failed: {failed[spec['check_id']]}"
    rows += threshold_rows
    data = {"sql": render_script(sql, wh.render), "rows": rows, "seconds": _seconds(t0)}
    run.save("results", data)
    counts = {}
    for row in rows:
        counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
    record_execution(run, {"at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "trigger": trigger,
                           "seconds": data["seconds"], "counts": counts,
                           "verdicts": {row["check_id"]: row["verdict"] for row in rows},
                           "observed": {row["check_id"]: row["observed"] for row in rows}}, notify, rows)
    return data
