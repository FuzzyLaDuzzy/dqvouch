"""The web API end to end on the demo databases (both engines), with the scripted FakeLLM."""
import json
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from fake_llm import FakeLLM
from fastapi.testclient import TestClient

from conftest import EXAMPLE

from dqvouch.llm import QuotaExhaustedError
from dqvouch import server as server_module
from dqvouch.server import create_app
from dqvouch.store import STAGES, Run


def make_client(tmp_path, config, llm_factory):
    app = create_app(tmp_path / "runs", tmp_path / ".env", config, llm_factory=llm_factory,
                     schedule_check_seconds=None)
    return TestClient(app, base_url="http://127.0.0.1")


@pytest.fixture(autouse=True)
def no_secrets_in_environment(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("DQVOUCH_DB_PASSWORD", raising=False)


@pytest.fixture
def client(tmp_path, demo_config_path, wh):
    c = make_client(tmp_path, demo_config_path, lambda settings, key: FakeLLM())
    assert c.put("/api/secrets/GEMINI_API_KEY", json={"value": "test-key"}).status_code == 200
    return c


def wait(client, run_id, timeout=60):
    deadline = time.monotonic() + timeout
    while True:
        job = client.get(f"/api/runs/{run_id}").json()["job"]
        if job is None or job["status"] != "running":
            return job
        assert time.monotonic() < deadline, job
        time.sleep(0.05)


def run_stage(client, run_id, stage, body=None):
    r = client.post(f"/api/runs/{run_id}/stages/{stage}", json=body or {})
    assert r.status_code == 202, r.text
    job = wait(client, run_id)
    assert job["status"] == "done", job
    return client.get(f"/api/runs/{run_id}/stages/{stage}").json()


def new_run(client):
    r = client.post("/api/runs", json=client.get("/api/settings").json()["settings"])
    assert r.status_code == 201, r.text
    return r.json()["id"]


def through_tests(client, run_id):
    for stage in ("context", "dimensions", "profiling", "sql_rules"):
        run_stage(client, run_id, stage)
    return run_stage(client, run_id, "tests")


def review_and_run(client, run_id, specs, excluded=()):
    reviewed = [{**s, "status": "excluded" if s["id"].split("__")[-1] in excluded else "confirmed"} for s in specs]
    r = client.post(f"/api/runs/{run_id}/stages/review", json={"test_specifications": reviewed, "metric_thresholds": []})
    assert r.status_code == 200, r.text
    return {row["check_id"].split("__")[-1]: row["verdict"] for row in run_stage(client, run_id, "results")["rows"]}


def test_full_flow_through_the_api(client):
    run_id = new_run(client)
    specs = through_tests(client, run_id)["test_specifications"]
    assert specs and all(s["_display"]["description"] for s in specs)
    domain = next(s for s in specs if s["id"].endswith("fact_orders_status_domain"))
    assert "unknown" in str(domain["_display"]["evidence"][0]["value"])
    verdicts = review_and_run(client, run_id, specs)
    assert verdicts == {"orders_trans_order_id_unique": "fail", "fact_orders_status_domain": "fail"}
    assert client.get(f"/api/runs/{run_id}").json()["completed"] == list(STAGES)
    assert [r["id"] for r in client.get("/api/runs").json()] == [run_id]


def test_only_confirmed_tests_run(client):
    run_id = new_run(client)
    specs = through_tests(client, run_id)["test_specifications"]
    assert review_and_run(client, run_id, specs, excluded={"fact_orders_status_domain"}) == {
        "orders_trans_order_id_unique": "fail"}


def test_secrets_never_leave_the_env_file(tmp_path, demo_config_path, wh, monkeypatch):
    keys_used, responses = [], []
    c = make_client(tmp_path, demo_config_path, lambda settings, key: keys_used.append(key) or FakeLLM())
    original = c.request

    def recording(*args, **kwargs):
        response = original(*args, **kwargs)
        responses.append(response.text)
        return response
    monkeypatch.setattr(c, "request", recording)
    c.put("/api/secrets/GEMINI_API_KEY", json={"value": "sk-secret-123"})
    run_id = new_run(c)
    review_and_run(c, run_id, through_tests(c, run_id)["test_specifications"])
    c.get("/api/settings")
    assert keys_used and set(keys_used) == {"sk-secret-123"}  # the saved key is the one the stages use
    assert not any("sk-secret-123" in text for text in responses)
    assert not any("sk-secret-123" in p.read_text(encoding="utf-8") for p in (tmp_path / "runs").rglob("*") if p.is_file())
    assert "sk-secret-123" in (tmp_path / ".env").read_text(encoding="utf-8")


def test_second_stage_while_running_is_refused(tmp_path, demo_config_path, wh):
    release, fake = threading.Event(), FakeLLM()

    def slow(prompt):
        release.wait(30)
        return fake(prompt)
    c = make_client(tmp_path, demo_config_path, lambda settings, key: slow)
    c.put("/api/secrets/GEMINI_API_KEY", json={"value": "test-key"})
    run_id = new_run(c)
    run_stage(c, run_id, "context")
    assert c.post(f"/api/runs/{run_id}/stages/dimensions", json={}).status_code == 202
    assert c.post(f"/api/runs/{run_id}/stages/dimensions", json={}).status_code == 409
    release.set()
    assert wait(c, run_id)["status"] == "done"
    assert len(fake.prompts) == 1


def test_quota_message_and_earlier_stages_kept(tmp_path, demo_config_path, wh):
    def exhausted(prompt):
        raise QuotaExhaustedError("429 RESOURCE_EXHAUSTED GenerateRequestsPerDay")
    c = make_client(tmp_path, demo_config_path, lambda settings, key: exhausted)
    c.put("/api/secrets/GEMINI_API_KEY", json={"value": "test-key"})
    run_id = new_run(c)
    run_stage(c, run_id, "context")
    assert c.post(f"/api/runs/{run_id}/stages/dimensions", json={}).status_code == 202
    job = wait(c, run_id)
    assert job["status"] == "error" and job["error_kind"] == "quota" and "quota" in job["error"]
    assert "new run" in job["error"]  # an existing run's model cannot change: the message says what can be done
    assert c.get(f"/api/runs/{run_id}").json()["completed"] == ["context"]


def test_saved_stages_open_without_database(client, monkeypatch):
    run_id = new_run(client)
    run_stage(client, run_id, "context")
    run_stage(client, run_id, "dimensions")

    def unreachable(db):
        raise OSError("connection refused")
    monkeypatch.setattr("dqvouch.server.connect", unreachable)
    assert client.get(f"/api/runs/{run_id}/stages/context").status_code == 200
    assert client.post(f"/api/runs/{run_id}/stages/profiling", json={}).status_code == 202
    job = wait(client, run_id)
    assert job["error_kind"] == "connection" and "connection refused" in job["error"]


def test_missing_key_is_reported(tmp_path, demo_config_path, wh):
    c = make_client(tmp_path, demo_config_path, lambda settings, key: FakeLLM())
    run_id = new_run(c)
    run_stage(c, run_id, "context")
    assert c.post(f"/api/runs/{run_id}/stages/dimensions", json={}).status_code == 202
    assert wait(c, run_id)["error_kind"] == "key"


def test_stage_order_is_enforced(client):
    r = client.post(f"/api/runs/{new_run(client)}/stages/profiling", json={})
    assert r.status_code == 409 and "run the 'context' stage first" in r.text


def test_connection_test_counts_tables(client):
    database = client.get("/api/settings").json()["settings"]["database"]
    assert client.post("/api/connection/test", json={"database": database}).json()["tables"] == 10
    bad = client.post("/api/connection/test", json={"database": {**database, "engine": "oracle"}}).json()
    assert bad["ok"] is False and bad["error"]


def test_connection_test_reports_write_access(client):
    """The demo user owns the tables: the Configuration screen must warn about it."""
    database = client.get("/api/settings").json()["settings"]["database"]
    answer = client.post("/api/connection/test", json={"database": database}).json()
    assert answer["can_write"] is True and "read" in answer["warning"]


def test_foreign_host_is_refused(client):
    assert client.get("/api/runs", headers={"host": "evil.example"}).status_code == 400


def test_unknown_run_is_404_and_not_a_path(client):
    assert client.get("/api/runs/nope").status_code == 404
    assert client.get("/api/runs/..").status_code == 404


def test_gemini_test_lists_models(client, monkeypatch):
    monkeypatch.setattr("dqvouch.server.list_models", lambda key: ["gemini-a"])
    assert client.post("/api/gemini/test", json={}).json() == {"ok": True, "models": ["gemini-a"]}

    def bad_key(key):
        raise ValueError(f"API key not valid: {key}")
    monkeypatch.setattr("dqvouch.server.list_models", bad_key)
    result = client.post("/api/gemini/test", json={}).json()
    assert result["ok"] is False and "test-key" not in result["error"]  # the key is never echoed back


def test_openai_test_lists_models(client, monkeypatch):
    monkeypatch.setattr("dqvouch.server.list_openai_models", lambda base_url, key=None: [base_url])
    assert client.post("/api/openai/test", json={"base_url": "http://x/v1"}).json() == {"ok": True,
                                                                                        "models": ["http://x/v1"]}

    def down(base_url, key=None):
        raise OSError("connection refused")
    monkeypatch.setattr("dqvouch.server.list_openai_models", down)
    result = client.post("/api/openai/test", json={"base_url": "http://x/v1"}).json()
    assert result["ok"] is False and "refused" in result["error"]


def test_a_run_uses_its_provider_and_key(tmp_path, demo_config_path, wh, monkeypatch):
    """No Gemini key is needed for a local model; its optional key is used but never written to the run."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    used = []
    c = make_client(tmp_path, demo_config_path,
                    lambda settings, key: used.append((settings["provider"], settings["base_url"], key)) or FakeLLM())
    assert c.get("/api/settings").json()["settings"]["provider"] == "gemini"
    body = {**c.get("/api/settings").json()["settings"], "provider": "openai", "base_url": "http://x/v1",
            "model": "llama3"}
    run_id = c.post("/api/runs", json=body).json()["id"]
    run_stage(c, run_id, "context")
    run_stage(c, run_id, "dimensions")
    assert used == [("openai", "http://x/v1", None)]
    c.put("/api/secrets/OPENAI_API_KEY", json={"value": "sk-local-1"})
    run_stage(c, run_id, "dimensions")
    assert used[-1] == ("openai", "http://x/v1", "sk-local-1")
    assert not any("sk-local-1" in p.read_text(encoding="utf-8") for p in (tmp_path / "runs").rglob("*") if p.is_file())
    bad = c.post("/api/runs", json={**body, "base_url": None})
    assert bad.status_code == 422 and "base_url" in bad.text


def test_cross_site_requests_are_refused(client):
    """A page on another site can POST to 127.0.0.1 (its Host passes), but not with JSON without a CORS preflight,
    and its Origin is not this machine: neither kind reaches a stage."""
    run_id = new_run(client)
    run_stage(client, run_id, "context")
    url = f"/api/runs/{run_id}/stages/context"
    form = client.post(url, content=b"", headers={"content-type": "application/x-www-form-urlencoded",
                                                   "origin": "http://evil.example"})
    assert form.status_code == 403
    plain = client.post(url, content=b"{}", headers={"content-type": "text/plain"})
    assert plain.status_code == 415
    foreign = client.post(url, json={}, headers={"origin": "http://evil.example"})
    assert foreign.status_code == 403
    secret = client.put("/api/secrets/DQVOUCH_DB_PASSWORD", content=b"value=x", headers={"origin": "null"})
    assert secret.status_code == 403
    assert client.get(f"/api/runs/{run_id}").json()["completed"] == ["context"]
    assert client.post(url, json={}, headers={"origin": "http://localhost:5173"}).status_code == 202  # the dev UI
    assert wait(client, run_id)["status"] == "done"


def test_review_runs_only_items_marked_confirmed(client):
    run_id = new_run(client)
    specs = through_tests(client, run_id)["test_specifications"]
    unmarked = [{k: v for k, v in s.items() if k != "status"} for s in specs]
    unmarked[1]["status"] = "proposed"
    r = client.post(f"/api/runs/{run_id}/stages/review", json={"test_specifications": unmarked, "metric_thresholds": []})
    assert r.status_code == 200 and r.json()["test_specifications"] == []
    assert run_stage(client, run_id, "results")["rows"] == []


def test_a_schedule_runs_the_confirmed_tests_again_and_keeps_each_result(client):
    run_id = new_run(client)
    specs = through_tests(client, run_id)["test_specifications"]
    review_and_run(client, run_id, specs)
    r = client.put(f"/api/runs/{run_id}/schedule", json={"every_minutes": 60})
    assert r.status_code == 200, r.text
    assert r.json()["schedule"]["every_minutes"] == 60
    run_due = client.app.state.run_due_schedules
    assert run_due() == []  # not due for an hour
    later = datetime.now(timezone.utc) + timedelta(minutes=61)
    assert run_due(later) == [run_id]
    assert wait(client, run_id)["status"] == "done"
    data = client.get(f"/api/runs/{run_id}/schedule").json()
    assert [h["trigger"] for h in data["history"]] == ["schedule", "manual"]  # newest first
    assert any(k.endswith("orders_trans_order_id_unique") and v == "fail"
               for k, v in data["history"][0]["verdicts"].items())
    assert data["history"][0]["counts"]["fail"] == 2
    assert datetime.fromisoformat(data["schedule"]["next_at"]) > later
    assert client.get("/api/runs").json()[0]["every_minutes"] == 60
    assert client.put(f"/api/runs/{run_id}/schedule", json={"every_minutes": None}).json()["schedule"] is None
    assert run_due(later + timedelta(days=2)) == []


def test_a_new_failure_sends_one_alert(client, tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr("dqvouch.alerts.send_webhook", lambda url, text, timeout=10: sent.append(text))
    assert client.put("/api/alerts", json={"DQVOUCH_ALERT_WEBHOOK_URL": "https://hooks.example/abc"}).status_code == 200
    run_id = new_run(client)
    specs = through_tests(client, run_id)["test_specifications"]
    reviewed = [{**s, "status": "confirmed"} for s in specs]
    assert client.post(f"/api/runs/{run_id}/stages/review",
                       json={"test_specifications": reviewed, "metric_thresholds": []}).status_code == 200
    earlier = {"at": "2026-01-01T00:00:00+00:00", "trigger": "manual", "counts": {"pass": len(specs)},
               "verdicts": {s["id"]: "pass" for s in specs}}  # an execution in which every test passed
    (tmp_path / "runs" / run_id / "history.jsonl").write_text(json.dumps(earlier) + "\n", encoding="utf-8")
    run_stage(client, run_id, "results")
    run_stage(client, run_id, "results")
    assert len(sent) == 1 and "started failing" in sent[0] and "orders_trans_order_id_unique" in sent[0]
    history = client.get(f"/api/runs/{run_id}/schedule").json()["history"]
    assert history[1]["alerted"] == ["webhook"] and "alerted" not in history[0]


def test_alert_settings_never_return_secrets(client):
    r = client.put("/api/alerts", json={"DQVOUCH_ALERT_WEBHOOK_URL": "https://hooks.example/s3cr3t",
                                        "DQVOUCH_SMTP_PASSWORD": "pw-123", "DQVOUCH_SMTP_HOST": "smtp.example",
                                        "DQVOUCH_SMTP_TO": "a@example.com"})
    assert r.status_code == 200
    answer = client.get("/api/alerts")
    assert "s3cr3t" not in answer.text and "pw-123" not in answer.text
    assert answer.json()["values"]["DQVOUCH_SMTP_HOST"] == "smtp.example"
    assert answer.json()["secrets"]["DQVOUCH_ALERT_WEBHOOK_URL"]["set"] is True
    assert client.put("/api/alerts", json={"GEMINI_API_KEY": "x"}).status_code == 422
    assert client.put("/api/alerts", json={"DQVOUCH_SMTP_PORT": "abc"}).status_code == 422
    assert client.put("/api/alerts", json={"DQVOUCH_SMTP_HOST": ""}).status_code == 200
    assert client.get("/api/alerts").json()["values"]["DQVOUCH_SMTP_HOST"] == ""


def test_send_a_test_alert(client, monkeypatch):
    assert client.post("/api/alerts/test", json={}).json()["ok"] is False  # nothing configured yet
    sent = []
    monkeypatch.setattr("dqvouch.alerts.send_webhook", lambda url, text, timeout=10: sent.append(url))
    client.put("/api/alerts", json={"DQVOUCH_ALERT_WEBHOOK_URL": "https://hooks.example/abc"})
    assert client.post("/api/alerts/test", json={}).json() == {"ok": True, "sent": ["webhook"], "error": None}
    assert sent == ["https://hooks.example/abc"]


def test_a_new_run_starts_with_the_last_interface_review(client, tmp_path):
    first = new_run(client)
    specs = through_tests(client, first)["test_specifications"]
    domain = next(s for s in specs if s["id"].endswith("fact_orders_status_domain"))
    unique = next(s for s in specs if s["id"].endswith("orders_trans_order_id_unique"))
    edited = {**unique["threshold"], "operator": "less_than_or_equal", "value": 5}
    reviewed = [{**domain, "status": "excluded"}, {**unique, "status": "confirmed", "threshold": edited}]
    assert client.post(f"/api/runs/{first}/stages/review",
                       json={"test_specifications": reviewed, "metric_thresholds": []}).status_code == 200
    assert Run.open(tmp_path / "runs" / first).load("review")["reviewed_in"] == "interface"
    second = new_run(client)
    previous = {s["id"].split("__")[-1]: s.get("_previous") for s in through_tests(client, second)["test_specifications"]}
    assert previous["fact_orders_status_domain"] == {"status": "excluded", "threshold": None, "run": first}
    assert previous["orders_trans_order_id_unique"] == {"status": "confirmed", "threshold": edited, "run": first}
    # sending the screen's own fields back is harmless: they are not part of the review
    again = [{**s, "status": "confirmed"} for s in through_tests(client, second)["test_specifications"]]
    assert client.post(f"/api/runs/{second}/stages/review",
                       json={"test_specifications": again, "metric_thresholds": []}).status_code == 200
    assert not any("_previous" in s for s in Run.open(tmp_path / "runs" / second).load("review")["submitted"]
                   ["test_specifications"])


def test_tests_left_undecided_are_not_carried_over(client):
    """Undecided tests do not run (they are sent as excluded), but nobody excluded them: a new run must not say so."""
    first = new_run(client)
    specs = through_tests(client, first)["test_specifications"]
    domain = next(s for s in specs if s["id"].endswith("fact_orders_status_domain"))
    unique = next(s for s in specs if s["id"].endswith("orders_trans_order_id_unique"))
    body = {"test_specifications": [{**domain, "status": "excluded"}, {**unique, "status": "excluded"}],
            "metric_thresholds": [], "undecided": [unique["id"]]}
    assert client.post(f"/api/runs/{first}/stages/review", json=body).status_code == 200
    second = new_run(client)
    previous = {s["id"].split("__")[-1]: s.get("_previous") for s in through_tests(client, second)["test_specifications"]}
    assert previous["fact_orders_status_domain"]["status"] == "excluded"
    assert previous["orders_trans_order_id_unique"] is None


def test_the_confirmed_tests_download_as_sql(client):
    run_id = new_run(client)
    assert client.get(f"/api/runs/{run_id}/export.sql").status_code == 409  # nothing reviewed yet
    review_and_run(client, run_id, through_tests(client, run_id)["test_specifications"])
    r = client.get(f"/api/runs/{run_id}/export.sql")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert f'filename="dqvouch-{run_id}.sql"' in r.headers["content-disposition"]
    assert r.text.count("-- CHECK_ID:") == 2


def test_a_locked_run_is_not_executed_twice(client, tmp_path):
    """While `dqvouch rerun` (another process) executes a run, the interface neither runs it nor schedules it."""
    run_id = new_run(client)
    review_and_run(client, run_id, through_tests(client, run_id)["test_specifications"])
    assert client.put(f"/api/runs/{run_id}/schedule", json={"every_minutes": 60}).status_code == 200
    later = datetime.now(timezone.utc) + timedelta(minutes=61)
    with Run.open(tmp_path / "runs" / run_id).execution_lock():
        busy = client.post(f"/api/runs/{run_id}/stages/results", json={})
        assert busy.status_code == 409 and "execution.lock" in busy.json()["detail"]  # how to clear a stale one
        assert client.app.state.run_due_schedules(later) == []
    assert datetime.fromisoformat(client.get(f"/api/runs/{run_id}/schedule").json()["schedule"]["next_at"]) > later
    run_stage(client, run_id, "results")  # free again


def test_a_rerun_that_starts_while_the_interface_connects_is_not_an_error(client, tmp_path, monkeypatch):
    """`dqvouch rerun` may take the lock between the interface's check and its execution: that is not a failed
    execution (no error line, so no alert)."""
    run_id = new_run(client)
    review_and_run(client, run_id, through_tests(client, run_id)["test_specifications"])
    path = tmp_path / "runs" / run_id
    real_connect = server_module.connect

    def rerun_takes_the_lock_meanwhile(db):
        if not (path / "execution.lock").exists():
            (path / "execution.lock").write_text(json.dumps({"pid": 1, "at": datetime.now(timezone.utc).isoformat()}),
                                                 encoding="utf-8")
        return real_connect(db)
    monkeypatch.setattr(server_module, "connect", rerun_takes_the_lock_meanwhile)
    assert client.post(f"/api/runs/{run_id}/stages/results", json={}).status_code == 202
    wait(client, run_id)
    (path / "execution.lock").unlink(missing_ok=True)
    assert not any("error" in h for h in client.get(f"/api/runs/{run_id}/schedule").json()["history"])


def test_the_operating_system_schedule_lines_name_this_run(client, tmp_path):
    run_id = new_run(client)
    lines = client.get(f"/api/runs/{run_id}/schedule/commands", params={"every_minutes": 30}).json()
    assert lines["cron"].startswith("*/30 * * * * ") and run_id in lines["cron"] and " rerun " in lines["schtasks"]
    assert str((tmp_path / ".env").resolve()) in lines["cron"]


def test_a_schedule_needs_reviewed_tests_and_a_whole_number_of_minutes(client):
    run_id = new_run(client)
    assert client.put(f"/api/runs/{run_id}/schedule", json={"every_minutes": 60}).status_code == 409
    specs = through_tests(client, run_id)["test_specifications"]
    review_and_run(client, run_id, specs)
    for bad in (0, -5, 1.5, "60", True):
        assert client.put(f"/api/runs/{run_id}/schedule", json={"every_minutes": bad}).status_code == 422, bad


def test_the_page_is_never_cached(tmp_path):
    """After an update the build's file names change: a cached page would ask for files that no longer exist."""
    c = make_client(tmp_path, None, lambda settings, key: None)
    assert c.get("/").headers["cache-control"] == "no-cache"



# --- structural changes found by a scheduled execution ----------------------------------------------------------

def _scheduled_run_on_a_sql_copy(client, tmp_path) -> tuple[str, object]:
    """A reviewed run of the demo whose SQL is an editable copy, executed once and scheduled every hour."""
    sql_dir = tmp_path / "sql"
    shutil.copytree(EXAMPLE / "sql", sql_dir)
    settings = {**client.get("/api/settings").json()["settings"], "sql_dir": str(sql_dir)}
    r = client.post("/api/runs", json=settings)
    assert r.status_code == 201, r.text
    run_id = r.json()["id"]
    review_and_run(client, run_id, through_tests(client, run_id)["test_specifications"])
    assert client.put(f"/api/runs/{run_id}/schedule", json={"every_minutes": 60}).status_code == 200
    path = sql_dir / "04_analytic_fact_orders.sql"
    path.write_text(path.read_text(encoding="utf-8").replace("WHERE o.status != 'test'",
                                                             "WHERE o.status NOT IN ('test', 'demo')"), encoding="utf-8")
    return run_id, datetime.now(timezone.utc) + timedelta(minutes=61)


def test_a_scheduled_execution_proposes_tests_after_a_structure_change(client, tmp_path):
    run_id, later = _scheduled_run_on_a_sql_copy(client, tmp_path)
    assert client.app.state.run_due_schedules(later) == [run_id]
    job = wait(client, run_id)
    assert job["status"] == "done" and job["changes"]["status"] == "proposed"
    runs = client.get("/api/runs").json()
    new = next(r for r in runs if (r.get("origin") or {}).get("kind") == "change")
    old = next(r for r in runs if r["id"] == run_id)
    assert old["changes"]["pending_run"] == new["id"]
    assert client.get(f"/api/runs/{run_id}").json()["changes"]["pending_run"] == new["id"]


def test_reviewing_a_pending_run_hands_the_schedule_over(client, tmp_path):
    run_id, later = _scheduled_run_on_a_sql_copy(client, tmp_path)
    client.app.state.run_due_schedules(later)
    wait(client, run_id)
    pending_id = client.get(f"/api/runs/{run_id}").json()["changes"]["pending_run"]
    tests = client.get(f"/api/runs/{pending_id}/stages/tests").json()
    kept = [s for s in tests["test_specifications"] if s.get("_kept_from")]
    assert kept and all(s["_previous"]["status"] == "confirmed" for s in kept)
    r = client.post(f"/api/runs/{pending_id}/stages/review", json={
        "test_specifications": [{**s, "status": "confirmed"} for s in tests["test_specifications"]],
        "metric_thresholds": [{**m, "status": "confirmed"} for m in tests["metric_thresholds"]]})
    assert r.status_code == 200, r.text
    assert r.json()["handed_over_from"] == run_id
    assert client.get(f"/api/runs/{pending_id}/schedule").json()["schedule"]["every_minutes"] == 60
    assert client.get(f"/api/runs/{run_id}/schedule").json()["schedule"] is None
    old = next(r for r in client.get("/api/runs").json() if r["id"] == run_id)
    assert old["changes"]["superseded_by"] == pending_id


def test_a_scheduled_execution_is_done_even_when_proposing_fails(tmp_path, demo_config_path, wh):
    model = {"down": False}

    def factory(settings, key):
        if not model["down"]:
            return FakeLLM()

        def down(prompt):
            raise RuntimeError("model down")
        return down
    c = make_client(tmp_path, demo_config_path, factory)
    assert c.put("/api/secrets/GEMINI_API_KEY", json={"value": "test-key"}).status_code == 200
    run_id, later = _scheduled_run_on_a_sql_copy(c, tmp_path)
    model["down"] = True
    assert c.app.state.run_due_schedules(later) == [run_id]
    job = wait(c, run_id)
    assert job["status"] == "done" and job["changes"]["status"] == "failed" and "model down" in job["changes"]["error"]
    history = c.get(f"/api/runs/{run_id}/schedule").json()["history"]
    assert history[0]["trigger"] == "schedule" and history[0]["verdicts"]


def test_a_run_made_in_the_interface_takes_propose_on_change_from_the_config(tmp_path, demo_config_path, wh):
    text = demo_config_path.read_text(encoding="utf-8").replace('sql_dir = "sql"',
                                                                 f"sql_dir = '{(EXAMPLE / 'sql').as_posix()}'")
    config = tmp_path / "dqvouch.toml"
    config.write_text(text + "propose_on_change = false\n", encoding="utf-8")  # [run] is the file's last section
    c = make_client(tmp_path, config, lambda settings, key: FakeLLM())
    settings = c.get("/api/settings").json()["settings"]
    assert settings["propose_on_change"] is False
    r = c.post("/api/runs", json=settings)
    assert r.status_code == 201, r.text
    assert Run.open(tmp_path / "runs" / r.json()["id"]).info()["settings"]["propose_on_change"] is False
