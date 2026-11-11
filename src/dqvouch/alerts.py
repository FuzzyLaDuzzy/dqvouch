"""Alerts when an execution of a run's tests gets worse: a test starts failing or erroring, or the execution itself
cannot run after one that did. Sent by webhook ({"text": ...}: Slack, Mattermost, Google Chat, Rocket.Chat, Teams
workflows; Discord with /slack appended) and/or by email (SMTP), as configured once in the .env file. A known problem
is said once, not at every execution, and a failed send never fails the execution: it is recorded instead."""
import json
import smtplib
import ssl
import urllib.request
from email.message import EmailMessage

from dqvouch.config import ENGINE_LABELS

WEBHOOK = "DQVOUCH_ALERT_WEBHOOK_URL"
BAD = ("fail", "error")
TRIGGERS = {"schedule": "a schedule", "command": "dqvouch rerun", "manual": "a person"}
MAX_LISTED = 20


def worse(entry: dict, history: list) -> dict | None:
    """What got worse in `entry` against `history` (the executions before it, newest first): {"error": ...} when it
    could not run and the one before could, {"new": [check ids]} for tests failing or erroring now that did not in
    the last execution with verdicts; None otherwise (the first execution has nothing to be compared with)."""
    if "error" in entry:
        return {"error": entry["error"]} if not history or "error" not in history[0] else None
    before = next((h for h in history if h.get("verdicts")), None)
    if before is None or not entry.get("verdicts"):
        return None
    new = [i for i, v in entry["verdicts"].items() if v in BAD and before["verdicts"].get(i) not in BAD]
    return {"new": new} if new else None


def message(run_info: dict, entry: dict, problem: dict, rows: list) -> tuple[str, str]:
    """(subject, text) of the alert."""
    db = run_info["settings"]["database"]
    where = (f"{ENGINE_LABELS.get(db['engine'], db['engine'])} at {db['host']}:{db['port']} "
             f"({', '.join(db['schemas'])})")
    when = f"started by {TRIGGERS.get(entry.get('trigger'), entry.get('trigger'))} at {entry.get('at')}"
    if "error" in problem:
        return (f"dqvouch: run {run_info['id']} could not execute its tests",
                f"The tests of dqvouch run {run_info['id']} on {where} could not run ({when}):\n{problem['error']}")
    new, by_id = problem["new"], {r["check_id"]: r for r in rows}
    counts = ", ".join(f"{n} {v.replace('_', ' ')}" for v, n in sorted((entry.get("counts") or {}).items()))
    lines = [f"{len(new)} test(s) of dqvouch run {run_info['id']} on {where} started failing ({when}).",
             f"This execution: {counts}.", ""]
    for check_id in new[:MAX_LISTED]:
        r = by_id.get(check_id, {})
        title = r.get("title") or str(check_id).split("__")[-1]
        detail = (f"error: {r['error']}" if r.get("error")
                  else f"observed {r.get('observed')}, expected {r.get('expected')}")
        lines.append(f"- {title} [{check_id}] on {r.get('asset', '?')}: {r.get('verdict', entry['verdicts'][check_id])}"
                     f", {detail}")
    if len(new) > MAX_LISTED:
        lines.append(f"... and {len(new) - MAX_LISTED} more")
    return f"dqvouch: {len(new)} test(s) started failing in run {run_info['id']}", "\n".join(lines)


def send_webhook(url: str, text: str, timeout: float = 10) -> None:
    request = urllib.request.Request(url, data=json.dumps({"text": text}).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout):
        pass


def send_email(values: dict, subject: str, text: str) -> None:
    host, port = values["DQVOUCH_SMTP_HOST"], int(values.get("DQVOUCH_SMTP_PORT") or 587)
    user = values.get("DQVOUCH_SMTP_USER") or ""
    msg = EmailMessage()
    msg["Subject"], msg["From"] = subject, values.get("DQVOUCH_SMTP_FROM") or user
    msg["To"] = ", ".join(a.strip() for a in values["DQVOUCH_SMTP_TO"].split(",") if a.strip())
    msg.set_content(text)
    context = ssl.create_default_context()
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=10, context=context)
    else:
        server = smtplib.SMTP(host, port, timeout=10)
    with server:
        encrypted = port == 465
        if not encrypted:
            server.ehlo()
            if server.has_extn("starttls"):
                server.starttls(context=context)
                server.ehlo()
                encrypted = True
        if user:
            if not encrypted:
                raise RuntimeError(f"{host}:{port} does not offer STARTTLS, and dqvouch does not send the SMTP password "
                                   f"unencrypted: use port 465 or a server with STARTTLS")
            server.login(user, values.get("DQVOUCH_SMTP_PASSWORD") or "")
        server.send_message(msg)


def _email_configured(values: dict) -> bool:
    return bool(values.get("DQVOUCH_SMTP_HOST") and values.get("DQVOUCH_SMTP_TO"))


def configured(values: dict) -> bool:
    return bool(values.get(WEBHOOK)) or _email_configured(values)


def deliver(values: dict, subject: str, text: str, redact) -> dict:
    """Sends to every configured channel: {"alerted": [channels]} and/or {"alert_error": text}, never raises."""
    sent, errors = [], []
    if values.get(WEBHOOK):
        try:
            send_webhook(values[WEBHOOK], text)
            sent.append("webhook")
        except Exception as e:
            errors.append(f"webhook: {e or type(e).__name__}")
    if _email_configured(values):
        try:
            send_email(values, subject, text)
            sent.append("email")
        except Exception as e:
            errors.append(f"email: {e or type(e).__name__}")
    result = {"alerted": sent} if sent else {}
    if errors:
        result["alert_error"] = redact("; ".join(errors))
    return result


def notifier(values: dict, redact, run_info: dict):
    """The `notify(entry, history, rows)` that record_execution calls: alerts when the execution got worse."""
    def notify(entry: dict, history: list, rows: list) -> dict:
        problem = worse(entry, history) if configured(values) else None
        if not problem:
            return {}
        subject, text = message(run_info, entry, problem, rows)
        return deliver(values, subject, redact(text), redact)
    return notify
