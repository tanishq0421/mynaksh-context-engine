"""Upstream fan-out and failure handling.

The other place bugs hide. A resilience bug is invisible in the happy path and
only appears when something is already going wrong, which is the worst time to
discover it.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from app.config_schema import ServicePolicy, ServicesConfig
from app.domain import Criticality
from app.services.aggregator import CriticalServiceError, gather_context
from app.services.cache import TTLCache
from app.services.horoscope import HoroscopeClient
from app.services.kundli import KundliClient
from app.services.panchang import PanchangClient
from app.services.user import UserClient

FAST = ServicePolicy(timeout_seconds=1.0, retries=1, backoff_seconds=0.01, ttl_seconds=60)


def make_clients(handler, cache: TTLCache | None = None, **policies):
    """Wire the real clients against an in-process transport.

    Deliberately not mocking `fetch` — that would skip the retry, timeout, cache
    and parse logic, which is the whole thing under test.
    """
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = cache or TTLCache()
    kinds = {
        "user": UserClient,
        "kundli": KundliClient,
        "horoscope": HoroscopeClient,
        "panchang": PanchangClient,
    }
    return {
        name: kind("http://svc", policies.get(name, FAST), cache, client)
        for name, kind in kinds.items()
    }, cache


PAYLOADS = {
    "/users/": {
        "id": "user_101",
        "name": "Aarav Sharma",
        "language": "en",
        "subscription": "premium",
        "tonePreference": "motivational",
        "birthDetails": {"date": "1997-08-15", "time": "09:35", "place": "Delhi"},
    },
    "/kundli/": {
        "lagna": "Libra",
        "moonSign": "Scorpio",
        "currentDasha": {"mahadasha": "Rahu", "antardasha": "Mars"},
        "houses": {"10": {"lord": "Moon", "strength": "Strong"}},
    },
    "/horoscope/": {"career": "Networking may bring new opportunities."},
    "/panchang": {
        "date": "2026-08-01",
        "tithi": "Shukla Panchami",
        "nakshatra": "Rohini",
        "yoga": "Siddhi",
        "karana": "Bava",
    },
}


def payload_for(path: str):
    for prefix, body in PAYLOADS.items():
        if path.startswith(prefix):
            return body
    raise AssertionError(f"unexpected path {path}")


def healthy(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=payload_for(request.url.path))


async def test_fan_out_is_concurrent_not_serial():
    """Four services that each take 150ms must total ~150ms, not ~600ms. This is
    the difference between a 200ms endpoint and an 800ms one."""

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.15)
        return httpx.Response(200, json=payload_for(request.url.path))

    clients, _ = make_clients(slow)
    started = time.perf_counter()
    await gather_context("user_101", clients)
    elapsed = time.perf_counter() - started

    assert elapsed < 0.4, f"serialized: {elapsed:.2f}s"


async def test_degradable_failure_still_returns_a_bundle():
    def flaky(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/kundli/"):
            return httpx.Response(500)
        return healthy(request)

    clients, _ = make_clients(flaky)
    bundle = await gather_context("user_101", clients)

    assert "kundli" in bundle.failures
    assert "horoscope" in bundle.data
    assert not bundle.healthy


async def test_required_service_failure_is_typed():
    """The API maps exactly this to 503. A bare RuntimeError would force the
    caller to pattern-match on message text."""

    def user_down(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/users/"):
            return httpx.Response(500)
        return healthy(request)

    clients, _ = make_clients(user_down, user=ServicePolicy(criticality=Criticality.REQUIRED, retries=0))

    with pytest.raises(CriticalServiceError) as err:
        await gather_context("user_101", clients)
    assert err.value.service == "user"


async def test_missing_user_carries_404_so_api_can_answer_correctly():
    """A missing user is a fact about the request, not an outage. Without the
    status travelling with the failure this would surface as a 503."""

    def not_found(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/users/"):
            return httpx.Response(404)
        return healthy(request)

    clients, _ = make_clients(not_found, user=ServicePolicy(criticality=Criticality.REQUIRED, retries=2))

    with pytest.raises(CriticalServiceError) as err:
        await gather_context("nope", clients)
    assert err.value.status_code == 404


async def test_client_errors_are_not_retried():
    """A 404 will still be a 404 in 100ms. Retrying it burns the request's
    latency budget to re-learn something already known."""
    attempts = {"n": 0}

    def not_found(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/kundli/"):
            attempts["n"] += 1
            return httpx.Response(404)
        return healthy(request)

    clients, _ = make_clients(not_found, kundli=ServicePolicy(retries=3, backoff_seconds=0.01))
    await gather_context("user_101", clients)

    assert attempts["n"] == 1


async def test_server_errors_are_retried():
    attempts = {"n": 0}

    def unstable(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/kundli/"):
            attempts["n"] += 1
            return httpx.Response(503)
        return healthy(request)

    clients, _ = make_clients(unstable, kundli=ServicePolicy(retries=2, backoff_seconds=0.01))
    await gather_context("user_101", clients)

    assert attempts["n"] == 3  # initial + 2 retries


async def test_malformed_body_is_a_failure_not_an_exception():
    """A 200 carrying a wrong-shaped body is a different code path from a
    network error, and it is the one people forget."""

    def garbage(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/kundli/"):
            return httpx.Response(200, json={"unexpected": "shape"})
        return healthy(request)

    clients, _ = make_clients(garbage)
    bundle = await gather_context("user_101", clients)

    assert "kundli" in bundle.failures
    assert "user" in bundle.data


async def test_stale_cache_is_served_when_upstream_dies():
    """The difference between degraded and broken."""
    state = {"up": True}

    def toggling(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/kundli/") and not state["up"]:
            return httpx.Response(500)
        return healthy(request)

    policy = ServicePolicy(ttl_seconds=0, retries=0, serve_stale_on_error=True)
    clients, _ = make_clients(toggling, kundli=policy)

    await gather_context("user_101", clients)  # warm
    state["up"] = False
    bundle = await gather_context("user_101", clients)

    assert "kundli" in bundle.data, "should have served last-known-good"
    assert "kundli" in bundle.stale


async def test_single_flight_collapses_concurrent_misses():
    """Without this, an expiring TTL under load stampedes the upstream."""
    calls = {"n": 0}

    async def counting(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/kundli/"):
            calls["n"] += 1
            await asyncio.sleep(0.05)
        return healthy(request)

    clients, _ = make_clients(counting)
    await asyncio.gather(*(gather_context("user_101", clients) for _ in range(5)))

    assert calls["n"] == 1


async def test_cache_prevents_a_second_upstream_call():
    calls = {"n": 0}

    def counting(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/horoscope/"):
            calls["n"] += 1
        return healthy(request)

    clients, cache = make_clients(counting)
    await gather_context("user_101", clients)
    await gather_context("user_101", clients)
    assert calls["n"] == 1

    cache.clear()
    await gather_context("user_101", clients)
    assert calls["n"] == 2
