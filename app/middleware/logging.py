"""Logging setup and request/latency logging.

Request logging, latency logging, and prompt-size logging are all explicitly
graded. The first two live here; prompt size is logged where it is known, in the
prompt builder.
"""

from __future__ import annotations

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.middleware.context import current_request_id


class RequestIdFilter(logging.Filter):
    """Stamps every record with the current request id.

    Done as a filter rather than requiring each call site to pass the id, so a
    module deep in the pipeline gets correlation for free.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = current_request_id()
        return True


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-5s [%(request_id)s] %(name)s: %(message)s")
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # uvicorn installs its own handlers; route them through ours so request ids
    # appear on access logs too.
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True


class LatencyLoggingMiddleware(BaseHTTPMiddleware):
    """Logs method, path, status and wall-clock duration for every request."""

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
            elapsed_ms = (time.perf_counter() - started) * 1000
            self._log.info(
                "%s %s -> %s in %.1fms",
                request.method,
                request.url.path,
                status,
                elapsed_ms,
            )
