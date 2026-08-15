# MyNaksh — Personalized AI Context Engine

The layer between four structured astrology backends (User, Kundli, Horoscope,
Panchang) and an LLM. It fetches context concurrently, works out what the
question is asking, selects **only** the relevant context, personalizes
language/tone/length from the profile, builds an optimized prompt, and returns a
grounded answer with a deterministic confidence score.

Two ideas carry the design:

1. **Context selection is scored, not looked up.** Config is authored as
   `primary` / `secondary` / `exclude` tiers, and compiled to numeric weights at
   boot. Multi-intent merging, time-scope relevance, budget ordering and
   backfill-on-failure all fall out of that; tier membership needs a hand-written
   special case for each.
2. **The debug endpoint runs the same code path**, stopping before the LLM — so
   it cannot drift into describing behaviour the live path no longer has.

> Diagrams, boot sequence and failure paths: **[ARCHITECTURE.md](ARCHITECTURE.md)**

---

## Run it

```bash
docker compose up --build      # engine :8000, mock services :8001
```

No API key needed — the LLM factory falls back to a deterministic mock provider
and logs which provider it resolved, so a demo run is never ambiguously
real-or-mocked. Set `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` to use a real one.

```bash
curl -sS -X POST localhost:8000/personalize -H 'Content-Type: application/json' \
  -d '{"userId":"user_101","question":"Should I consider changing my job in the next few months?"}'
```

```json
{
  "answer": "Aarav Sharma, the month ahead has something to work with …",
  "confidence": "HIGH",
  "sourcesUsed": ["10th House", "Career Horoscope", "Current Dasha", "Today's Panchang"]
}
```

`POST /debug/personalization` returns the same decision **without** calling the
LLM — intent, matched terms, every context item with its score and the reason for
it, and what was excluded, unavailable or dropped for budget.

---

## Simulating failures

**All of these need the stack already running** — `docker compose up --build`
first, then these in a second terminal. They call the live services; they do not
start anything.

The mock services expose a fault switch, so an upstream can be broken at runtime
without restarting or touching the engine:

```bash
make demo-healthy     # baseline: all four services up
make demo-degraded    # kundli returns 500 -> fewer sources, lower confidence
make demo-stale       # kundli returns 500, but the cache is warm -> full answer
make demo-reset       # clear all faults and the cache
```

`demo-degraded` and `demo-stale` inject the *same* fault and behave differently
on purpose. The only difference is the cache:

| | cache | result |
|---|---|---|
| `demo-degraded` | cleared first | 2 sources, confidence LOW — the engine answers from whatever survived |
| `demo-stale` | left warm | 4 sources, confidence HIGH — served from last-known-good |

That second one is the point of caching: an upstream outage degrades the answer
rather than breaking it. Auto-clearing the cache whenever a fault fired would
make that path impossible to demonstrate, which is why the sequencing lives in
the Makefile rather than in either service.

Pick the service and failure mode:

```bash
make demo-degraded SERVICE=horoscope FAULT=timeout
```

`FAULT` is one of `error` (500), `timeout` (sleeps past the client deadline),
`slow` (1s but succeeds — set all four to prove the fan-out is concurrent), or
`malformed` (a valid 200 carrying a wrong-shaped body).

Ask your own questions, as any of the three fixture users:

```bash
make ask   USER_ID=user_102 Q="What should I focus on for my health?"   # the answer
make debug USER_ID=user_102 Q="What should I focus on for my health?"   # the reasoning
```

`user_101` is premium/motivational with a full chart, `user_102` is free tier
with a different tone, and `user_103` has a valid kundli that is **missing its
10th house** — which exercises partial failure inside a *healthy* service.

Tests need no running stack; the live suite starts its own servers:

```bash
make test    # 102 tests: 75 unit + 27 over real HTTP
```

---

## What it does

Actual output, captured from a running server. Five of these are asserted in
`tests/test_live_api.py` and the rest in `tests/test_classifier.py`, though the
tests check the intent rather than every column — so treat the context and
confidence columns as a snapshot, not a guarantee.

| Question | Intent | Selected context | Confidence |
|---|---|---|---|
| Should I consider changing my job this year? | `career` | 10th House, Current Dasha, Career Horoscope, Panchang | HIGH |
| How does this month look for my relationship? | `relationship` | 7th House, Relationship Horoscope, Current Dasha, Moon Sign | HIGH |
| What should I focus on for my health? | `health` | Health Horoscope, 6th House, Moon Sign, Panchang | HIGH |
| What should I prioritize this week? | `general` | all 12 items | MEDIUM |
| Can you summarize today's guidance? | `general` | all 12, Panchang first | MEDIUM |
| Will I get a salary hike this year? | `career` **+** `finance` (0.38 / 0.62) | context from both | MEDIUM |
| Should I switch companies? | `career` | — no `job`/`career` token appears | HIGH |
| What does my Cancer moon sign mean? | `general` — **not** health | `cancer` is a sign as well as a disease | MEDIUM |
| What is the capital of France? | out of domain | declined; LLM never called | LOW |

The two `general` rows are correct, not failures: neither question is *about* a
topic, so `general` maps to all available context. Confidence drops to MEDIUM
because the intent was a fallback rather than a match.

---

## Personalization engine

Config stays readable — named tiers, not a table of decimals:

```yaml
career:
  primary:   [house_10, horoscope_career]
  secondary: [dasha_current, panchang_today]
  exclude:   [horoscope_relationship]
```

At boot this is compiled to item-keyed weights. Per request:

```
score(item) = Σ over active intents (intent_weight × tier_weight)
            + time_scope_modifier          # panchang +0.5 today, −0.4 this year
            → hard zero if excluded        # applied last, cannot be outvoted
```

Then walk the ranking, spending a token budget. Single-intent collapses to
exactly 1.0 / 0.5 — **scoring is not a different model, it is tiers with the
merge arithmetic made explicit.**

- **Exclusion is answer quality, not token saving.** Leave relationship context
  in a career prompt and the model works it into the answer.
- **Three reasons an item is absent** — excluded (a rule removed it), unavailable
  (an upstream failed), dropped for budget — reported separately, because
  collapsing them makes a deliberate decision look like an outage.
- **`maxWords = max(floor, min(subscription, intent, context volume))`.** The
  context term is a hallucination control: ask for 250 words when two thin facts
  survived and the model invents the rest. The floor stops it collapsing to
  nothing.
- **Adding an intent is one YAML block plus one line in the `Intent` enum.**
  The enum is the reason a typo in config dies at boot instead of silently
  selecting nothing — that safety costs one line of Python per intent. Adding a
  new *service* really is zero code beyond its client: those are string-keyed.

---

## Failure handling

| Scenario | Result |
|---|---|
| Degradable service 500 / malformed 200 / timeout | 200, degraded answer, confidence drops |
| All degradable services down | 200, declines honestly, **LLM never called** |
| Required service (user) down | 503 |
| Unknown user | 404 — a missing user is not an outage |
| Warm cache + upstream down | full answer from last-known-good |
| Four slow services | ~1.0s, not ~4.0s — fan-out is concurrent |

---

## Assumptions

- **English input only.** Cut on evidence: a multilingual embedding model matched
  romanized Hinglish on sentence *frame* rather than meaning, scoring 0.87 on the
  wrong intent. Shipping that would misclassify confidently.
- Upstream services mocked; payload shapes taken verbatim from the brief
- The horoscope service has no date field, so its content is assumed **daily**
- Multi-intent questions are merged rather than forced to a single winner
- Time scope is a second dimension orthogonal to intent
- Single instance, single worker — the cache is per-process

## Trade-offs

| Decision | Instead of | Why |
|---|---|---|
| Scored selection | Flat tier lists at runtime | Merge, time scope, budget and backfill come free; tiers need a special case each |
| Tiers in config, weights at boot | Numbers in the file | Named tiers are readable and reviewable; a wall of decimals is neither. One tier weight also changes policy everywhere, instead of a find-and-replace across every intent |
| Registry in code | Registry in YAML | Extractors and renderers are functions; logic in YAML is a language with no type checker |
| Rules-only classification | Embedding / LLM classifier | A deterministic fallback is needed anyway. Measured: rules handle every sample question at zero latency; embeddings scored 16/20 and add a model download |
| Deterministic confidence | Ask the model | Models are badly calibrated at self-reporting |
| `sourcesUsed` from selection | Ask the model to cite | Asking a model to cite invites invented citations |
| Mock services on a socket | In-process stubs | Otherwise the retry loop and timeouts are code that compiles but never runs |

**Rejected:**

- **One LLM call doing classify + answer.** Fatal: you cannot select context
  before knowing intent, so it forces sending everything.
- **A vector DB of vocabulary.** You embed the four scored intents (~40 vectors, a
  Python list), not every word a user might type.
- **DB-backed config.** Loses atomic deploy-with-code and boot validation; a row
  can reference a key the running build lacks.
- **Circuit breaker.** Retries + timeouts + stale-on-error already bound the
  damage at four upstreams; single-flight prevents the stampede it would guard.

## Known limitations

- **The out-of-domain gate is lexical.** "How do I reset my password?" contains
  "my" and passes. Structural, not tunable: "What should I prioritize this week?"
  is lexically identical and must stay in-domain.
- **The merge threshold has no margin on its own showcase.** "Will I get a
  salary hike this year?" produces `absoluteEvidence` of exactly 0.8 against a
  `min_evidence` of exactly 0.8, surviving only because the comparison is strict
  `<`. Raise the threshold by 0.01 and that row becomes `general`.
- **A stale-served upstream is invisible in the API.** Under stale-on-error the
  fetch reports success, so `upstreamFailures` is empty and the debug trace looks
  fully healthy — it cannot tell you the answer came from last-known-good.
- **Thresholds are provisional, not tuned** — across an 11-case trace,
  `min_evidence` changed the outcome zero times. Proper tuning needs a few
  hundred labelled questions.
- Token costs are a `len//4` estimate (±25%).
- Latency under a timing-out upstream is `timeout × (retries+1)`; there is no
  global request deadline.

## With another day

- Embedding tier behind the existing `Classifier` interface — the seam is already there, it needs one signal extractor.
- A real tokenizer, so the budget binds on actual tokens rather than a `len//4` estimate.
- A labelled question set, to tune the thresholds against evidence instead of judgement.
- Golden-output tests on the prompt builder, so a context-selection regression fails CI.
- Streaming responses, since a 250-word answer has real perceived latency today.

## Left out for production

- Multiple instances behind a load balancer — needs Redis first, because the cache is per-process.
- Circuit breaker — deliberate: retries, timeouts and stale-on-error already bound the damage at four upstreams.
- Auth, rate limiting and per-user quotas.
- Metrics and distributed tracing — there are structured JSON logs and nothing else.
- Prompt-injection defence on the user's question.
- LLM cost controls.
- Config hot reload — the `ConfigSource` seam exists, the database loader does not.

---

## Project structure

```
.
├── app/
│   ├── main.py                     app wiring; config validated + compiled at boot
│   ├── config.py                   deployment settings from .env
│   ├── config_schema.py            typed shapes for config/*.yaml
│   ├── config_loader.py            YAML → validated models; ConfigSource seam
│   ├── domain.py                   shared vocabulary; imports no layer
│   ├── confidence.py               deterministic confidence + sourcesUsed
│   │
│   ├── api/
│   │   ├── router.py               /personalize, /debug/personalization, /health, /_cache
│   │   └── request_response.py     wire contracts, kept apart from domain types
│   │
│   ├── middleware/
│   │   ├── context.py              request-id ContextVar
│   │   └── logging.py              JSON formatter + latency logging
│   │
│   ├── classifier/                 question → intent + time scope
│   │   ├── tokenizer.py            contractions, possessives — pinned first
│   │   ├── signals.py              lexicon, phrases, negation, time scope
│   │   ├── domain_gate.py          is this an astrology question at all
│   │   ├── rules.py                score vector + decision policy
│   │   └── base.py                 Classifier ABC — the embedding seam
│   │
│   ├── engine/                     what context, and why
│   │   ├── registry.py             12 context items: extractor, renderer, cost
│   │   ├── compiler.py             tiers → item-keyed weights, at boot
│   │   ├── planner.py              phase 1: scores, tone, language, maxWords
│   │   └── selector.py             phase 2: resolve vs availability + budget
│   │
│   ├── services/                   concurrent fan-out to the four upstreams
│   │   ├── base.py                 UpstreamClient ABC: timeout, retry, stale
│   │   ├── cache.py                per-service TTL, stale-on-error, single-flight
│   │   ├── user.py kundli.py horoscope.py panchang.py
│   │   └── aggregator.py           gather + criticality policy
│   │
│   ├── prompt/builder.py           selected context only; logs prompt size
│   └── llm/
│       ├── base.py                 LLMClient ABC
│       ├── factory.py              provider resolution + mock fallback
│       └── anthropic.py openai.py mock.py
│
├── config/                         behaviour as data, not code
│   ├── intents.yaml                lexicon, thresholds, domain gate, time scope
│   ├── personalization.yaml        tiers, modifiers, tone rules, length caps
│   └── services.yaml               timeout, retries, TTL, criticality
│
├── mock_services/                  scaffolding — stands in for real backends
│   ├── main.py                     the four endpoints, on their own port
│   ├── data.py                     three fixture users
│   └── faults.py                   error / timeout / slow / malformed injection
│
├── tests/
│   ├── conftest.py                 fixtures load the real config
│   ├── test_classifier.py          regressions for defects a trace found
│   ├── test_multi_intent.py        merge fires when it should, and not otherwise
│   ├── test_engine_selection.py    scoring, exclusion, backfill, budget
│   ├── test_aggregator_failures.py concurrency, retries, stale, malformed
│   └── test_live_api.py            both endpoints over a real socket
│
├── scripts/simulate.py             12-config failure matrix → report with logs
├── ARCHITECTURE.md                 diagrams, boot sequence, failure paths
├── Dockerfile  docker-compose.yml  one image, two roles
├── Makefile                        run, demo and fault-injection targets
└── pyproject.toml  .env.example
```

## Tests

```bash
make test        # 102 tests: 75 unit + 27 over real HTTP
```

The live suite starts both servers on free ports and talks to them over a
socket, because that is the only way the HTTP client, retry loop, per-attempt
timeout and wire-format parsing actually execute.

`scripts/simulate.py` runs a 12-configuration failure matrix across both
endpoints and writes a report with the JSON logs attached, for manual review.
