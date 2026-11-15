"""Notifications.

``notify()`` writes a Notification row (inside the caller's transaction when a session is
passed, so a trade and its notification commit atomically). A separate delivery loop
(``deliver_pending``) pushes pending rows to the configured channels and retries failures,
so a webhook/SMTP outage never loses a notification and never blocks trading.
"""
from __future__ import annotations

import json
import logging
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import session_scope
from app.models import Notification

log = logging.getLogger(__name__)

# Notification kinds
NEW_TRADE = "trade_opened"
TRADE_CLOSED = "trade_closed"
STOP_LOSS = "stop_loss_hit"
TAKE_PROFIT = "take_profit_hit"
DETERIORATION = "strategy_deterioration"
SERVICE_OUTAGE = "service_outage"
DATA_INTERRUPTION = "market_data_interruption"
DATA_RESTORED = "market_data_restored"
STRATEGY_STOPPED = "strategy_stopped"
JOB_FAILED = "job_failed"
SIGNAL_REJECTED = "signal_rejected"


def notify(
    kind: str,
    title: str,
    body: str = "",
    *,
    severity: str = "info",
    data: dict | None = None,
    dedup_key: str | None = None,
    session: Session | None = None,
) -> Notification | None:
    """Record a notification. If ``dedup_key`` is given, identical alerts within the
    cooldown window are suppressed (prevents alert storms during an outage)."""

    def _do(s: Session) -> Notification | None:
        if dedup_key:
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=get_settings().alert_cooldown_seconds)
            exists = s.scalar(
                select(Notification.id).where(
                    Notification.dedup_key == dedup_key, Notification.created_at >= cutoff
                )
            )
            if exists:
                return None
        n = Notification(
            kind=kind, title=title, body=body, severity=severity, data=data or {}, dedup_key=dedup_key
        )
        s.add(n)
        s.flush()
        log.log(
            logging.WARNING if severity != "info" else logging.INFO,
            "notification: %s - %s",
            title,
            body,
        )
        return n

    if session is not None:
        return _do(session)
    with session_scope() as s:
        return _do(s)


# --------------------------------------------------------------------------- delivery
def _send_webhook(url: str, n: Notification) -> None:
    emoji = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}.get(n.severity, "")
    text = f"{emoji} *{n.title}*\n{n.body}".strip()
    # "text" works for Slack, "content" for Discord; generic receivers get the full object
    payload = {
        "text": text,
        "content": text[:1900],
        "kind": n.kind,
        "severity": n.severity,
        "data": n.data,
        "created_at": n.created_at.isoformat() if n.created_at else None,
    }
    r = httpx.post(url, json=payload, timeout=10)
    r.raise_for_status()


def _send_email(n: Notification) -> None:
    s = get_settings()
    msg = EmailMessage()
    msg["Subject"] = f"[StrategyLab][{n.severity.upper()}] {n.title}"
    msg["From"] = s.smtp_from or s.smtp_user
    msg["To"] = s.notify_email_to
    msg.set_content(f"{n.body}\n\n{json.dumps(n.data, indent=2, default=str)}")
    with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=15) as smtp:
        smtp.starttls()
        if s.smtp_user:
            smtp.login(s.smtp_user, s.smtp_password)
        smtp.send_message(msg)


def deliver_pending(limit: int = 50) -> int:
    s_ = get_settings()
    channels = []
    if s_.webhook_url:
        channels.append(lambda n: _send_webhook(s_.webhook_url, n))
    if s_.smtp_host and s_.notify_email_to:
        # email only for warnings/criticals + closed trades, to keep inbox sane
        channels.append(lambda n: _send_email(n) if n.severity != "info" or n.kind == TRADE_CLOSED else None)

    sent = 0
    with session_scope() as s:
        rows = s.scalars(
            select(Notification)
            .where(Notification.delivery_status.in_(("pending", "failed")), Notification.delivery_attempts < 10)
            .order_by(Notification.id)
            .limit(limit)
        ).all()
        for n in rows:
            if not channels:
                n.delivery_status = "sent"  # stored + visible in dashboard only
                continue
            try:
                for ch in channels:
                    ch(n)
                n.delivery_status = "sent"
                n.delivery_error = None
                sent += 1
            except Exception as e:  # keep for retry; never drop
                n.delivery_status = "failed"
                n.delivery_error = str(e)[:500]
                log.warning("notification delivery failed id=%s: %s", n.id, e)
            finally:
                n.delivery_attempts += 1
    return sent
