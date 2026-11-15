"""Structured logging to stdout.

Containers write JSON lines to stdout; Docker (or systemd-journald) collects them, rotates
them and can ship them to a central store (Loki, CloudWatch, Datadog, Better Stack...).
Every line carries the ``component`` name so logs from all services can be merged.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

from app.config import get_settings

_COMPONENT = "unknown"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "component": _COMPONENT,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(component: str) -> None:
    global _COMPONENT
    _COMPONENT = component
    s = get_settings()
    handler = logging.StreamHandler(sys.stdout)
    if s.log_json:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(f"%(asctime)s %(levelname)s [{component}] %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(s.log_level.upper())
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def log_extra(**fields) -> dict:
    return {"extra": {"extra_fields": fields}}
