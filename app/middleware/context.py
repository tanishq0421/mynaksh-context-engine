"""Request-scoped correlation id.

Every log line in a request carries the same id, so a slow or degraded response
can be traced across the fan-out to four upstream services without guessing
which log lines belonged together.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

REQUEST_ID_HEADER = "X-Request-ID"

# A ContextVar rather than a parameter threaded through every call: the
# alternative is passing a request id into the classifier, the engine, and each
# client purely so they can log, which pollutes signatures that have nothing to
# do with HTTP.
_request_id: ContextVar[str] = ContextVar("request_id", default="-")


def current_request_id() -> str:
    return _request_id.get()


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Honour an inbound id so the correlation survives across services.
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex[:12]
        token = _request_id.set(request_id)
        try:
            response = await call_next(request)
            response.headers[REQUEST_ID_HEADER] = request_id
            return response
        finally:
            _request_id.reset(token)
