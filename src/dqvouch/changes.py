"""Structural changes between a run and the database now, and what dqvouch does about them.

A structural change is a change in what a table is: its columns (names, types, descriptions), its description, the
SQL that builds it and the tables it reads from or feeds. What dqvouch derives from those (dq_signals, semantic
roles, usage facts) is not part of it, so a new dqvouch release never looks like a change; values (row counts,
nulls, distributions) never are either: catching a value going wrong is the tests' job. Each table gets its own
signature, so only the tables that changed go back to the model."""
import copy
import datetime
import hashlib
import json
import re
import shutil
import time
from pathlib import Path

from dqvouch import alerts, pipeline
from dqvouch.profiling import DQ_DIMENSIONS
from dqvouch.rules import run_invariant_extractor, with_invariant_items
from dqvouch.selector import select_dimensions
from dqvouch.store import Run, RunBusyError
from dqvouch.validator import (
    validate_dimension_output, validate_reviewed_metric_thresholds, validate_reviewed_test_specifications,
)


def _structure(ctx: dict) -> dict:
    """The facts of an AssetContext that come from the database and the SQL files, not from dqvouch's code."""
    lineage = ctx.get("lineage") or {}

    def names(value):
        return sorted(value) if isinstance(value, list) else value
    return {"description": (ctx.get("asset") or {}).get("description"),
            "columns": sorted([[c.get("name"), c.get("type"), c.get("description")] for c in ctx.get("columns") or []],
                              key=lambda c: str(c[0])),
            "sql": (ctx.get("transformation") or {}).get("raw_sql"),
            "upstream": names(lineage.get("upstream_assets")), "downstream": names(lineage.get("downstream_assets"))}


def table_signatures(asset_contexts: dict) -> dict[str, str]:
    """{table: sha256 of its structure}: equal signatures, the table is what it was."""
    return {table: hashlib.sha256(json.dumps(_structure(ctx), sort_keys=True, default=str).encode("utf-8")).hexdigest()
            for table, ctx in asset_contexts.items()}


def _columns(ctx: dict) -> dict:
    return {c["name"]: c for c in ctx.get("columns") or []}


def _detail(old: dict, new: dict) -> dict:
    """What changed on one table, in the terms a person reads."""
    before, after = _columns(old), _columns(new)
    common = [name for name in before if name in after]
    detail = {
        "columns_added": [{"column": name, "type": after[name].get("type")} for name in after if name not in before],
        "columns_dropped": [name for name in before if name not in after],
        "columns_retyped": [{"column": name, "from": before[name].get("type"), "to": after[name].get("type")}
                            for name in common if before[name].get("type") != after[name].get("type")],
        "descriptions_changed": ((old.get("asset") or {}).get("description")
                                 != (new.get("asset") or {}).get("description")
                                 or any(before[n].get("description") != after[n].get("description") for n in common)),
        "sql_changed": (old.get("transformation") or {}).get("raw_sql")
                       != (new.get("transformation") or {}).get("raw_sql"),
        "lineage_changed": ((_structure(old)["upstream"], _structure(old)["downstream"])
                            != (_structure(new)["upstream"], _structure(new)["downstream"])),
    }
    return detail


def diff_contexts(old_contexts: dict, old_available: list[str], new_contexts: dict) -> dict:
    """{"changed": {table: detail}, "new": [tables], "dropped": [tables]} between a run's AssetContexts and fresh
    ones. A table the run left out on purpose (in `old_available` but not in its contexts) is neither."""
    before, after = table_signatures(old_contexts), table_signatures(new_contexts)
    return {
        "changed": {t: _detail(old_contexts[t], new_contexts[t])
                    for t in sorted(before) if t in after and before[t] != after[t]},
        "new": sorted(t for t in after if t not in set(old_available)),
        "dropped": sorted(t for t in before if t not in after),
    }


def has_changes(changes: dict) -> bool:
    return bool(changes["changed"] or changes["new"] or changes["dropped"])


def targets(changes: dict) -> list[str]:
    """The tables the model proposes tests for again: the changed ones and the new ones."""
    return sorted(set(changes["changed"]) | set(changes["new"]))


def _line(table: str, detail: dict) -> str:
    parts = [f"+ {a['column']} ({a['type']})" for a in detail["columns_added"]]
    parts += [f"- {c}" for c in detail["columns_dropped"]]
    parts += [f"{r['column']}: {r['from']} -> {r['to']}" for r in detail["columns_retyped"]]
    for key, words in (("descriptions_changed", "descriptions changed"), ("sql_changed", "SQL changed"),
                       ("lineage_changed", "lineage changed")):
        if detail[key]:
            parts.append(words)
    return f"{table}: {', '.join(parts)}"


def describe_changes(changes: dict) -> list[str]:
    """One line per table, sorted by table: what a person reads in the alert and on the Review screen."""
    lines = {t: _line(t, d) for t, d in changes["changed"].items()}
    lines.update({t: f"{t}: new table" for t in changes["new"]})
    lines.update({t: f"{t}: dropped" for t in changes["dropped"]})
    return [lines[t] for t in sorted(lines)]


def detect(run: Run, wh) -> dict:
    """{"changes": diff, "context": stage 1's result now} for the run's tables still there plus the tables that
    appeared since (not the ones the run left out with --tables). Reads only metadata and the SQL files."""
    settings, old = run.info()["settings"], run.load("context")
    sql_dir = settings.get("sql_dir")
    if settings.get("context_level") == "C3" and sql_dir and not (Path(sql_dir).is_dir()
                                                                  and any(Path(sql_dir).rglob("*.sql"))):
        # without it every table's SQL would look gone: a moved folder is not a change of the database
        raise ValueError(f"cannot check the structure: the SQL folder {sql_dir} is missing or has no .sql file")
    now = pipeline.build_context(settings, wh)  # one read of the metadata: tables_available and every context
    known = set(old["tables_available"])
    wanted = [t for t in now["tables_available"] if t in old["asset_contexts"] or t not in known]
    if not wanted:  # every table of the run is gone and nothing new appeared
        fresh = {**now, "asset_contexts": {}}
    elif set(wanted) == set(now["asset_contexts"]):
        fresh = now
    else:  # the run covered some tables only: their contexts must be built the same way (lineage, usage)
        fresh = pipeline.build_context(settings, wh, wanted)
    return {"changes": diff_contexts(old["asset_contexts"], old["tables_available"], fresh["asset_contexts"]),
            "context": fresh}


def _without_rationale(item: dict) -> str:
    return json.dumps({k: v for k, v in item.items() if k != "rationale"}, sort_keys=True, default=str)


def merge_dimensions(old: dict, new: dict, targets: set[str], tables: set[str]) -> dict:
    """The dimensions of the new run: the old run's for every table that did not change, the union of old and new
    for the target tables (so nothing a person already relied on is lost), and the cross-table checks of both, new
    ones only when they involve a target. Tables no longer in `tables` are dropped."""
    def items(dims: dict, table: str) -> list:
        return next((list(t.get("dimensions") or []) for t in dims.get("tables", []) if t.get("table") == table), [])

    out_tables = []
    for table in [t.get("table") for t in old.get("tables", [])] + [t.get("table") for t in new.get("tables", [])]:
        if table not in tables or any(e["table"] == table for e in out_tables):
            continue
        merged, seen = [], set()
        for item in items(old, table) + (items(new, table) if table in targets else []):
            key = (str(item.get("dimension", "")).lower(), tuple(item.get("columns") or []))
            if key not in seen:
                seen.add(key)
                merged.append(item)
        if merged:
            out_tables.append({"table": table, "dimensions": merged})
    cross, seen = [], set()
    candidates = list(old.get("cross_table_checks") or []) + [
        c for c in new.get("cross_table_checks") or [] if set(c.get("tables_involved") or []) & targets]
    for check in candidates:
        key = _without_rationale(check)
        if set(check.get("tables_involved") or []) <= tables and key not in seen:
            seen.add(key)
            cross.append(check)
    return {"tables": out_tables, "cross_table_checks": cross}


def merge_tests(kept: list[dict], proposed: list[dict]) -> list[dict]:
    """The kept tests first, then each proposal that checks something new; a proposal whose id is taken gets the
    first free _2, _3... suffix."""
    out = list(kept)
    checks = {pipeline.signature(s) for s in kept}
    ids = {s["id"] for s in kept}
    for spec in proposed:
        if pipeline.signature(spec) in checks:
            continue
        spec_id, n = spec["id"], 2
        while spec_id in ids:
            spec_id, n = f"{spec['id']}_{n}", n + 1
        out.append({**spec, "id": spec_id})
        checks.add(pipeline.signature(spec))
        ids.add(spec_id)
    return out


def _with_free_ids(kept: list[dict], proposed: list[dict], same) -> list[dict]:
    """`kept`, then the proposals `same` does not match with a kept item, ids made unique with _2, _3..."""
    out, ids = list(kept), {i["id"] for i in kept}
    for item in proposed:
        if any(same(item, k) for k in kept):
            continue
        item_id, n = item["id"], 2
        while item_id in ids:
            item_id, n = f"{item['id']}_{n}", n + 1
        out.append({**item, "id": item_id})
        ids.add(item_id)
    return out


def _same_threshold(a: dict, b: dict) -> bool:
    return (a.get("asset"), a.get("check_id"), a.get("metric")) == (b.get("asset"), b.get("check_id"), b.get("metric"))


def _set_aside(rejected: list, asset_of: dict) -> list:
    """The validator's rejections as a person reads them: which test, on which table, and why."""
    out = []
    for r in rejected:
        detail = r.get("detail")
        reason = r.get("reason") or "rejected by the validator"
        if detail and detail != asset_of.get(r.get("scope")):
            reason = f"{reason}: {detail if isinstance(detail, str) else json.dumps(detail, default=str)}"
        out.append({"id": r.get("scope"), "asset": asset_of.get(r.get("scope")), "reason": reason})
    return out


def carry_over(old_run: Run, contexts: dict) -> tuple[list, list, list]:
    """The old run's confirmed tests and metric thresholds, checked again against the tables as they are now:
    (kept tests, kept thresholds, set aside). Only the structure can set one aside (a column or table gone): the
    validator measures them against the values the review saw, and a kept item comes back exactly as reviewed
    (threshold, edits, warnings), starting confirmed and saying which run it comes from. A test that is failing
    on a real defect today must not lose its limit because the structure changed."""
    review = old_run.load("review") or {}
    summary = (old_run.load("tests") or {}).get("profiling_summary") or {}  # what the review was made against
    specs = [dict(s, status="confirmed") for s in review.get("test_specifications") or []
             if s.get("status") != "excluded"]
    thresholds = [dict(t, status="confirmed") for t in review.get("metric_thresholds") or []
                  if t.get("status") != "excluded"]
    spec_report = validate_reviewed_test_specifications(copy.deepcopy(specs), contexts, pipeline._ASSERTION_TYPES,
                                                        profiling_summary=summary)
    threshold_report = validate_reviewed_metric_thresholds(copy.deepcopy(thresholds), contexts, summary)
    asset_of = {i.get("id"): i.get("asset") for i in specs + thresholds}
    reviewed = {i.get("id"): i for i in specs + thresholds}

    def mark(items):
        return [{**reviewed[i["id"]], "status": "confirmed", "_kept_from": old_run.id} for i in items]
    return (mark(spec_report.accepted), mark(threshold_report.accepted),
            _set_aside(spec_report.rejected + threshold_report.rejected, asset_of))


def _neighbours(contexts: dict, goal: set) -> set:
    """Tables one lineage step from a target, either way: the other side of the checks across two tables."""
    linked = set()
    for table, ctx in contexts.items():
        lineage = ctx.get("lineage") or {}
        near = set()
        for key in ("upstream_assets", "downstream_assets"):
            if isinstance(lineage.get(key), list):
                near |= set(lineage[key])
        if table in goal:
            linked |= near
        elif near & goal:
            linked.add(table)
    return (linked & set(contexts)) - goal


def _has_sql(ctx: dict) -> bool:
    return (ctx.get("transformation") or {}).get("raw_sql") not in (None, "", "unknown")


def _source_of(guarantee_id: str, tables) -> str | None:
    """The table whose transformation a SQL guarantee was read from: its id is "<that table, as a name>_<n>"."""
    return next((t for t in tables if re.fullmatch(rf"{re.escape(re.sub(r'[^0-9A-Za-z]+', '_', t))}_\d+",
                                                   guarantee_id)), None)


def rule_contexts(contexts: dict, old_contexts: dict, reread: set) -> dict:
    """The contexts the SQL rules stage leaves for a change's run, before re-reading `reread`: every table as it is
    now, plus the old guarantees whose transformation still exists, still has SQL and is not read again (an input
    requirement stays on its input table even when that table is the one that changed)."""
    out = {t: copy.deepcopy(ctx) for t, ctx in contexts.items()}
    for table, ctx in old_contexts.items():
        if table not in out:
            continue
        for guarantee_id, guarantee in (ctx.get("sql_invariants") or {}).items():
            source = _source_of(guarantee_id, old_contexts)
            if source in contexts and source not in reread and _has_sql(contexts[source]):
                out[table].setdefault("sql_invariants", {})[guarantee_id] = copy.deepcopy(guarantee)
    return out


def propose(old_run: Run, wh, llm, detected: dict, progress=None) -> Run:
    """A new run (stages 1-5) for a structural change: the model only works on the changed and new tables, every
    other table's dimensions and SQL rules come from `old_run`, profiling is measured again in full, and every
    confirmed test of `old_run` is carried (or set aside, with the reason). Nothing runs: it waits for review.
    If anything fails, the new run's folder is removed and the error raised."""
    new = Run.create(old_run.path.parent, old_run.info()["settings"])
    try:
        _build(old_run, new, wh, llm, detected, progress or (lambda message: None))
    except BaseException:
        shutil.rmtree(new.path, ignore_errors=True)
        raise
    return new


def _build(old_run: Run, new: Run, wh, llm, detected: dict, progress) -> None:
    settings, changes = old_run.info()["settings"], detected["changes"]
    contexts = detected["context"]["asset_contexts"]
    goal = targets(changes)
    new.save("context", detected["context"])

    # 2 dimensions: one call on the targets (and their neighbours, for checks across two tables), merged with the
    # old run's, then checked against the tables as they are now (a dropped column leaves its dimensions)
    t0, calls, prompt, raw, rejected = time.monotonic(), [], None, None, []
    found = {"tables": [], "cross_table_checks": []}
    if goal:
        scoped = {t: contexts[t] for t in sorted(set(goal) | _neighbours(contexts, set(goal)))}
        counted, calls = pipeline._counted(llm)
        progress(f"Dimension Selector: {settings['model']} is reading the {len(goal)} changed table(s)")
        dq_json, raw, prompt = select_dimensions(scoped, settings["context_level"], counted,
                                                 pipeline._engine(settings))
        report = validate_dimension_output(dq_json, scoped, DQ_DIMENSIONS)
        found, rejected = report.accepted, report.rejected
    old_dims = (old_run.load("dimensions") or {}).get("dimensions") or {"tables": [], "cross_table_checks": []}
    merged = validate_dimension_output(merge_dimensions(old_dims, found, set(goal), set(contexts)), contexts,
                                       DQ_DIMENSIONS)
    new.save("dimensions", {"prompt": prompt, "raw_response": raw, "dimensions": merged.accepted,
                            "rejected": rejected + merged.rejected, "llm_calls": len(calls),
                            "seconds": round(time.monotonic() - t0, 2), "scoped_to": goal})

    # 3 profiling, in full: the carried tests cite these measurements and their thresholds measure these checks
    pipeline.stage_profiling(new, wh, progress)

    # 4 SQL rules: re-read only the targets' transformations; the other tables keep the old run's rules
    t0 = time.monotonic()
    reason = pipeline.sql_rules_skip_reason(settings, contexts)
    if reason:
        new.save("sql_rules", {"skipped": reason, "seconds": 0.0})
    else:
        old_contexts = (old_run.load("sql_rules") or {}).get("asset_contexts") or {}
        reread = {t for t in goal if _has_sql(contexts[t])}
        rule_ctx = rule_contexts(contexts, old_contexts, reread)
        record, calls = {"calls": [], "accepted": [], "rejected": []}, []
        if reread:
            counted, calls = pipeline._counted(llm)
            record = run_invariant_extractor(rule_ctx, counted, progress, only=reread)
        dims = new.load("profiling")["dimensions"]
        new.save("sql_rules", {"asset_contexts": rule_ctx,
                               "checklist_dimensions": with_invariant_items(dims, rule_ctx),
                               "calls": record["calls"], "accepted": record["accepted"],
                               "rejected": record["rejected"], "llm_calls": len(calls),
                               "seconds": round(time.monotonic() - t0, 2)})

    # 5 tests: proposals for the targets only, then every confirmed test of the old run, checked again
    t0 = time.monotonic()
    rules, profiling = new.load("sql_rules"), new.load("profiling")
    test_contexts = rules.get("asset_contexts") or contexts
    checklist = rules.get("checklist_dimensions") or profiling["dimensions"]
    scoped_checklist = {
        "tables": [t for t in checklist.get("tables", []) if t.get("table") in goal],
        "cross_table_checks": [c for c in checklist.get("cross_table_checks") or []
                               if set(c.get("tables_involved") or []) & set(goal)]}
    if scoped_checklist["tables"] or scoped_checklist["cross_table_checks"]:
        proposed = pipeline.propose_tests(test_contexts, scoped_checklist, profiling["profiling_summary"], llm, wh,
                                          settings, progress)
    else:
        proposed = {"calls": [], "test_specifications": [], "rejected": [], "metric_thresholds": [],
                    "metric_thresholds_rejected": [],
                    "profiling_summary": copy.deepcopy(profiling["profiling_summary"]), "llm_calls": 0}
    kept_specs, kept_thresholds, aside = carry_over(old_run, test_contexts)
    specs = merge_tests(kept_specs, proposed["test_specifications"])
    thresholds = _with_free_ids(kept_thresholds, proposed["metric_thresholds"], _same_threshold)
    new.save("tests", {**proposed, "test_specifications": specs, "metric_thresholds": thresholds,
                       "kept_from": old_run.id, "set_aside": aside, "seconds": round(time.monotonic() - t0, 2)})
    new.update_info(origin={
        "kind": "change", "from_run": old_run.id,
        "detected_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "changes": changes, "summary": describe_changes(changes),
        "kept": len(kept_specs) + len(kept_thresholds),
        "proposed": (len(specs) - len(kept_specs)) + (len(thresholds) - len(kept_thresholds)),
        "set_aside": aside})


SCHEDULE_MODEL_ATTEMPTS = 8  # a busy model gets about half a minute; then the proposal fails and is tried next time


def read_state(run: Run) -> dict:
    """The run's changes.json: its pending run, what it was proposed for, a failed attempt, who superseded it."""
    path = run.path / "changes.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def write_state(run: Run, **fields) -> None:
    """Sets (or, with None, removes) keys of the run's changes.json."""
    state = {k: v for k, v in {**read_state(run), **fields}.items() if v is not None}
    (run.path / "changes.json").write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def _alert(values: dict, redact, subject: str, lines: list[str]) -> list[str]:
    """Sends one alert (subject first, then the lines) to the configured channels; the channels it reached."""
    if not alerts.configured(values):
        return []
    sent = alerts.deliver(values, subject, redact("\n".join([subject, ""] + lines)), redact)
    return sent.get("alerted", [])


def after_execution(run: Run, wh, make_llm, alert_values: dict, redact, progress=None) -> dict:
    """What a scheduled execution does once its tests ran: look for structural changes and, at most once per
    change, propose tests for them in a new run that waits for review, and alert. `make_llm()` gives the model
    (it may raise: no key, no server). Returns {"status", "pending_run", "lines", "alerted", "error"}, status
    one of unchanged, pending_exists, proposed, replaced, detected_only, failed, busy. Never runs a test."""
    outcome = {"status": "unchanged", "pending_run": None, "lines": [], "alerted": [], "error": None}
    try:
        with run.proposal_lock():
            return _check(run, wh, make_llm, alert_values, redact, progress, outcome)
    except RunBusyError:  # the in-app schedule and a `dqvouch rerun` noticed the same change: one proposes
        return {**outcome, "status": "busy"}


def _check(run: Run, wh, make_llm, alert_values: dict, redact, progress, outcome: dict) -> dict:
    """after_execution, with the proposal lock held."""
    detected = detect(run, wh)
    state = read_state(run)
    pending = state.get("pending_run")
    if not has_changes(detected["changes"]):
        if pending:  # the structure went back to what was reviewed: the pending run has nothing left to do
            _replace(run, pending, None)
        if pending or state.get("failed") or state.get("detected"):
            write_state(run, pending_run=None, signatures=None, failed=None, detected=None)
        return outcome
    lines = describe_changes(detected["changes"])
    sigs = table_signatures(detected["context"]["asset_contexts"])
    outcome.update(lines=lines, pending_run=pending)
    if pending and state.get("signatures") == sigs:
        return {**outcome, "status": "pending_exists"}
    n = len(lines)
    if not run.info()["settings"].get("propose_on_change", True):
        outcome["status"] = "detected_only"
        if (state.get("detected") or {}).get("signatures") != sigs:
            outcome["alerted"] = _alert(alert_values, redact, f"dqvouch: structure changed in {n} table(s)",
                                        lines + ["", "Proposing is off (propose_on_change = false): start a new "
                                                     "run to cover it."])
            write_state(run, detected={"signatures": sigs, "lines": lines})
        return outcome
    try:
        llm = make_llm() if targets(detected["changes"]) else None
        if hasattr(llm, "max_attempts"):  # no one waits on a schedule: an outage must end in "failed", not hang
            llm.max_attempts = SCHEDULE_MODEL_ATTEMPTS
        new = propose(run, wh, llm, detected, progress)
    except Exception as e:
        error = redact(str(e) or type(e).__name__)
        outcome.update(status="failed", error=error)
        if (state.get("failed") or {}).get("signatures") != sigs:
            outcome["alerted"] = _alert(
                alert_values, redact, f"dqvouch: structure changed in {n} table(s), but proposing new tests failed",
                lines + ["", f"{error}. dqvouch will try again at the next execution."])
        write_state(run, failed={"signatures": sigs, "error": error, "lines": lines})
        return outcome
    if pending:
        _replace(run, pending, new.id)
    origin = new.info()["origin"]
    write_state(run, pending_run=new.id, signatures=sigs, failed=None, detected=None,
                detected_at=origin["detected_at"], proposed=origin["proposed"])
    aside = origin["set_aside"]
    body = lines + ["", f"{origin['kept']} confirmed test(s) kept, {len(aside)} set aside, {origin['proposed']} new. "
                        f"Review them in run {new.id} (dqvouch ui)."]
    body += [f"- {a['id']}: {a['reason']}" for a in aside]
    outcome.update(status="replaced" if pending else "proposed", pending_run=new.id,
                   alerted=_alert(alert_values, redact, f"dqvouch: structure changed in {n} table(s), "
                                                        f"{origin['proposed']} new test(s) to review", body))
    return outcome


def _replace(run: Run, pending: str, by: str | None) -> None:
    """Marks an earlier pending run as replaced (kept on disk, shown as replaced), if it is still there."""
    path = run.path.parent / pending
    if (path / "run.json").is_file():
        Run.open(path).update_info(replaced_by=by or "the reviewed structure")


def hand_over(new_run: Run) -> bool:
    """Once a change's run is reviewed, the daily executions move to it: the old run's schedule moves over, its
    history comes first in the new run's (so the trends of the kept tests, same ids, continue), and the old run
    is marked superseded (which `current` follows, so cron lines naming the old run keep working). Only for a
    reviewed run made by a change that is still the one waiting (not replaced by a later change). True when the
    executions moved (or had already moved) to `new_run`."""
    info = new_run.info()
    origin = info.get("origin") or {}
    old_path = new_run.path.parent / str(origin.get("from_run"))
    if origin.get("kind") != "change" or "review" not in new_run.completed() or not (old_path / "run.json").is_file():
        return False
    old = Run.open(old_path)
    state = read_state(old)
    if state.get("superseded_by") == new_run.id:
        return True
    if info.get("replaced_by") or (state.get("pending_run") and state["pending_run"] != new_run.id):
        return False  # a later change replaced it: the run that replaced it is the one to review
    schedule = old.path / "schedule.json"
    if schedule.exists() and not (new_run.path / "schedule.json").exists():
        schedule.replace(new_run.path / "schedule.json")
    history = old.path / "history.jsonl"
    if history.exists():
        mine = new_run.path / "history.jsonl"
        own = mine.read_text(encoding="utf-8") if mine.exists() else ""
        mine.write_text(history.read_text(encoding="utf-8") + own, encoding="utf-8")
    write_state(old, superseded_by=new_run.id, pending_run=None, signatures=None)
    return True


def current(run: Run) -> Run:
    """The run whose tests are executed in place of `run`: follows superseded_by while the next run exists and
    was reviewed (a run still waiting for review is never executed)."""
    seen = {run.id}
    while True:
        nxt = read_state(run).get("superseded_by")
        path = run.path.parent / str(nxt)
        if not nxt or nxt in seen or not (path / "run.json").is_file() or "review" not in Run.open(path).completed():
            return run
        run = Run.open(path)
        seen.add(run.id)
