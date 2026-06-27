"""Best-effort completion notifications (SMTP email + Slack webhook).

Config (non-secret) lives in the ``settings`` table as JSON; the SMTP password
and Slack webhook URL are secrets in the encrypted store (refs ``smtp:password``,
``slack:webhook``) — never in the DB, logs, or any artifact. Delivery is strictly
best-effort: any failure is swallowed so a run is never blocked or failed by it.
"""

from __future__ import annotations

import json
import smtplib
import sqlite3
import urllib.request
from email.message import EmailMessage
from typing import Any, Optional

from pgbench_webapp import queries
from pgbench_webapp.secrets_store import SecretStore

SMTP_PASSWORD_REF = "smtp:password"
SLACK_WEBHOOK_REF = "slack:webhook"
SETTINGS_KEY = "notify_config"


def get_config(conn: sqlite3.Connection) -> dict[str, Any]:
    raw = queries.get_setting(conn, SETTINGS_KEY, "")
    try:
        return dict(json.loads(raw)) if raw else {}
    except ValueError:
        return {}


def set_config(conn: sqlite3.Connection, cfg: dict[str, Any]) -> None:
    queries.set_setting(conn, SETTINGS_KEY, json.dumps(cfg))


def _message(state: str, run_id: Optional[str], label: str, base_url: str,
             peak_qps: Optional[float]) -> tuple[str, str]:
    subject = f"[pgbench-harness] {label or run_id or 'run'} — {state}"
    lines = [f"Run: {label or run_id}", f"Status: {state}"]
    if peak_qps:
        lines.append(f"Peak QPS: {peak_qps:,.0f}")
    if run_id and base_url:
        lines.append(f"Report: {base_url.rstrip('/')}/runs/{run_id}/report")
    return subject, "\n".join(lines)


def _send_email(c: dict[str, Any], password: Optional[str], subject: str, body: str) -> None:
    smtp = c.get("smtp") or {}
    host, to = smtp.get("host"), smtp.get("to")
    if not host or not to:
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp.get("from") or smtp.get("user") or "pgbench-harness@localhost"
    msg["To"] = to
    msg.set_content(body)
    port = int(smtp.get("port", 587))
    with smtplib.SMTP(host, port, timeout=10) as s:
        if smtp.get("tls", True):
            s.starttls()
        if smtp.get("user") and password:
            s.login(smtp["user"], password)
        s.send_message(msg)


def _send_slack(webhook: Optional[str], text: str) -> None:
    if not webhook:
        return
    data = json.dumps({"text": text}).encode()
    req = urllib.request.Request(webhook, data=data,
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10).close()  # nosec - operator-supplied webhook


def notify(conn: sqlite3.Connection, store: SecretStore, *, state: str,
           run_id: Optional[str], label: str, peak_qps: Optional[float] = None) -> list[str]:
    """Fire configured notifications. Returns the channels attempted; never raises."""
    c = get_config(conn)
    base_url = queries.get_setting(conn, "base_url", "")
    subject, body = _message(state, run_id, label, base_url, peak_qps)
    sent: list[str] = []
    if (c.get("smtp") or {}).get("host"):
        try:
            _send_email(c, store.get(SMTP_PASSWORD_REF), subject, body)
            sent.append("email")
        except Exception:  # noqa: BLE001  (best-effort; must not fail the run)
            pass
    if (c.get("slack") or {}).get("enabled"):
        try:
            _send_slack(store.get(SLACK_WEBHOOK_REF), f"*{subject}*\n{body}")
            sent.append("slack")
        except Exception:  # noqa: BLE001
            pass
    return sent
