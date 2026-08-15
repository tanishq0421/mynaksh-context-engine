"""The one place upstream resilience is implemented.

Every client shares the same failure modes — slow upstream, flaky connection,
500, 404, and (the one people forget) a perfectly healthy 200 carrying a body
that does not match the contract. Putting timeout, retry, backoff, caching and
degradation in the base class means a subclass is a URL and a shape check, and
that there is exactly one implementation of "what happens when astrology
services misbehave" to reason about.

Deliberately absent: a circuit breaker. At four upstreams and this traffic,
bounded retries plus a short timeout plus stale-on-error already bound the
damage; a breaker would add state, a half-open policy and a tuning surface to
protect against a stampede that single-flight already prevents. Noted as an
omission so it reads as a decision rather than a gap.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any

import httpx

from app.config_schema import ServicePolicy
from app.domain import FetchResult
from app.services.cache import TTLCache, cache_key

logger = logging.getLogger(__name__)


class UpstreamError(Exception):
    """Base for anything that makes one attempt unusable."""


class UpstreamStatusError(UpstreamError):
    """Non-2xx response. Carries the code so the retry policy can consult it."""

    def __init__(self, service: str, status_code: int) -> None:
        super().__init__(f"{service} returned HTTP {status_code}")
        self.status_code = status_code

    @property
    def is_retryable(self) -> bool:
        # A 4xx is an answer, not a fault: a 404 means this user has no kundli
        # and will still have none in 100ms. Retrying it burns the timeout
        # budget of the whole request to re-learn something we already know.
        return self.status_code >= 500


class UpstreamMalformedError(UpstreamError):
    """A 2xx whose body does not match the contract.

    Distinct from a transport error on purpose. It is a *different code path*
    with an identical outcome (a failed FetchResult), and conflating them is how
    a schema drift upstream turns into a 500 here instead of a degraded answer.
    Not retried: the same upstream will serialise the same wrong shape again.
    """


class UpstreamClient(ABC):
    """One upstream service. `fetch` is total — it returns, it does not raise."""

    name: str

    def __init__(
        self,
        base_url: str,
        policy: ServicePolicy,
        cache: TTLCache,
        http_client: httpx.AsyncClient,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.policy = policy
        self.cache = cache
        # Injected, never constructed here: an AsyncClient owns a connection
        # pool, and building one per request throws the pool away every time and
        # pays a fresh TCP/TLS handshake for each call.
        self.http = http_client

    # -- subclass contract -------------------------------------------------

    @abstractmethod
    def path(self, identifier: str | None) -> str:
        """Path below `base_url`, e.g. `/users/u1`."""

    @abstractmethod
    def parse(self, raw: dict[str, Any]) -> Any:
        """Validate the shape and return the payload.

        Raise on anything unexpected; `fetch` converts that into a failed
        FetchResult. Subclasses never handle errors themselves.
        """

    def cache_identifier(self, identifier: str | None) -> str | None:
        """What the cache key is scoped by. Per-user by default; panchang
        overrides this to the date, since one payload serves everybody."""
        return identifier

    # -- the only public entry point --------------------------------------

    async def fetch(self, identifier: str | None = None) -> FetchResult:
        """Cache -> retries -> parse -> stale fallback. Never raises."""
        key = cache_key(self.name, self.cache_identifier(identifier))

        try:
            value, cached = await self.cache.get_or_fetch(
                key,
                self.policy.ttl_seconds,
                lambda: self._load(identifier),
            )
        except Exception as exc:  # noqa: BLE001 - failure is a return value here
            error = self._describe(exc)
            if self.policy.serve_stale_on_error and self.cache.has_stale(key):
                logger.warning("%s failed (%s); serving stale cache", self.name, error)
                return FetchResult(
                    service=self.name,
                    ok=True,
                    data=self.cache.get_stale(key),
                    error=error,  # kept: a stale success still has a cause worth logging
                    stale=True,
                )
            logger.warning("%s failed with no usable cache: %s", self.name, error)
            return FetchResult(service=self.name, ok=False, error=error)

        if cached:
            logger.debug("%s cache hit", self.name)
        return FetchResult(service=self.name, ok=True, data=value)

    # -- internals ---------------------------------------------------------

    async def _load(self, identifier: str | None) -> Any:
        """One logical fetch: up to `retries + 1` attempts, then parse.

        Raises on total failure so `get_or_fetch` never caches a failure and
        `fetch` can decide between stale and hard failure in one place.
        """
        url = f"{self.base_url}{self.path(identifier)}"
        attempts = self.policy.retries + 1
        last: Exception | None = None

        for attempt in range(attempts):
            try:
                response = await self.http.get(url, timeout=self.policy.timeout_seconds)
                if response.status_code >= 400:
                    raise UpstreamStatusError(self.name, response.status_code)
                raw = response.json()
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = exc
            except UpstreamStatusError as exc:
                if not exc.is_retryable:
                    raise
                last = exc
            except ValueError as exc:  # json() on a non-JSON body
                raise UpstreamMalformedError(f"{self.name}: invalid JSON body") from exc
            else:
                # Parsing lives outside the retry loop's error handling: a body
                # that fails validation is a contract problem, not a blip, and
                # replaying the request only produces the same wrong shape.
                try:
                    if not isinstance(raw, dict):
                        raise UpstreamMalformedError(
                            f"{self.name}: expected a JSON object, got {type(raw).__name__}"
                        )
                    return self.parse(raw)
                except UpstreamMalformedError:
                    raise
                except Exception as exc:  # noqa: BLE001 - normalised, then raised
                    raise UpstreamMalformedError(f"{self.name}: {exc}") from exc

            if attempt < attempts - 1:
                # Exponential, so a struggling upstream gets progressively more
                # room instead of a second hit while it is still recovering.
                await asyncio.sleep(self.policy.backoff_seconds * (2**attempt))

        assert last is not None  # unreachable: the loop runs at least once
        raise last

    def _describe(self, exc: Exception) -> str:
        if isinstance(exc, UpstreamError):
            return str(exc)
        if isinstance(exc, httpx.TimeoutException):
            return f"{self.name}: timed out after {self.policy.timeout_seconds}s"
        if isinstance(exc, httpx.TransportError):
            return f"{self.name}: connection error ({exc.__class__.__name__})"
        # An unexpected type here is a bug in our code, not the upstream's.
        # It still must not escape: one broken client cannot fail the request.
        logger.exception("unexpected error fetching %s", self.name)
        return f"{self.name}: unexpected error ({exc.__class__.__name__})"
