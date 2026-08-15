"""Runtime fault injection for the mock backends.

Resilience is a graded requirement, and a requirement you can only *assert* is
worth nothing. This module lets a live demo break a specific upstream mid-run
and watch the engine retry, time out, degrade, and recover — without restarting
anything.

State is a plain module-level dict because the mocks are a single uvicorn
process. The moment that stops being true this needs to move to Redis, and at
that point you should be asking why the mocks are running more than one worker.
"""

from __future__ import annotations

import asyncio
import logging
import os
from enum import Enum
from typing import Any

from fastapi import Depends, HTTPException

log = logging.getLogger("mock.faults")

# Names the engine uses for its upstreams; also the keys accepted by POST /_faults.
SERVICES: tuple[str, ...] = ("user", "kundli", "horoscope", "panchang")


class FaultMode(str, Enum):
    ERROR = "error"  # 500 — retryable server failure
    TIMEOUT = "timeout"  # hangs past any sane client deadline
    SLOW = "slow"  # succeeds, but late
    MALFORMED = "malformed"  # 200 OK with a body that is the wrong shape


# Comfortably past the longest client timeout in config/services.yaml (2.0s), so
# a `timeout` fault always trips the client's deadline rather than racing it.
TIMEOUT_SLEEP_SECONDS = 10.0

# `slow` must stay *under* the client timeouts, otherwise it is just another
# timeout. Its job is to prove the fan-out is concurrent: with three services
# slowed, total latency should land near one delay, not three.
DEFAULT_SLOW_DELAY_SECONDS = 1.0


class MalformedResponse(Exception):
    """Signals 'reply 200 with garbage'.

    Raised from a dependency and converted to a real 200 response by a handler
    in main.py, because a dependency cannot return a response body itself. This
    is the failure mode people forget: it is not a network error, the transport
    succeeded, and a client that only guards against exceptions will happily
    parse nonsense into its domain model.
    """

    def __init__(self, service: str, payload: Any) -> None:
        super().__init__(f"malformed response injected for {service}")
        self.service = service
        self.payload = payload


# Type-wrong rather than merely empty. An empty body is indistinguishable from a
# sparse-but-valid record (see user_103); these bodies break any client that
# trusts the schema — `data["houses"]["10"]` raises on a string, not a dict.
_MALFORMED_BODIES: dict[str, Any] = {
    "user": {"id": None, "birthDetails": "unavailable", "subscription": 42},
    "kundli": {"lagna": None, "currentDasha": [], "houses": "not-a-dict"},
    "horoscope": {"career": {"text": "nested wrongly"}, "finance": None},
    "panchang": ["Shukla Panchami", "Rohina"],
}


_faults: dict[str, FaultMode] = {}
_slow_delay_seconds: float = DEFAULT_SLOW_DELAY_SECONDS


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------


def current() -> dict[str, str]:
    return {service: mode.value for service, mode in _faults.items()}


def set_faults(spec: dict[str, str]) -> dict[str, str]:
    """Replace the fault table. Raises ValueError on an unknown service/mode."""
    parsed: dict[str, FaultMode] = {}
    for service, raw in spec.items():
        if service not in SERVICES:
            raise ValueError(f"unknown service {service!r}; expected one of {list(SERVICES)}")
        try:
            parsed[service] = FaultMode(raw)
        except ValueError:
            raise ValueError(
                f"unknown mode {raw!r} for {service!r}; "
                f"expected one of {[m.value for m in FaultMode]}"
            ) from None

    _faults.clear()
    _faults.update(parsed)
    log.warning("faults set: %s", current() or "(none)")
    return current()


def clear_faults() -> None:
    _faults.clear()
    log.warning("faults cleared")


def slow_delay_seconds() -> float:
    return _slow_delay_seconds


# --------------------------------------------------------------------------
# Startup configuration
# --------------------------------------------------------------------------


def load_from_env(env: dict[str, str] | None = None) -> None:
    """Seed fault state from the environment so docker-compose can boot a
    already-degraded stack for the resilience demo.

        MOCK_FAIL=kundli                 -> kundli returns 500
        MOCK_FAIL=kundli:timeout,user:slow
        MOCK_LATENCY_MS=3000             -> tunes how slow `slow` is; on its own
                                            (no MOCK_FAIL) it slows everything,
                                            so the var is meaningful by itself.
    """
    global _slow_delay_seconds
    env = os.environ if env is None else env

    latency_ms = env.get("MOCK_LATENCY_MS")
    if latency_ms:
        try:
            _slow_delay_seconds = max(0.0, int(latency_ms) / 1000)
        except ValueError:
            log.warning("ignoring non-numeric MOCK_LATENCY_MS=%r", latency_ms)
            latency_ms = None

    spec: dict[str, str] = {}
    raw_fail = (env.get("MOCK_FAIL") or "").strip()
    if raw_fail:
        for entry in raw_fail.split(","):
            entry = entry.strip()
            if not entry:
                continue
            # `error` is the default mode: MOCK_FAIL=kundli should mean the
            # obvious thing without spelling out a mode.
            service, _, mode = entry.partition(":")
            spec[service.strip()] = (mode or FaultMode.ERROR.value).strip()
    elif latency_ms:
        spec = {service: FaultMode.SLOW.value for service in SERVICES}

    if not spec:
        return
    try:
        set_faults(spec)
    except ValueError as exc:
        log.warning("ignoring invalid MOCK_FAIL=%r: %s", raw_fail, exc)


# --------------------------------------------------------------------------
# Enforcement
# --------------------------------------------------------------------------


async def _apply(service: str) -> None:
    mode = _faults.get(service)
    if mode is None:
        return

    log.warning("injecting %s fault on %s", mode.value, service)

    if mode is FaultMode.ERROR:
        raise HTTPException(status_code=500, detail=f"injected failure in {service} service")
    if mode is FaultMode.TIMEOUT:
        await asyncio.sleep(TIMEOUT_SLEEP_SECONDS)
        return  # If the client is still waiting after 10s, its timeout is broken.
    if mode is FaultMode.SLOW:
        await asyncio.sleep(_slow_delay_seconds)
        return
    if mode is FaultMode.MALFORMED:
        raise MalformedResponse(service, _MALFORMED_BODIES[service])


def gate(service: str) -> Any:
    """Dependency that enforces the current fault for `service`.

    Attached per-route so the data handlers stay pure fixture lookups with no
    idea faults exist.
    """

    async def _gate() -> None:
        await _apply(service)

    return Depends(_gate)


__all__ = [
    "FaultMode",
    "MalformedResponse",
    "SERVICES",
    "clear_faults",
    "current",
    "gate",
    "load_from_env",
    "set_faults",
    "slow_delay_seconds",
]
