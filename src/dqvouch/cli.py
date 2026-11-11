"""Command line: `dqvouch run --config dqvouch.toml`. Runs the seven stages in order and prints the verdicts.
--level, --repeat and the --no-* switches exist to reproduce the thesis experiments."""
import argparse
import datetime
import json
import re
from pathlib import Path

from dqvouch import alerts
from dqvouch.config import CONTEXT_LEVELS, DatabaseSettings, Secrets, llm_api_key, load_settings
from dqvouch.llm import QuotaExhaustedError, make_llm
from dqvouch.pipeline import (
    export_sql, record_execution, stage_context, stage_dimensions, stage_profiling, stage_results, stage_review, stage_sql_rules,
    stage_tests,
)
from dqvouch.render_sql import print_results_report
from dqvouch.store import Run, RunBusyError
from dqvouch.warehouse import WRITE_WARNING, connect

REVIEW_INSTRUCTIONS = (
    "Review each item below before it gets checked as pass/fail. Two sections: "
    "'test_specifications' (rendered into their own SQL and run against the source "
    "table) and 'metric_thresholds' (compared directly against a value already "
    "computed by this run's profiling pass -- no separate SQL). You can, for either: "
    "(1) edit an existing 'threshold' (test_specifications) or 'operator'/'value' "
    "(metric_thresholds) if the AI's number looks wrong; "
    "(2) add a 'threshold' block to a test_specification that doesn't have one, e.g. "
    "{\"metric\": \"failed_rows\", \"operator\": \"equals\", \"value\": 0}, "
    "if you know the right limit -- you do NOT need to add an 'evidence' entry to "
    "justify it, your review is the evidence; "
    "(3) set \"status\": \"excluded\" on any item you don't want to run at all; "
    "(4) leave an item untouched to run it as-is (a test_specification with no threshold "
    "still executes and reports its observed value, just with no verdict); "
    "(5) an item with 'review_warnings' is one the observed data contradicts: the validator moved its "
    "threshold to 'suspended_threshold' so it runs without a verdict. Either the rule is wrong for this "
    "data (exclude it) or the data has a real problem (move 'suspended_threshold' back to 'threshold'). "
    "Do not rename or add references to columns/tables/assertion types/check_ids/metrics "
    "that don't already appear here -- those are re-checked after you save and will be "
    "dropped if invalid."
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dqvouch", description="An LLM proposes data quality tests, code "
                                                                 "validates them, you vouch for them.")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run every stage and print the verdicts")
    run.add_argument("--config", required=True, help="path to dqvouch.toml")
    run.add_argument("--level", nargs="+", choices=CONTEXT_LEVELS,
                     help="context level(s): C1 structure, C2 + descriptions, C3 + SQL and lineage (default C3)")
    run.add_argument("--repeat", type=int, default=1, help="repeat every level N times (measures model variance)")
    run.add_argument("--tables", nargs="+", metavar="SCHEMA.TABLE", help="only these tables")
    run.add_argument("--no-signals", action="store_true", help="leave the code-extracted dq_signals out")
    run.add_argument("--no-sql-rules", action="store_true", help="skip the SQL Rule Extractor")
    run.add_argument("--no-repair", action="store_true", help="skip the repair pass")
    run.add_argument("--no-review", action="store_true",
                     help="execute the proposed tests without the review pause (reproducing experiments)")
    rerun = sub.add_parser("rerun", help="execute a reviewed run's confirmed tests again (for cron, Task "
                                         "Scheduler, Airflow or CI); exit 0 all fine, 1 a test failed or errored, "
                                         "2 it could not run")
    rerun.add_argument("run_dir", help="the run's folder")
    rerun.add_argument("--env-file", default=".env", help="where the database password is saved")
    export = sub.add_parser("export", help="write a reviewed run's confirmed tests as one SQL file")
    export.add_argument("run_dir", help="the run's folder")
    export.add_argument("-o", "--output", help="the file to write (default: print it)")
    ui = sub.add_parser("ui", help="open the step-by-step web interface in the browser")
    ui.add_argument("--config", help="a dqvouch.toml that fills in the first screen")
    ui.add_argument("--runs-dir", help="where runs are saved (default: the config's runs_dir, otherwise ./runs)")
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--env-file", default=".env", help="where the API key and the database password are saved")
    ui.add_argument("--no-browser", action="store_true", help="do not open the browser")
    return parser


def _report(stage: str, data: dict) -> None:
    calls = f", {data['llm_calls']} LLM call(s)" if "llm_calls" in data else ""
    skipped = f" -- skipped: {data['skipped']}" if "skipped" in data else ""
    print(f"[{stage}] {data.get('seconds', 0)}s{calls}{skipped}")


def _review(run: Run, tests: dict, interactive: bool) -> dict:
    specs, thresholds = tests["test_specifications"], tests["metric_thresholds"]
    if interactive:
        path = run.path / "review_input.json"
        path.write_text(json.dumps({"instructions": REVIEW_INSTRUCTIONS, "test_specifications": specs,
                                    "metric_thresholds": thresholds}, indent=2, default=str), encoding="utf-8")
        print(f"\n{len(specs)} test(s) and {len(thresholds)} metric threshold(s) written to {path}")
        input("Edit the file, save it, then press Enter... ")
        edited = json.loads(path.read_text(encoding="utf-8"))
        specs = edited.get("test_specifications", specs)
        thresholds = edited.get("metric_thresholds", thresholds)
    return stage_review(run, specs, thresholds)


def _run(args, llm) -> int:
    settings = load_settings(args.config)
    settings.signals = settings.signals and not args.no_signals
    settings.sql_rules = settings.sql_rules and not args.no_sql_rules
    settings.repair = settings.repair and not args.no_repair
    if args.repeat < 1:
        raise ValueError("--repeat must be at least 1")
    if llm is None:
        key = llm_api_key(settings.provider)
        if not key and settings.provider == "gemini":  # a local model usually needs no key
            print("GEMINI_API_KEY is not set (environment or .env)")
            return 2
        llm = make_llm(settings.public_dict(), key)
    levels = args.level or [settings.context_level]
    interactive = not args.no_review and len(levels) == 1 and args.repeat == 1
    db = settings.database
    try:
        wh = connect(db)
    except Exception as e:
        raise ValueError(f"cannot connect to {db.engine} at {db.host}:{db.port}: {e}") from e
    try:
        if wh.can_write(db.schemas):
            print(f"warning: {WRITE_WARNING}")
        for repetition in range(1, args.repeat + 1):
            for level in levels:
                settings.context_level = level
                run = Run.create(settings.runs_dir, settings)
                print(f"\n=== Run {run.path} (level {level}, repetition {repetition}/{args.repeat}) ===")
                _report("context", stage_context(run, wh, args.tables))
                _report("dimensions", stage_dimensions(run, llm))
                _report("profiling", stage_profiling(run, wh))
                _report("sql_rules", stage_sql_rules(run, llm))
                tests = stage_tests(run, llm, wh)
                _report("tests", tests)
                _report("review", _review(run, tests, interactive))
                results = stage_results(run, wh)
                _report("results", results)
                print_results_report(results["rows"])
    finally:
        wh.close()
    return 0


def _cron_quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def os_schedule_lines(exe: str, run_dir: str, env_file: str, every_minutes: int) -> dict:
    """A crontab line and a Windows `schtasks` command that run `dqvouch rerun` about every `every_minutes`
    (rounded to what each tool can express: minutes below an hour, then whole hours, then whole days)."""
    n = max(1, int(every_minutes))
    if n >= 1440:
        days = max(1, round(n / 1440))
        cron_when, task_when = f"0 0 */{days} * *", f"/sc daily /mo {days}"
    elif n < 60:
        cron_when, task_when = f"*/{n} * * * *", f"/sc minute /mo {n}"
    else:
        cron_when, task_when = f"0 */{min(23, round(n / 60))} * * *", f"/sc minute /mo {n}"
    cron = f"{cron_when} {_cron_quote(exe)} rerun {_cron_quote(run_dir)} --env-file {_cron_quote(env_file)}"
    name = re.split(r"[\\/]", run_dir.rstrip("\\/"))[-1]
    command = f'\\"{exe}\\" rerun \\"{run_dir}\\" --env-file \\"{env_file}\\"'
    schtasks = f'schtasks /create /tn "dqvouch {name}" {task_when} /tr "{command}"'
    return {"cron": cron, "schtasks": schtasks}


def _rerun(args) -> int:
    path = Path(args.run_dir)
    if not (path / "run.json").is_file():
        print(f"error: no dqvouch run in {path}")
        return 2
    run = Run.open(path)
    if "review" not in run.completed():
        print(f"error: run {run.id} has no review yet: confirm its tests first (dqvouch ui)")
        return 2
    secrets = Secrets(args.env_file)
    info = run.info()
    db = DatabaseSettings(**info["settings"]["database"], password=secrets.get("DQVOUCH_DB_PASSWORD") or "")
    notify = alerts.notifier(secrets.alert_values(), secrets.redact, info)

    def failed(message: str) -> int:
        message = secrets.redact(message)
        record_execution(run, {"at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "trigger": "command",
                               "error": message}, notify)
        print(f"error: {message}")
        return 2
    try:
        with run.execution_lock():
            try:
                wh = connect(db)
            except Exception as e:
                return failed(f"cannot connect to {db.engine} at {db.host}:{db.port}: {e}")
            try:
                if wh.can_write(db.schemas):
                    print(f"warning: {WRITE_WARNING}")
                rows = stage_results(run, wh, trigger="command", notify=notify)["rows"]
            except Exception as e:
                return failed(str(e) or type(e).__name__)
            finally:
                wh.close()
    except RunBusyError as e:
        print(f"error: {e}")
        return 2
    print_results_report(rows)
    return 1 if any(r["verdict"] in ("fail", "error") for r in rows) else 0


def _export(args) -> int:
    path = Path(args.run_dir)
    if not (path / "run.json").is_file():
        print(f"error: no dqvouch run in {path}")
        return 2
    try:
        text = export_sql(Run.open(path))
    except ValueError as e:
        print(f"error: nothing to export yet: {e}")
        return 2
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8", newline="\n")
        print(f"{text.count('-- CHECK_ID:')} queries written to {args.output}")
    else:
        print(text)
    return 0


def _ui(args) -> int:
    import threading
    import webbrowser

    import uvicorn  # the web stack is only loaded for `dqvouch ui`

    from dqvouch.server import create_app
    runs_dir = args.runs_dir or (load_settings(args.config).runs_dir if args.config else "runs")
    app = create_app(runs_dir, args.env_file, args.config)
    url = f"http://127.0.0.1:{args.port}"
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, [url]).start()
    print(f"dqvouch is running at {url} (Ctrl+C to stop)")
    uvicorn.run(app, host="127.0.0.1", port=args.port)  # this computer only
    return 0


def main(argv: list[str] | None = None, llm=None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "rerun":
            return _rerun(args)
        if args.command == "export":
            return _export(args)
        return _ui(args) if args.command == "ui" else _run(args, llm)
    except QuotaExhaustedError as e:
        print(e)
        return 2
    except ValueError as e:
        print(f"error: {e}")
        return 1
