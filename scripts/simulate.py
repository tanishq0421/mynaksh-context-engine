"""Run a permutation matrix over service health x question x user and write a
verifiable report.

Every scenario calls BOTH endpoints against the same service configuration, and
attaches the structured log lines each call produced (correlated by request id),
so the report can be checked by hand without trusting a summary.

Usage:
    python scripts/simulate.py [--out simulation-report.md]

Requires the engine on :8000 (with ENABLE_ADMIN_ENDPOINTS=true) and the mock
services on :8001.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path

import httpx

ENGINE = "http://localhost:8000"
MOCKS = "http://localhost:8001"
# Override with ENGINE_LOG when the engine writes elsewhere. Without a log file
# the report still builds; the per-request log blocks just say so.
LOG = Path(os.environ.get("ENGINE_LOG", "/tmp/engine.log"))

USERS = {
    "user_101": "premium, tone=motivational, full chart",
    "user_102": "free tier, tone=direct, inverted chart",
    "user_103": "premium, tone=calm, kundli MISSING house 10",
}

QUESTIONS = {
    "career": "Should I consider changing my job this year?",
    "relationship": "How does this month look for my relationship?",
    "health": "What should I focus on for my health?",
    "week": "What should I prioritize this week?",
    "today": "Can you summarize today's guidance?",
    "ambiguous": "Will I get a salary hike this year?",
    "out_of_domain": "What is the capital of France?",
}

# (label, faults, note). `user` is the only REQUIRED service.
SCENARIOS = [
    ("S1  all services up", {}, "baseline"),
    ("S2  kundli 500", {"kundli": "error"}, "loses every house, dasha, lagna, moon sign"),
    ("S3  horoscope 500", {"horoscope": "error"}, "loses all four horoscope areas"),
    ("S4  panchang 500", {"panchang": "error"}, "loses only the daily panchang"),
    ("S5  kundli + horoscope 500", {"kundli": "error", "horoscope": "error"}, "chart and horoscope both gone"),
    ("S6  kundli + panchang 500", {"kundli": "error", "panchang": "error"}, ""),
    ("S7  horoscope + panchang 500", {"horoscope": "error", "panchang": "error"}, ""),
    ("S8  all degradable 500", {"kundli": "error", "horoscope": "error", "panchang": "error"},
     "nothing left to ground on: must decline WITHOUT calling the LLM"),
    ("S9  user 500 (REQUIRED)", {"user": "error"}, "no profile means nothing to personalize: 503"),
    ("S10 kundli timeout", {"kundli": "timeout"}, "upstream sleeps 10s, client deadline is 2s"),
    ("S11 kundli malformed 200", {"kundli": "malformed"}, "valid HTTP, wrong shape"),
    ("S12 all services slow", {"user": "slow", "kundli": "slow", "horoscope": "slow", "panchang": "slow"},
     "proves the fan-out is concurrent: total ~= slowest, not sum"),
]

# Scenarios beyond the baseline run a representative subset, to keep the report
# readable rather than exhaustive-but-unreadable.
DEGRADED_QUESTIONS = ["career", "week"]


def set_faults(faults: dict[str, str]) -> None:
    httpx.delete(f"{MOCKS}/_faults", timeout=10)
    if faults:
        httpx.post(f"{MOCKS}/_faults", json=faults, timeout=10)


def clear_cache() -> None:
    """Every scenario starts cold, or a warm entry from the previous scenario
    would mask the fault under test (that is stale-on-error, covered separately)."""
    httpx.delete(f"{ENGINE}/_cache", timeout=10)


def call(path: str, user: str, question: str) -> tuple[int, dict, str, float]:
    started = time.perf_counter()
    try:
        r = httpx.post(
            f"{ENGINE}{path}", json={"userId": user, "question": question}, timeout=60
        )
        elapsed = (time.perf_counter() - started) * 1000
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        return r.status_code, body, r.headers.get("X-Request-ID", "-"), elapsed
    except Exception as exc:  # noqa: BLE001 - the report should record failures too
        return 0, {"error": repr(exc)}, "-", (time.perf_counter() - started) * 1000


def log_lines_for(request_id: str) -> list[dict]:
    if request_id == "-" or not LOG.exists():
        return []
    out = []
    for line in LOG.read_text(errors="ignore").splitlines():
        if request_id not in line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


LOG_FIELDS = [
    "event", "service", "outcome", "latency_ms", "reason", "ok", "failed", "stale",
    "intent", "intent_reason", "time_scope", "selected", "unavailable",
    "dropped_for_budget", "context_tokens", "items", "total_prompt_tokens",
    "status", "path",
]


def render_logs(request_id: str, indent: str = "") -> list[str]:
    rows = log_lines_for(request_id)
    if not rows:
        return [f"{indent}(no log lines captured)"]
    out = []
    for row in rows:
        fields = {k: row[k] for k in LOG_FIELDS if k in row}
        out.append(f'{indent}{row.get("level","?"):<7} {row.get("message","")}')
        if fields:
            out.append(f"{indent}        {json.dumps(fields)}")
    return out


def render_run(user: str, key: str, question: str) -> list[str]:
    out = [f"#### `{key}` — {user}", "", f"> {question}", ""]

    status, debug, rid_d, ms_d = call("/debug/personalization", user, question)
    out += ["**POST /debug/personalization** (no LLM call)", "", "```json"]
    if status == 200:
        out.append(json.dumps(debug, indent=2))
    else:
        out.append(json.dumps({"status": status, **debug}, indent=2))
    out += ["```", "", f"_{ms_d:.0f}ms, request id `{rid_d}`_", ""]
    out += ["<details><summary>logs</summary>", "", "```"]
    out += render_logs(rid_d)
    out += ["```", "", "</details>", ""]

    status, answer, rid_p, ms_p = call("/personalize", user, question)
    out += [f"**POST /personalize** — HTTP {status}", "", "```json"]
    out.append(json.dumps(answer, indent=2))
    out += ["```", "", f"_{ms_p:.0f}ms, request id `{rid_p}`_", ""]
    out += ["<details><summary>logs</summary>", "", "```"]
    out += render_logs(rid_p)
    out += ["```", "", "</details>", "", "---", ""]
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="simulation-report.md")
    args = parser.parse_args()

    doc: list[str] = [
        "# Simulation report",
        "",
        "Generated by `scripts/simulate.py`. Every scenario calls **both** endpoints",
        "against the same service configuration, with a cold cache, and attaches the",
        "structured log lines each call emitted (correlated by request id).",
        "",
        "## Fixture users", "",
        "| id | profile |", "|---|---|",
    ]
    doc += [f"| `{u}` | {d} |" for u, d in USERS.items()]
    doc += ["", "## Scenarios", "", "| # | faults | expectation |", "|---|---|---|"]
    doc += [
        f"| {label.split()[0]} | `{json.dumps(f) if f else 'none'}` | {note or '-'} |"
        for label, f, note in SCENARIOS
    ]
    doc += ["", "---", ""]

    for label, faults, note in SCENARIOS:
        print(f"running {label} ...", flush=True)
        set_faults(faults)
        clear_cache()

        doc += [f"## {label}", ""]
        if note:
            doc += [f"_{note}_", ""]
        doc += [f"Faults: `{json.dumps(faults) if faults else 'none'}`", "", "---", ""]

        if label.startswith("S1 "):
            # Baseline: the full matrix, every question against every user.
            for user in USERS:
                for key, question in QUESTIONS.items():
                    doc += render_run(user, key, question)
        else:
            for key in DEGRADED_QUESTIONS:
                doc += render_run("user_101", key, QUESTIONS[key])

    set_faults({})
    clear_cache()

    Path(args.out).write_text("\n".join(doc))
    print(f"\nwrote {args.out} ({len(doc)} lines)")


if __name__ == "__main__":
    main()
