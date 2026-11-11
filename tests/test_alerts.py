"""Alerts: only when an execution gets worse, by webhook and by email, and never a failure of their own."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from dqvouch import alerts

RUN = {"id": "20261111-120000", "settings": {"database": {"engine": "postgres", "host": "db.example", "port": 5432,
                                                          "schemas": ["landing", "staging"]}}}
PASS = {"at": "1", "verdicts": {"a": "pass", "b": "pass"}}
A_FAILS = {"at": "2", "verdicts": {"a": "fail", "b": "pass"}}
B_ERRORS = {"at": "3", "verdicts": {"a": "fail", "b": "error"}}
DOWN = {"at": "4", "error": "cannot connect"}


@pytest.mark.parametrize("entry,history,expected", [
    (A_FAILS, [PASS], {"new": ["a"]}),                # a test starts failing
    (A_FAILS, [A_FAILS, PASS], None),                 # still failing: said already
    (B_ERRORS, [A_FAILS], {"new": ["b"]}),            # a test starts erroring
    (A_FAILS, [], None),                              # the first execution: nothing to compare with
    (DOWN, [PASS], {"error": "cannot connect"}),      # the execution fails after one that worked
    (DOWN, [DOWN, PASS], None),                       # still down: said already
    (DOWN, [], {"error": "cannot connect"}),
    (A_FAILS, [DOWN, PASS], {"new": ["a"]}),          # back up, and compared with the last execution that ran
    (B_ERRORS, [PASS, A_FAILS], {"new": ["a", "b"]}),  # fixed, then broken again: a new alert
])
def test_only_a_change_for_the_worse_alerts(entry, history, expected):
    assert alerts.worse(entry, history) == expected


def test_the_message_lists_at_most_20_new_failures():
    ids = [f"t{i}" for i in range(25)]
    entry = {"at": "2026-11-11T12:00:00+00:00", "trigger": "schedule", "counts": {"fail": 25},
             "verdicts": {i: "fail" for i in ids}}
    rows = [{"check_id": i, "title": f"Test {i}", "asset": "staging.orders", "verdict": "fail", "observed": 3,
             "expected": "failed_rows equals 0"} for i in ids]
    subject, text = alerts.message(RUN, entry, {"new": ids}, rows)
    assert "25" in subject and RUN["id"] in subject
    assert "Test t0" in text and "Test t19" in text and "Test t20" not in text and "and 5 more" in text
    assert "observed 3" in text and "failed_rows equals 0" in text and "db.example" in text
    down_subject, down_text = alerts.message(RUN, DOWN, {"error": "cannot connect"}, [])
    assert "could not" in down_subject and "cannot connect" in down_text


@pytest.fixture
def hook():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/hook", received
    server.shutdown()


def test_the_webhook_gets_the_text(hook):
    url, received = hook
    alerts.send_webhook(url, "2 tests started failing")
    assert received == [{"text": "2 tests started failing"}]


class FakeSMTP:
    made = []

    def __init__(self, host, port, timeout=None, context=None):
        self.calls = [("connect", host, port)]
        FakeSMTP.made.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.calls.append(("quit",))

    def ehlo(self):
        pass

    def has_extn(self, name):
        return name.lower() == "starttls"

    def starttls(self, context=None):
        self.calls.append(("starttls",))

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, msg):
        self.calls.append(("send", msg["From"], msg["To"], msg["Subject"], msg.get_content().strip()))


SMTP = {"DQVOUCH_SMTP_HOST": "smtp.example", "DQVOUCH_SMTP_PORT": "587", "DQVOUCH_SMTP_USER": "dq@example.com",
        "DQVOUCH_SMTP_PASSWORD": "pw-123", "DQVOUCH_SMTP_FROM": "", "DQVOUCH_SMTP_TO": "a@example.com, b@example.com"}


def test_email_uses_starttls_and_logs_in(monkeypatch):
    FakeSMTP.made.clear()
    monkeypatch.setattr(alerts.smtplib, "SMTP", FakeSMTP)
    alerts.send_email(SMTP, "subject", "body")
    assert FakeSMTP.made[0].calls == [
        ("connect", "smtp.example", 587), ("starttls",), ("login", "dq@example.com", "pw-123"),
        ("send", "dq@example.com", "a@example.com, b@example.com", "subject", "body"), ("quit",)]


class PlainSMTP(FakeSMTP):
    def has_extn(self, name):
        return False


def test_the_password_is_never_sent_without_tls(monkeypatch):
    FakeSMTP.made.clear()
    monkeypatch.setattr(alerts.smtplib, "SMTP", PlainSMTP)
    with pytest.raises(RuntimeError, match="STARTTLS"):
        alerts.send_email(SMTP, "s", "b")
    assert not any(call[0] == "login" for call in FakeSMTP.made[0].calls)
    alerts.send_email({**SMTP, "DQVOUCH_SMTP_USER": "", "DQVOUCH_SMTP_FROM": "dq@example.com"}, "s", "b")  # a relay
    assert FakeSMTP.made[1].calls[-2][0] == "send"


def test_port_465_uses_ssl(monkeypatch):
    FakeSMTP.made.clear()
    monkeypatch.setattr(alerts.smtplib, "SMTP_SSL", FakeSMTP)
    alerts.send_email({**SMTP, "DQVOUCH_SMTP_PORT": "465"}, "s", "b")
    assert ("starttls",) not in FakeSMTP.made[0].calls and FakeSMTP.made[0].calls[0] == ("connect", "smtp.example", 465)


def test_a_failed_send_is_recorded_and_the_url_redacted(monkeypatch):
    url = "http://127.0.0.1:9/hook?token=s3cr3t"
    values = {"DQVOUCH_ALERT_WEBHOOK_URL": url}
    notify = alerts.notifier(values, lambda text: text.replace(url, "***"), RUN)
    result = notify(A_FAILS, [PASS], [])  # nothing listens on port 9
    assert set(result) == {"alert_error"} and "s3cr3t" not in result["alert_error"]

    def leaky(url, text, timeout=10):
        raise OSError(f"could not post to {url}")
    monkeypatch.setattr(alerts, "send_webhook", leaky)
    result = notify(A_FAILS, [PASS], [])
    assert "s3cr3t" not in result["alert_error"] and "***" in result["alert_error"]


def test_nothing_is_sent_when_nothing_is_configured_or_nothing_got_worse(monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "send_webhook", lambda url, text, timeout=10: sent.append(text))
    assert alerts.notifier({}, str, RUN)(A_FAILS, [PASS], []) == {}
    assert alerts.notifier({"DQVOUCH_ALERT_WEBHOOK_URL": "https://h"}, str, RUN)(PASS, [PASS], []) == {}
    assert alerts.notifier({"DQVOUCH_ALERT_WEBHOOK_URL": "https://h"}, str, RUN)(A_FAILS, [PASS], []) == {
        "alerted": ["webhook"]}
    assert len(sent) == 1


def test_a_failure_after_a_long_outage_still_alerts(tmp_path):
    from dqvouch.config import DatabaseSettings, Settings
    from dqvouch.pipeline import record_execution
    from dqvouch.store import Run
    run = Run.create(tmp_path, Settings(database=DatabaseSettings(schemas=["landing"])))
    run.add_history(PASS)
    for _ in range(60):  # five hours of a database that was down, every 5 minutes
        run.add_history(DOWN)
    entry = record_execution(run, dict(A_FAILS), lambda e, history, rows: {"seen": alerts.worse(e, history)})
    assert entry["seen"] == {"new": ["a"]}
