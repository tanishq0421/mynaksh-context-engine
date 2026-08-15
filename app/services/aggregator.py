"""Concurrent fan-out across every upstream, with a criticality policy.

Two properties matter here and nothing else does:

*Latency is the slowest service, not the sum.* Four sequential 300ms calls make
a 1.2s request out of four fast ones. `asyncio.gather` makes it 300ms, and since
these are independent reads there is no ordering constraint to trade away.

*One failure does not become no answer.* Each client's `fetch` already returns a
value instead of raising, and `return_exceptions=True` on top of that is belt
and braces: if a client has a bug we want that one service marked failed, not
the whole fan-out unwound.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from app.config import Settings
from app.config_schema import ServicePolicy, ServicesConfig
from app.domain import ContextBundle, Criticality, FetchResult
from app.services.base import UpstreamClient
from app.services.cache import TTLCache
from app.services.horoscope import HoroscopeClient
from app.services.kundli import KundliClient
from app.services.panchang import PanchangClient
from app.services.user import UserClient

logger = logging.getLogger(__name__)

# The canonical registry. Adding a service is one line here plus a client file —
# nothing in the aggregator, the engine or the API special-cases a service name.
CLIENT_TYPES: dict[str, type[UpstreamClient]] = {
    "user": UserClient,
    "kundli": KundliClient,
    "horoscope": HoroscopeClient,
    "panchang": PanchangClient,
}


class CriticalServiceError(RuntimeError):
    """A REQUIRED upstream failed and the request cannot be personalized.

    Typed rather than a bare RuntimeError so the API layer can map exactly this
    to a 503 without pattern-matching on message text.
    """

    def __init__(self, service: str, reason: str, status_code: int | None = None) -> None:
        super().__init__(f"required service '{service}' unavailable: {reason}")
        self.service = service
        self.reason = reason
        # An upstream 404 means the user does not exist, which is a 404 to our
        # caller — not a 503. Same failure path, different answer.
        self.status_code = status_code


def build_clients(
    settings: Settings,
    services_config: ServicesConfig,
    cache: TTLCache,
    http_client: httpx.AsyncClient,
) -> dict[str, UpstreamClient]:
    """Wire deployment settings (URLs) to behavioural config (policies).

    A service absent from services.yaml gets the ServicePolicy defaults rather
    than being dropped: an omitted policy is a config gap, and silently not
    fetching kundli would be far harder to notice than a wrong timeout.
    """
    clients: dict[str, UpstreamClient] = {}
    for name, client_type in CLIENT_TYPES.items():
        policy = services_config.services.get(name) or ServicePolicy()
        clients[name] = client_type(
            base_url=settings.service_url(name),
            policy=policy,
            cache=cache,
            http_client=http_client,
        )
    return clients


async def gather_context(user_id: str, clients: dict[str, UpstreamClient]) -> ContextBundle:
    """Fetch every service concurrently and fold the results into one bundle.

    Note what the bundle is *for*: the personalization engine never reads
    `failures`. The engine decides relevance as a pure function of intent and
    config, and only the selector resolves that relevance against what actually
    arrived. If the engine consumed availability, the same question would
    produce different plans on different days and /debug/personalization would
    stop being reproducible — the ranking would silently encode which upstream
    happened to be down when you ran it.
    """
    names = list(clients)
    started = time.perf_counter()

    results = await asyncio.gather(
        *(_timed_fetch(clients[name], user_id) for name in names),
        return_exceptions=True,
    )

    bundle = ContextBundle()
    for name, outcome in zip(names, results):
        policy = clients[name].policy

        if isinstance(outcome, BaseException):
            # Only reachable if a client raised despite fetch's contract, i.e. a
            # bug on our side. Treated as an ordinary failure so the blast radius
            # stays one service.
            logger.exception("client %s raised through fetch()", name, exc_info=outcome)
            result = FetchResult(service=name, ok=False, error=f"internal error: {outcome!r}")
        else:
            result = outcome

        if result.ok:
            bundle.data[name] = result.data
            if result.stale:
                bundle.stale.add(name)
            continue

        if policy.criticality is Criticality.REQUIRED:
            raise CriticalServiceError(
                name, result.error or "unknown error", result.status_code
            )
        bundle.failures[name] = result.error or "unknown error"

    logger.info(
        "context fan-out for %s in %.0fms: ok=%s stale=%s failed=%s",
        user_id,
        (time.perf_counter() - started) * 1000,
        sorted(bundle.data),
        sorted(bundle.stale),
        sorted(bundle.failures),
    )
    return bundle


async def _timed_fetch(client: UpstreamClient, user_id: str) -> FetchResult:
    """Per-service latency and outcome, which is what makes a slow request
    diagnosable — the total alone cannot say which upstream caused it."""
    started = time.perf_counter()
    result = await client.fetch(user_id)
    elapsed_ms = (time.perf_counter() - started) * 1000

    if not result.ok:
        outcome = "failed"
    elif result.stale:
        outcome = "stale"
    else:
        outcome = "ok"
    logger.info("service=%s outcome=%s latency_ms=%.1f", client.name, outcome, elapsed_ms)
    return result
