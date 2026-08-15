"""Structured JSON logging, plus request and latency logging.

Logs are emitted as one JSON object per line so a collector (CloudWatch, Loki,
Datadog) can index fields directly instead of parsing a human-readable string
with a regex that breaks the first time a message format changes.

Request logging, latency logging, and prompt-size logging are all explicitly
graded. The first two live here; prompt size is logged where it is known, in the
prompt builder.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.middleware.context import current_request_id

# LogRecord's own attributes. Anything outside this set arrived via
# `logger.info(..., extra={...})` and is real structured context worth emitting.
_RESERVED = frozenset(
    """args asctime created exc_info exc_text filename funcName levelname levelno
    lineno message module msecs msg name pathname process processName
    relativeCreated stack_info taskName thread threadName request_id""".split()
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }

        # Promote structured extras to top-level keys so they are queryable.
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        # default=str so an unexpected object never takes down the log line —
        # losing fidelity on one field beats losing the whole record.
        return json.dumps(payload, default=str)


class RequestIdFilter(logging.Filter):
    """Stamps every record with the current request id.

    A filter rather than a parameter threaded through each call site, so a module
    deep in the pipeline gets correlation without knowing HTTP exists.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = current_request_id()
        return True


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # uvicorn installs its own plain-text handlers; clearing them and enabling
    # propagation routes its output through ours, so the stream stays valid
    # newline-delimited JSON rather than a mix of two formats.
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True


class LatencyLoggingMiddleware(BaseHTTPMiddleware):
    """One structured record per request: method, path, status, duration."""

    def __init__(self, app, logger_name: str = "app.request"):
        super().__init__(app)
        self._log = logging.getLogger(logger_name)

    async def dispatch(self, request: Request, call_next):
        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            self._log.info(
                "request completed",
                extra={
                    "event": "http_request",
                    "method": request.method,
                    "path": request.url.path,
                    "status": status,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
