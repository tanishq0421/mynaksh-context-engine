"""End-to-end tests over real HTTP.

The unit suites exercise the pipeline in-process. These start both servers as
subprocesses on free ports and talk to them over a socket, which is the only way
the parts that matter here actually run: the HTTP client, the retry loop, the
per-attempt timeout, and the parse of a body that arrived over the wire. Patch
those out and the resilience layer becomes code that compiles but is never
executed.

Self-contained on purpose — the fixture owns the servers rather than assuming
something is already listening, so a stale process from a demo cannot make the
suite pass or fail for the wrong reason. Faults and cache are reset around every
test, so ordering cannot leak state.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
CAREER_Q = "Should I consider changing my job this year?"
GENERAL_Q = "What should I prioritize this week?"


def free_port() -> int:
    """Bind :0 and let the OS choose. Fixed ports would collide with a dev
    server and turn an unrelated running process into a test failure."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_until_healthy(url: str, proc: subprocess.Popen, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"{url} exited early with code {proc.returncode}")
        try:
            if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.15)
    raise RuntimeError(f"{url} never became healthy")


@pytest.fixture(scope="session")
def servers():
    mock_port, engine_port = free_port(), free_port()
    mocks_url = f"http://127.0.0.1:{mock_port}"
    engine_url = f"http://127.0.0.1:{engine_port}"

    env = {
        **os.environ,
        "ENABLE_ADMIN_ENDPOINTS": "true",
        "USER_SERVICE_URL": mocks_url,
        "KUNDLI_SERVICE_URL": mocks_url,
        "HOROSCOPE_SERVICE_URL": mocks_url,
        "PANCHANG_SERVICE_URL": mocks_url,
        "LOG_LEVEL": "WARNING",
    }

    def spawn(target: str, port: int) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, "-m", "uvicorn", target, "--port", str(port), "--log-level", "warning"],
            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    mocks = spawn("mock_services.main:app", mock_port)
    engine = spawn("app.main:app", engine_port)
    try:
        wait_until_healthy(mocks_url, mocks)
        wait_until_healthy(engine_url, engine)
        yield engine_url, mocks_url
    finally:
        for proc in (engine, mocks):
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


@pytest.fixture
def api(servers):
    """One client per test, with faults and cache reset either side.

    A warm cache from a previous test would mask an injected fault and the
    failure would look like the fault not firing.
    """
    engine_url, mocks_url = servers

    class API:
        engine = engine_url
        mocks = mocks_url

        def ask(self, question=CAREER_Q, user="user_101", **kw):
            return httpx.post(
                f"{engine_url}/personalize",
                json={"userId": user, "question": question}, timeout=60, **kw,
            )

        def debug(self, question=CAREER_Q, user="user_101"):
            return httpx.post(
                f"{engine_url}/debug/personalization",
                json={"userId": user, "question": question}, timeout=60,
            )

        def fault(self, **services):
            httpx.post(f"{mocks_url}/_faults", json=services, timeout=10)

        def reset(self):
            httpx.delete(f"{mocks_url}/_faults", timeout=10)
            httpx.delete(f"{engine_url}/_cache", timeout=10)

    client = API()
    client.reset()
    yield client
    client.reset()


# -- contract --------------------------------------------------------------


def test_personalize_returns_exactly_the_specified_shape(api):
    body = api.ask().json()
    assert set(body) == {"answer", "confidence", "sourcesUsed"}
    assert body["confidence"] in {"HIGH", "MEDIUM", "LOW"}
    assert body["answer"].strip()


def test_debug_returns_the_specified_keys_and_never_calls_the_llm(api):
    body = api.debug().json()
    for key in ("intent", "selectedContext", "excludedContext", "language", "tone"):
        assert key in body
    assert "answer" not in body


@pytest.mark.parametrize(
    "question,intent",
    [
        (CAREER_Q, "career"),
        ("How does this month look for my relationship?", "relationship"),
        ("What should I focus on for my health?", "health"),
        (GENERAL_Q, "general"),
        ("Can you summarize today's guidance?", "general"),
    ],
)
def test_sample_questions_over_http(api, question, intent):
    assert api.debug(question).json()["intent"] == intent
    assert api.ask(question).status_code == 200


def test_sources_used_matches_what_the_engine_selected(api):
    """The two endpoints must agree. If they drift, `sourcesUsed` is claiming
    context the prompt never contained."""
    assert api.ask().json()["sourcesUsed"] == api.debug().json()["selectedContext"]


# -- personalization -------------------------------------------------------


def test_personalization_varies_by_user(api):
    premium = api.debug(user="user_101").json()
    free = api.debug(user="user_102").json()
    assert free["maxWords"] < premium["maxWords"]


def test_tone_guardrail_applies_per_intent(api):
    career = api.debug(CAREER_Q, user="user_101").json()["tone"]
    health = api.debug("What should I focus on for my health?", user="user_101").json()["tone"]
    assert career.lower() == "motivational"
    assert health.lower() != "motivational"


def test_multi_intent_merges_and_draws_from_both(api):
    body = api.debug("Will I get a salary hike this year?").json()
    assert body["intentReason"] == "AMBIGUOUS_MERGED"
    assert set(body["intentWeights"]) == {"career", "finance"}
    selected = body["selectedContext"]
    assert "10th House" in selected and "Finance Horoscope" in selected


def test_out_of_domain_is_declined_with_no_sources(api):
    body = api.ask("What is the capital of France?").json()
    assert body["confidence"] == "LOW"
    assert body["sourcesUsed"] == []


# -- failure handling, over a real socket ---------------------------------


def test_unknown_user_is_404_not_503(api):
    """A missing user is a fact about the request, not an outage."""
    assert api.ask(user="does_not_exist").status_code == 404


@pytest.mark.parametrize("mode", ["error", "malformed", "timeout"])
def test_degradable_failure_still_answers(api, mode):
    """A 500, a wrong-shaped 200 and a timeout all mean the same thing to the
    caller: kundli is unusable. All three must converge on a degraded answer
    rather than each failing its own way."""
    api.fault(kundli=mode)
    response = api.ask()

    assert response.status_code == 200
    body = response.json()
    assert body["sourcesUsed"], "should still answer from the surviving services"
    assert not any("House" in s for s in body["sourcesUsed"])


def test_required_service_down_is_503(api):
    api.fault(user="error")
    assert api.ask().status_code == 503


def test_total_outage_declines_without_fabricating(api):
    """Nothing resolved. Answering anyway would mean generating astrology from
    no facts, which is worse than saying so."""
    api.fault(kundli="error", horoscope="error", panchang="error")
    body = api.ask().json()
    assert body["sourcesUsed"] == []
    assert body["confidence"] == "LOW"


def test_stale_cache_survives_an_outage(api):
    """Warm the cache, then kill the upstream. The full answer should still be
    served from last-known-good — the difference between degraded and broken."""
    warm = api.ask().json()
    api.fault(kundli="error")
    during = api.ask().json()
    assert during["sourcesUsed"] == warm["sourcesUsed"]


def test_fan_out_is_concurrent(api):
    """Four services delayed ~1s each. Serial execution would take ~4s."""
    api.fault(user="slow", kundli="slow", horoscope="slow", panchang="slow")
    started = time.perf_counter()
    assert api.ask().status_code == 200
    elapsed = time.perf_counter() - started
    assert elapsed < 2.5, f"fan-out serialized: {elapsed:.2f}s"


def test_timeout_is_bounded_by_the_client_not_the_upstream(api):
    """The upstream sleeps 10s. The client deadline is 2s, so the request must
    return well before the upstream would have."""
    api.fault(kundli="timeout")
    started = time.perf_counter()
    assert api.ask().status_code == 200
    assert time.perf_counter() - started < 9.0


def test_cache_prevents_a_repeat_upstream_call(api):
    """Second call is served from cache, so it must be markedly faster than a
    cold one that paid four network round trips."""
    api.fault(kundli="slow", horoscope="slow")
    cold = time.perf_counter()
    api.ask()
    cold_ms = time.perf_counter() - cold

    warm = time.perf_counter()
    api.ask()
    warm_ms = time.perf_counter() - warm

    assert warm_ms < cold_ms / 2


# -- validation ------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [{"userId": "user_101"}, {"question": "hi"}, {"userId": "user_101", "question": ""}],
)
def test_malformed_requests_are_rejected(servers, payload):
    engine_url, _ = servers
    response = httpx.post(f"{engine_url}/personalize", json=payload, timeout=30)
    assert response.status_code == 422


def test_request_id_is_returned_for_correlation(api):
    assert api.ask().headers.get("X-Request-ID")


def test_identical_requests_are_deterministic(api):
    assert api.ask().json() == api.ask().json()
