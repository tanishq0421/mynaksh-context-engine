"""Stand-in MyNaksh backends: user, kundli, horoscope, panchang.

Deliberately a SEPARATE process on port 8001 rather than in-process stubs.
"Handles retries, timeouts, and partial failures" is only *demonstrated* if
there is a real network boundary — with monkeypatched stubs the HTTP client,
the retry loop and the timeout never actually execute, and the resilience layer
is untested code that happens to compile.

This is scaffolding, not the deliverable. The engine lives in app/.

    python -m mock_services.main        # or: uvicorn mock_services.main:app --port 8001
"""

from __future__ import annotations

import itertools
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable, Awaitable

from fastapi import Body, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from mock_services import data, faults

logging.basicConfig(
    level=os.getenv("MOCK_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("mock")

PORT = int(os.getenv("MOCK_PORT", "8001"))


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    faults.load_from_env()
    log.info("mock services up on :%d | faults=%s", PORT, faults.current() or "none")
    yield


app = FastAPI(title="MyNaksh Mock Services", version="0.1.0", lifespan=lifespan)


# --------------------------------------------------------------------------
# Request log
# --------------------------------------------------------------------------

_seq = itertools.count(1)


@app.middleware("http")
async def log_requests(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """One line per *inbound* request, numbered.

    The sequence number is the point: a client retry has to show up here as a
    second, separately numbered request. Without it a reviewer has to take the
    engine's own "retried: 2" metadata on faith — self-reported evidence from
    the component under test.
    """
    n = next(_seq)
    started = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - started) * 1000
    log.info(
        "#%-4d %s %s -> %d (%.0fms)",
        n,
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
    )
    return response


@app.exception_handler(faults.MalformedResponse)
async def _malformed(_: Request, exc: faults.MalformedResponse) -> JSONResponse:
    # 200, on purpose. The transport succeeded; only the body is wrong.
    return JSONResponse(status_code=200, content=exc.payload)


# --------------------------------------------------------------------------
# Data endpoints. Shapes are fixed by the assignment spec — do not "improve"
# them; the engine's extractors are written against exactly these keys.
# --------------------------------------------------------------------------


def _found(record: dict[str, Any] | None, user_id: str) -> dict[str, Any]:
    """404 for an unknown user.

    A 404 is an answer, not a fault: the user genuinely does not exist, and
    retrying cannot change that. The engine must treat it as terminal, so the
    mocks have to distinguish it from the 500 that `error` injects.
    """
    if record is None:
        raise HTTPException(status_code=404, detail=f"no such user: {user_id}")
    return record


@app.get("/users/{user_id}", dependencies=[faults.gate("user")])
async def get_user(user_id: str) -> dict[str, Any]:
    return _found(data.get_user(user_id), user_id)


@app.get("/kundli/{user_id}", dependencies=[faults.gate("kundli")])
async def get_kundli(user_id: str) -> dict[str, Any]:
    return _found(data.get_kundli(user_id), user_id)


@app.get("/horoscope/{user_id}", dependencies=[faults.gate("horoscope")])
async def get_horoscope(user_id: str) -> dict[str, Any]:
    return _found(data.get_horoscope(user_id), user_id)


@app.get("/panchang", dependencies=[faults.gate("panchang")])
async def get_panchang() -> dict[str, Any]:
    # Global and date-keyed — no userId. One cache entry serves every user.
    return data.panchang_today()


@app.get("/health")
async def health() -> dict[str, Any]:
    # Ungated by design: a liveness probe that fails when a fault is injected
    # would take the container down mid-demo, which is exactly backwards.
    return {"status": "ok", "faults": faults.current()}


# --------------------------------------------------------------------------
# Fault control
# --------------------------------------------------------------------------


@app.get("/_faults")
async def read_faults() -> dict[str, Any]:
    return {"faults": faults.current(), "slow_delay_seconds": faults.slow_delay_seconds()}


@app.post("/_faults")
async def write_faults(spec: dict[str, str] = Body(...)) -> dict[str, Any]:
    """e.g. {"kundli": "error", "panchang": "slow"} — replaces the whole table."""
    try:
        return {"faults": faults.set_faults(spec)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/_faults")
async def reset_faults() -> dict[str, Any]:
    faults.clear_faults()
    return {"faults": faults.current()}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
