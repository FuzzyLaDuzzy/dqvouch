"""Local web API behind the step-by-step interface (`dqvouch ui`).

Each stage runs in a background thread, one at a time, and saves into the run folder exactly as the command line
does; the browser polls GET /api/runs/{id} for progress. Secrets live only in the .env file (see config.Secrets):
no response, run file or error message carries them.
"""
import contextlib
import json
import os
import shutil
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from dqvouch import alerts, changes, pipeline
from dqvouch.config import ALERT_NAMES, SECRET_NAMES, DatabaseSettings, Secrets, Settings, load_settings
from dqvouch import llm as llm_module
from dqvouch.llm import QuotaExhaustedError, list_models, list_openai_models
from dqvouch.render_sql import _describe_assertion, expected_value_str
from dqvouch.cli import os_schedule_lines
from dqvouch.store import STAGES, Run, RunBusyError
from dqvouch.validator import _get_evidence_value, _get_profile_evidence_value
from dqvouch.warehouse import WRITE_WARNING, connect

STATIC = Path(__file__).resolve().parent / "static"
LLM_STAGES = {"dimensions", "sql_rules", "tests"}
DB_STAGES = {"context", "profiling", "tests", "results"}
DEFAULT_SETTINGS = {
    "database": {"engine": "postgres", "host": "localhost", "port": 5432, "user": "", "database": "", "schemas": [],
                 "query_timeout_seconds": 600},
    "sql_dir": None, "model": Settings.model, "provider": Settings.provider, "base_url": None,
}


class _JobError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def _display(spec: dict, contexts: dict, summary: dict) -> dict:
    """What the review screens show for one specification: readable text, expected value, and each evidence path
    with the fact it resolves to (the observed value) when it resolves."""
    evidence = []
    for entry in spec.get("evidence") or []:
        path = entry.get("path", "") if isinstance(entry, dict) else str(entry)
        try:
            found, value = (_get_profile_evidence_value(summary, path) if path.startswith("profile:")
                            else _get_evidence_value(contexts, spec.get("asset"), path))
        except Exception:  # a malformed path is just "not resolved" here; the validator already judged it
            found, value = False, None
        evidence.append({"path": path, "value": value if found else None})
    return {"description": _describe_assertion(spec), "expected": expected_value_str(spec), "evidence": evidence}


def _as_reviewed(items):
    """The review as the validator sees it: without our display fields, and with every item the person did not
    explicitly confirm marked excluded -- so nothing runs unless it was confirmed, whoever sends the request."""
    if not isinstance(items, list):
        return items
    return [{**{k: v for k, v in i.items() if not k.startswith("_")},  # _display, _previous: the screen's own
             "status": "confirmed" if i.get("status") == "confirmed" else "excluded"} if isinstance(i, dict) else i
            for i in items]


class _Static(StaticFiles):
    """The built interface. Its scripts have content-hashed names and may be cached; the page that names them must
    not be, or after an update the browser asks for scripts that no longer exist and shows a blank page."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        if response.media_type == "text/html":
            response.headers["Cache-Control"] = "no-cache"
        return response


def _schedule(run: Run) -> dict | None:
    path = run.path / "schedule.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def create_app(runs_dir, env_path=".env", config_path=None, llm_factory=None, schedule_check_seconds=30) -> FastAPI:
    """schedule_check_seconds: how often scheduled executions are looked for (None: never, for tests)."""
    runs_dir = Path(runs_dir)
    initial = load_settings(config_path) if config_path else None
    secrets = Secrets(env_path, defaults={"DQVOUCH_DB_PASSWORD": initial.database.password} if initial else None)
    make_llm = llm_factory or llm_module.make_llm  # (run settings, key) -> llm
    lock = threading.Lock()
    state = {"job": None}

    app = FastAPI(title="dqvouch")
    # Only this machine's browser, by name: a page elsewhere that points a DNS name at 127.0.0.1 is refused.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    @app.middleware("http")
    async def this_machine_only(request: Request, call_next):
        # A page on any other site can still send a "simple" POST to 127.0.0.1 (a form, or fetch in no-cors mode).
        # It cannot send JSON without a CORS preflight, which this server never answers, and its Origin is not
        # this machine: anything that changes state must pass both checks.
        if request.method not in ("GET", "HEAD"):
            origin = request.headers.get("origin")
            if origin is not None and urlsplit(origin).hostname not in ("127.0.0.1", "localhost"):
                return JSONResponse(status_code=403, content={"detail": "requests from other sites are refused"})
            if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
                return JSONResponse(status_code=415, content={"detail": "send JSON (Content-Type: application/json)"})
        return await call_next(request)

    redact = secrets.redact

    def open_run(run_id: str) -> Run:
        path = runs_dir / run_id
        if path.resolve().parent != runs_dir.resolve() or not (path / "run.json").is_file():
            raise HTTPException(404, f"no run {run_id!r}")
        return Run.open(path)

    def check_stage(stage: str) -> None:
        if stage not in STAGES:
            raise HTTPException(404, f"no stage {stage!r}")

    def database(db: dict) -> DatabaseSettings:
        fields = {k: v for k, v in (db or {}).items() if k != "password"}
        try:
            return DatabaseSettings(**fields, password=secrets.get("DQVOUCH_DB_PASSWORD") or "")
        except TypeError as e:
            raise HTTPException(422, f"invalid database settings: {e}") from e

    def list_runs() -> list[dict]:
        runs = []
        for path in runs_dir.iterdir() if runs_dir.is_dir() else []:
            if (path / "run.json").is_file():
                run = Run.open(path)
                info = run.info()
                runs.append({"id": run.id, "created_at": info["created_at"],
                             "engine": info["settings"]["database"]["engine"],
                             "schemas": info["settings"]["database"]["schemas"], "completed": run.completed(),
                             "every_minutes": (_schedule(run) or {}).get("every_minutes"),
                             "origin": info.get("origin"), "replaced_by": info.get("replaced_by"),
                             "changes": changes.read_state(run)})
        return sorted(runs, key=lambda r: r["id"], reverse=True)

    def current_job(run_id: str):
        with lock:
            job = state["job"]
            return dict(job) if job and job["run_id"] == run_id else None

    @app.get("/api/settings")
    def get_settings():
        if initial:
            s = initial.public_dict()
        else:
            runs = list_runs()
            s = open_run(runs[0]["id"]).info()["settings"] if runs else DEFAULT_SETTINGS
        return {"settings": {"database": s["database"], "sql_dir": s.get("sql_dir"), "model": s.get("model"),
                             "provider": s.get("provider", "gemini"), "base_url": s.get("base_url"),
                             "propose_on_change": s.get("propose_on_change", True)},
                "secrets": secrets.status(), "env_file": str(secrets.env_path.resolve())}

    @app.put("/api/secrets/{name}")
    def put_secret(name: str, body: dict = Body(...)):
        if name not in SECRET_NAMES:
            raise HTTPException(404, f"no secret {name!r}")
        value = body.get("value")
        if not isinstance(value, str) or not value.strip():
            raise HTTPException(422, "the value is empty")
        secrets.save(name, value.strip())
        return secrets.status()

    def alert_settings() -> dict:
        status = secrets.status()
        return {"values": secrets.plain_values(), "secrets": {n: status[n] for n in ALERT_NAMES if n in SECRET_NAMES}}

    @app.get("/api/alerts")
    def get_alerts():
        return alert_settings()

    @app.put("/api/alerts")
    def put_alerts(body: dict = Body(...)):
        """{name: value} for the alert settings; an empty value removes one."""
        for name, value in body.items():
            if name not in ALERT_NAMES or not isinstance(value, str):
                raise HTTPException(422, f"{name!r} is not an alert setting (or its value is not text)")
            if name == "DQVOUCH_SMTP_PORT" and value.strip() and not value.strip().isdigit():
                raise HTTPException(422, "the SMTP port is a number, usually 587 (STARTTLS) or 465 (SSL)")
        for name, value in body.items():
            if value.strip():
                secrets.save(name, value.strip())
            else:
                secrets.clear(name)
        return alert_settings()

    @app.post("/api/alerts/test")
    def test_alert():
        values = secrets.alert_values()
        if not alerts.configured(values):
            return {"ok": False, "sent": [], "error": "Set a webhook URL, or the SMTP host and recipients, first."}
        result = alerts.deliver(values, "dqvouch: test alert",
                                "This is a test alert from dqvouch. Alerts like this one say when a test starts "
                                "failing or an execution cannot run.", redact)
        return {"ok": "alert_error" not in result, "sent": result.get("alerted", []),
                "error": result.get("alert_error")}

    @app.post("/api/connection/test")
    def test_connection(body: dict = Body(...)):
        db = database(body.get("database"))
        try:
            wh = connect(db)
            try:
                rows = wh.fetch_metadata(db.schemas)
                can_write = wh.can_write(db.schemas)
            finally:
                wh.close()
        except Exception as e:
            return {"ok": False, "error": redact(str(e) or type(e).__name__)}
        return {"ok": True, "tables": len({(r["database"], r["table"]) for r in rows}), "can_write": can_write,
                "warning": WRITE_WARNING if can_write else None}

    @app.post("/api/gemini/test")
    def test_gemini():
        key = secrets.get("GEMINI_API_KEY")
        if not key:
            return {"ok": False, "error": "Set the Gemini API key first."}
        try:
            return {"ok": True, "models": list_models(key)}
        except Exception as e:
            return {"ok": False, "error": redact(str(e) or type(e).__name__)}

    @app.post("/api/openai/test")
    def test_openai(body: dict = Body(...)):
        base_url = body.get("base_url")
        if not isinstance(base_url, str) or not base_url.strip():
            return {"ok": False, "error": "Type the server's address first, e.g. http://localhost:11434/v1."}
        try:
            return {"ok": True, "models": list_openai_models(base_url.strip(), secrets.get("OPENAI_API_KEY"))}
        except Exception as e:
            return {"ok": False, "error": redact(str(e) or type(e).__name__)}

    @app.get("/api/runs")
    def get_runs():
        return list_runs()

    @app.post("/api/runs", status_code=201)
    def create_run(body: dict = Body(...)):
        sql_dir = body.get("sql_dir") or None
        if sql_dir:
            folder = Path(sql_dir).expanduser()
            if not folder.is_dir():
                raise HTTPException(422, f"SQL folder not found: {sql_dir}")
            sql_dir = str(folder.resolve())
        # The interface always runs the thesis's final configuration; levels and ablations stay on the command line.
        settings = Settings(database=database(body.get("database")), model=body.get("model") or Settings.model,
                            provider=body.get("provider") or Settings.provider, base_url=body.get("base_url") or None,
                            sql_dir=sql_dir, runs_dir=str(runs_dir), context_level="C3", signals=True,
                            sql_rules=True, repair=True, propose_on_change=body.get("propose_on_change", True))
        try:
            settings.validate()
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        return {"id": Run.create(runs_dir, settings).id}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str):
        run = open_run(run_id)
        return {"info": run.info(), "completed": run.completed(), "job": current_job(run_id),
                "changes": changes.read_state(run)}

    @app.get("/api/runs/{run_id}/stages/{stage}")
    def get_stage(run_id: str, stage: str):
        run = open_run(run_id)
        check_stage(stage)
        data = run.load(stage)
        if data is None:
            raise HTTPException(404, f"the {stage} stage has not run yet")
        if stage in ("tests", "review"):
            contexts = pipeline._contexts(run)
            summary = (run.load("tests") or {}).get("profiling_summary") or {}
            earlier = pipeline.earlier_decisions(run, runs_dir) if stage == "tests" else {}
            for spec in data.get("test_specifications") or []:
                if isinstance(spec, dict):
                    spec["_display"] = _display(spec, contexts, summary)
                    if pipeline.signature(spec) in earlier:
                        spec["_previous"] = earlier[pipeline.signature(spec)]
            # kept from the run a structural change started from: they start confirmed, as they were
            for item in (data.get("test_specifications") or []) + (data.get("metric_thresholds") or []):
                if isinstance(item, dict) and item.get("_kept_from"):
                    item["_previous"] = {"status": "confirmed", "run": item["_kept_from"],
                                         **({"threshold": item.get("threshold")} if "assertion" in item else {})}
        return data

    @app.post("/api/runs/{run_id}/stages/{stage}")
    def post_stage(run_id: str, stage: str, body: dict = Body(default={})):
        run = open_run(run_id)
        check_stage(stage)
        completed = run.completed()
        missing = next((s for s in STAGES[:STAGES.index(stage)] if s not in completed), None)
        if missing:
            raise HTTPException(409, f"run the '{missing}' stage first")
        if stage in ("review", "results") and run.locked():
            raise HTTPException(409, "the tests of this run are being executed by another dqvouch (a schedule or "
                                     "`dqvouch rerun`); try again when it finishes. If none is running (dqvouch was "
                                     f"stopped in the middle), delete {run.path / 'execution.lock'}.")
        if stage == "review":  # the person's decisions: quick, no database and no model
            with lock:
                if state["job"] and state["job"]["status"] == "running":
                    raise HTTPException(409, "a stage is already running; wait for it to finish")
                try:
                    undecided = [i for i in body.get("undecided") or [] if isinstance(i, str)]
                    data = pipeline.stage_review(run, _as_reviewed(body.get("test_specifications")),
                                                 _as_reviewed(body.get("metric_thresholds", [])), "interface",
                                                 undecided)
                except ValueError as e:
                    raise HTTPException(422, str(e)) from e
                origin = run.info().get("origin") or {}
                if origin.get("kind") == "change" and changes.hand_over(run):  # the executions move to this run
                    data = {**data, "handed_over_from": origin.get("from_run")}
                return data
        return JSONResponse(status_code=202, content={"job": start_job(run, stage, {**body, "trigger": "manual"})})

    def start_job(run: Run, stage: str, body: dict) -> dict:
        """Runs a stage in the background; one at a time on the whole server."""
        with lock:
            if state["job"] and state["job"]["status"] == "running":
                raise HTTPException(409, "a stage is already running; wait for it to finish")
            job = {"run_id": run.id, "stage": stage, "status": "running", "message": f"Starting {stage}…",
                   "note": None, "started": time.time(), "error": None, "error_kind": None}
            state["job"] = job
        threading.Thread(target=work, args=(job, run, stage, body), daemon=True).start()
        return dict(job)

    @app.get("/api/runs/{run_id}/export.sql")
    def export(run_id: str):
        run = open_run(run_id)
        try:
            text = pipeline.export_sql(run)
        except ValueError as e:
            raise HTTPException(409, f"nothing to export yet: {e}") from e
        return PlainTextResponse(text, headers={"Content-Disposition": f'attachment; filename="dqvouch-{run.id}.sql"'})

    @app.get("/api/runs/{run_id}/schedule")
    def get_schedule(run_id: str):
        run = open_run(run_id)
        return {"schedule": _schedule(run), "history": run.history()}

    @app.put("/api/runs/{run_id}/schedule")
    def put_schedule(run_id: str, body: dict = Body(...)):
        """{"every_minutes": n} runs the reviewed tests again every n minutes, from n minutes from now, while this
        server runs; {"every_minutes": null} stops it."""
        run = open_run(run_id)
        every = body.get("every_minutes")
        path = run.path / "schedule.json"
        if every is None:
            path.unlink(missing_ok=True)
            return {"schedule": None}
        if type(every) is not int or every < 1:
            raise HTTPException(422, "every_minutes must be a whole number of minutes, 1 or more")
        if "review" not in run.completed():
            raise HTTPException(409, "review the tests first: a schedule runs the tests you confirmed")
        schedule = {"every_minutes": every,
                    "next_at": (datetime.now(timezone.utc) + timedelta(minutes=every)).isoformat()}
        path.write_text(json.dumps(schedule), encoding="utf-8")
        return {"schedule": schedule}

    @app.get("/api/runs/{run_id}/schedule/commands")
    def schedule_commands(run_id: str, every_minutes: int = 1440):
        """The lines that run this run's tests with dqvouch closed (cron, Windows Task Scheduler)."""
        run = open_run(run_id)
        if every_minutes < 1:
            raise HTTPException(422, "every_minutes must be 1 or more")
        here = Path(sys.executable).parent / ("dqvouch.exe" if os.name == "nt" else "dqvouch")
        exe = str(here) if here.is_file() else shutil.which("dqvouch") or "dqvouch"
        return os_schedule_lines(exe, str(run.path.resolve()), str(secrets.env_path.resolve()), every_minutes)

    def run_due_schedules(now: datetime | None = None) -> list[str]:
        """Starts the executions that are due (one at a time: a due run waits while another stage runs). An
        execution missed while the server was off runs once, then the schedule continues from now; one that is
        due while another process executes the run is skipped to its next time."""
        now = now or datetime.now(timezone.utc)
        started = []
        for path in sorted(runs_dir.glob("*/schedule.json")):
            run = Run.open(path.parent)
            schedule = _schedule(run)
            if datetime.fromisoformat(schedule["next_at"]) > now or "review" not in run.completed():
                continue
            if not run.locked():
                try:
                    start_job(run, "results", {"trigger": "schedule"})
                except HTTPException:
                    break
                started.append(run.id)
            schedule["next_at"] = (now + timedelta(minutes=schedule["every_minutes"])).isoformat()
            path.write_text(json.dumps(schedule), encoding="utf-8")
        return started

    app.state.run_due_schedules = run_due_schedules
    if schedule_check_seconds:
        def scheduler():
            while True:
                time.sleep(schedule_check_seconds)
                try:
                    run_due_schedules()
                except Exception as e:  # a broken schedule file must not stop the others for good
                    print(f"dqvouch scheduler: {e}")
        threading.Thread(target=scheduler, daemon=True).start()

    def notify(run: Run):
        return alerts.notifier(secrets.alert_values(), redact, run.info())

    def model_for(settings: dict):
        """The model of a run's settings, with this installation's key; _JobError("key") without a Gemini key."""
        local = settings.get("provider", "gemini") == "openai"
        key = secrets.get("OPENAI_API_KEY" if local else "GEMINI_API_KEY")
        if not key and not local:  # a local model usually needs no key
            raise _JobError("key", "Set the Gemini API key on the Configuration screen.")
        return make_llm(settings, key)

    def work(job: dict, run: Run, stage: str, body: dict) -> None:
        settings, wh = run.info()["settings"], None
        held = contextlib.ExitStack()

        def progress(message: str) -> None:
            job.update(message=message, note=None)

        def retrying(note: str | None) -> None:
            job["note"] = note
        try:
            if stage == "results":  # before connecting: a `dqvouch rerun` that started meanwhile is a skip
                held.enter_context(run.execution_lock())
            llm = None
            if stage in LLM_STAGES:
                llm = model_for(settings)
                if hasattr(llm, "on_retry"):
                    llm.on_retry = retrying
            if stage in DB_STAGES:
                db = DatabaseSettings(**settings["database"], password=secrets.get("DQVOUCH_DB_PASSWORD") or "")
                try:
                    wh = connect(db)
                except Exception as e:
                    raise _JobError("connection", f"Cannot connect to {db.engine} at {db.host}:{db.port}: {e}") from e
            if stage == "context":
                pipeline.stage_context(run, wh, body.get("tables"), progress)
            elif stage == "dimensions":
                pipeline.stage_dimensions(run, llm, progress)
            elif stage == "profiling":
                pipeline.stage_profiling(run, wh, progress)
            elif stage == "sql_rules":
                pipeline.stage_sql_rules(run, llm, progress)
            elif stage == "tests":
                pipeline.stage_tests(run, llm, wh, progress)
            elif stage == "results":
                pipeline.stage_results(run, wh, progress, body.get("trigger", "manual"), notify(run))
                if body.get("trigger") == "schedule":  # then look for structural changes, the lock released
                    held.close()
                    job.update(message="Checking for structure changes", note=None)
                    try:
                        job["changes"] = changes.after_execution(run, wh, lambda: model_for(settings),
                                                                 secrets.alert_values(), redact, progress)
                    except Exception as e:  # never turns a finished execution into a failed one
                        job["changes"] = {"status": "failed", "error": redact(str(e) or type(e).__name__)}
            job.update(status="done", message=f"{stage} finished")
        except RunBusyError as e:  # not a failed execution: nothing is recorded, nobody is alerted
            job.update(status="error", error_kind="busy", error=str(e))
        except _JobError as e:
            job.update(status="error", error_kind=e.kind, error=redact(str(e)))
        except QuotaExhaustedError:
            job.update(status="error", error_kind="quota",
                       error=f"The Gemini daily quota for {settings['model']} is used up. Try again tomorrow, or "
                             "start a new run with another model.")
        except Exception as e:
            job.update(status="error", error_kind="error", error=redact(str(e) or type(e).__name__))
        finally:
            if wh is not None:
                wh.close()
            if stage == "results" and job["status"] == "error" and job["error_kind"] != "busy":
                pipeline.record_execution(run, {"at": datetime.now(timezone.utc).isoformat(),
                                                "trigger": body.get("trigger", "manual"), "error": job["error"]},
                                          notify(run))
            held.close()

    if (STATIC / "index.html").is_file():
        app.mount("/", _Static(directory=STATIC, html=True), name="ui")
    return app
