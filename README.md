# MyNaksh — Personalized AI Context Engine

## What this is

The intelligence layer between four structured astrology backends (User, Kundli,
Horoscope, Panchang) and an LLM. It gathers user context concurrently,
determines what the question is actually asking, selects only the context that
is relevant to that question, personalizes language, tone and length from the
user's profile, builds an optimized prompt from the selected facts alone, and
returns a grounded answer with a deterministic confidence score. The two graded
ideas are that **context selection is scored rather than looked up**, and that
**configuration is authored in a domain expert's vocabulary and compiled to a
numeric runtime form at boot** — the same shape on both sides of the system, so
there is one mental model instead of two.

---

## Quick start

```bash
docker compose up --build          # engine :8000, mock services :8001
```

No API key required — the LLM factory resolves to a deterministic mock provider
and says so loudly at startup.

```bash
curl -sS -X POST http://localhost:8000/personalize \
  -H 'Content-Type: application/json' \
  -d '{"userId":"user_101","question":"Should I consider changing my job in the next few months?"}'
```

Real, verified output:

```json
{
    "answer": "Aarav Sharma, the month ahead has something to work with — here is what the chart shows. Your strongest card is your 10th house: lord Moon, strength Strong. Adding to that, your career horoscope: Networking may bring new opportunities. Backing that up, your current dasha: Rahu mahadasha, Mars antardasha. You also hold your today's panchang: Shukla Panchami tithi, Rohini nakshatra, Siddhi yoga, Bava karana. The openings are there; the effort is the part that is yours.",
    "confidence": "HIGH",
    "sourcesUsed": [
        "10th House",
        "Career Horoscope",
        "Current Dasha",
        "Today's Panchang"
    ]
}
```

The same question through the debug endpoint, which runs the identical pipeline
and stops before the LLM:

```bash
curl -sS -X POST http://localhost:8000/debug/personalization \
  -H 'Content-Type: application/json' \
  -d '{"userId":"user_101","question":"Should I consider changing my job in the next few months?"}'
```

```json
{
    "intent": "career",
    "intentReason": "CONFIDENT_MATCH",
    "inDomain": true,
    "timeScope": "this_month",
    "intentWeights": { "career": 1.0 },
    "absoluteEvidence": 1.2,
    "relativeDominance": 1.0,
    "matchedTerms": { "career": ["job"] },
    "language": "en",
    "tone": "motivational",
    "maxWords": 180,
    "selectedContext": ["10th House", "Career Horoscope", "Current Dasha", "Today's Panchang"],
    "excludedContext": ["Relationship Horoscope"],
    "unavailableContext": [],
    "droppedForBudget": [],
    "ranked": [
        { "key": "house_10",               "displayName": "10th House",             "score":  1.0, "reasons": ["career primary (1.0)"] },
        { "key": "horoscope_career",       "displayName": "Career Horoscope",       "score":  0.9, "reasons": ["career primary (1.0)", "this_month modifier (-0.10)"] },
        { "key": "dasha_current",          "displayName": "Current Dasha",          "score":  0.7, "reasons": ["career secondary (0.5)", "this_month modifier (+0.20)"] },
        { "key": "panchang_today",         "displayName": "Today's Panchang",       "score":  0.2, "reasons": ["career secondary (0.5)", "this_month modifier (-0.30)"] },
        { "key": "birth_details",          "displayName": "Birth Details",          "score":  0.0, "reasons": [] },
        { "key": "horoscope_relationship", "displayName": "Relationship Horoscope", "score":  0.0, "reasons": ["this_month modifier (-0.10)", "excluded by career"] },
        { "key": "house_6",                "displayName": "6th House",              "score":  0.0, "reasons": [] },
        { "key": "house_7",                "displayName": "7th House",              "score":  0.0, "reasons": [] },
        { "key": "lagna",                  "displayName": "Lagna",                  "score":  0.0, "reasons": [] },
        { "key": "moon_sign",              "displayName": "Moon Sign",              "score":  0.0, "reasons": [] },
        { "key": "horoscope_finance",      "displayName": "Finance Horoscope",      "score": -0.1, "reasons": ["this_month modifier (-0.10)"] },
        { "key": "horoscope_health",       "displayName": "Health Horoscope",       "score": -0.1, "reasons": ["this_month modifier (-0.10)"] }
    ],
    "contextTokens": 43,
    "upstreamFailures": {}
}
```

Two things to read off that output. `maxWords` is **180**, not the 250 the
premium tier allows: four context items survived, and 45 words per item is the
hallucination cap (see the Personalization Engine section). And the Relationship
Horoscope is `excludedContext`, not merely low-scoring — a rule actively removed
it, which is a different fact from the 6th House simply never being a candidate.

---

## The brief's sample questions

Live output, all five, `user_101`. Nothing here is illustrative — these are the
actual responses from a running server.

| Question | Intent | Reason | Time scope | Selected context | Confidence |
|---|---|---|---|---|---|
| Should I consider changing my job this year? | `career` | `CONFIDENT_MATCH` | `this_year` | 10th House, Current Dasha, Career Horoscope, Today's Panchang | HIGH |
| How does this month look for my relationship? | `relationship` | `CONFIDENT_MATCH` | `this_month` | 7th House, Relationship Horoscope, Current Dasha, Moon Sign | HIGH |
| What should I focus on for my health? | `health` | `CONFIDENT_MATCH` | `unspecified` | Health Horoscope, 6th House, Moon Sign, Today's Panchang | HIGH |
| What should I prioritize this week? | `general` | `NO_EVIDENCE_DEFAULT` | `this_week` | all 12 items | MEDIUM |
| Can you summarize today's guidance? | `general` | `NO_EVIDENCE_DEFAULT` | `today` | all 12 items, Panchang first | MEDIUM |

Four things worth reading off that table:

**The two `general` rows are not failures.** Neither question carries a topical
keyword, and neither is *about* a topic — "what should I prioritize" is a
request for everything. `NO_EVIDENCE_DEFAULT` is the classifier reporting
honestly that it found no evidence, and `general` maps to all available context,
which is the right answer. Confidence drops to MEDIUM because the intent was a
fallback rather than a match, and that is the system saying so rather than
hiding it.

**Exclusions are per-intent and mutual.** Career excludes Relationship
Horoscope; relationship excludes Career Horoscope; health excludes Finance
Horoscope. This is an answer-quality control, not a token saving: leave
relationship context in a career prompt and the model will work it into the
answer.

**Time scope moves selection independently of intent.** "This week" and "today"
produce different orderings of the same twelve items — Panchang leads for
`today` and sinks for `this_year`, while Dasha does the reverse.

**Beyond the brief**, the same path handles genuine ambiguity and questions that
are not ours to answer:

| Question | Result |
|---|---|
| Will I get a salary hike this year? | `AMBIGUOUS_MERGED` — `{finance: 0.62, career: 0.38}`, context drawn from both, confidence MEDIUM |
| Should I switch companies? | `career` — no `job` or `career` token appears; the paraphrase vocabulary carries it |
| What does my Cancer moon sign mean? | `general`, **not** health — `cancer` is a sign as well as a disease |
| What is the capital of France? | `OUT_OF_DOMAIN` — declined, and the LLM is never called |

---

## Running the service

### Docker (primary path)

```bash
docker compose up --build            # engine :8000, mocks :8001
docker compose logs -f engine        # follow structured JSON logs
docker compose down                  # stop everything
```

This needs **no API key**. `app/llm/factory.py` resolves the provider at startup
and logs which one won and why — at INFO for the expected paths and WARNING when
a key was requested but is missing. A demo run is therefore never ambiguously
real-or-mocked; the startup line tells you.

To use a real provider:

```bash
ANTHROPIC_API_KEY=sk-ant-... docker compose up --build
# or
OPENAI_API_KEY=sk-... docker compose up --build
```

The provider SDKs are optional extras (`pip install '.[anthropic]'`), imported
lazily so the service starts without either installed.

### Local (venv)

```bash
python3 -m venv .venv
./.venv/bin/pip install -e ".[dev]"

# terminal 1 — mock upstream services
./.venv/bin/python -m uvicorn mock_services.main:app --port 8001

# terminal 2 — the engine
ENABLE_ADMIN_ENDPOINTS=true ./.venv/bin/python -m uvicorn app.main:app --port 8000
```

**Run one worker.** The cache is an in-memory dict and therefore per-process.
With several workers, cache hits become a coin flip and `DELETE /_cache` would
clear only whichever worker happened to serve that request. Multiple instances
is a production concern and it needs Redis first — see *Production concerns left
out*.

Copy `.env.example` to `.env` to change anything; every value has a working
default.

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/personalize` | the graded endpoint: `{answer, confidence, sourcesUsed}` |
| `POST` | `/debug/personalization` | same pipeline, stops before the LLM, returns the full reasoning trace |
| `GET` | `/health` | liveness plus the resolved LLM provider name |
| `DELETE` | `/_cache` | dev only, gated behind `ENABLE_ADMIN_ENDPOINTS` |

### Fixture users

Three, because with one user every response looks identical and you cannot
demonstrate that personalization actually varies.

| User | Profile | What it demonstrates |
|---|---|---|
| `user_101` | premium, tone `motivational`, full chart | The spec's canonical example, reproduced byte-for-byte so the demo output can be diffed against the assignment. |
| `user_102` | free, tone `direct`, inverted chart strengths | Subscription changes the word budget (**120** vs user_101's 180) and tone changes the prompt. |
| `user_103` | premium, tone `calm`, kundli valid but **missing house 10** | Partial failure *inside a healthy service*: 200 OK, well-formed body, the extractor finds nothing, the item degrades to `unavailable`. A career question for this user still answers, minus the house. |

### Makefile

```
make up                              # docker compose up --build -d
make down                            # stop
make logs                            # tail engine logs
make test                            # run the test suite
make ask   USER=user_102 Q="..."     # POST /personalize, pretty-printed
make debug USER=user_101 Q="..."     # POST /debug/personalization
make demo-degraded                   # cold cache + fault  -> degraded answer
make demo-stale                      # warm cache + fault  -> stale-on-error
make demo-reset                      # clear all faults and the cache
```

---

## Simulating failure cases

The mock services accept runtime fault injection, so a live demo can break a
specific upstream mid-run and watch the engine retry, time out, degrade and
recover — without restarting anything. Resilience you can only *assert* is worth
nothing.

```bash
curl -X POST localhost:8001/_faults -H 'Content-Type: application/json' -d '{"kundli":"error"}'
curl localhost:8001/_faults          # inspect current state
curl -X DELETE localhost:8001/_faults # clear
```

`POST /_faults` replaces the whole table, so `{"kundli":"error","panchang":"slow"}`
sets exactly those two and clears anything else.

All results below were observed live against `user_101` asking the career
question, with the engine's cache cleared first unless stated otherwise.

| Fault | Command | Observed |
|---|---|---|
| Degradable 500 | `-d '{"kundli":"error"}'` | 200, degraded answer, confidence **LOW**, sources drop to Career Horoscope + Today's Panchang |
| Malformed 200 | `-d '{"kundli":"malformed"}'` | 200, degraded — a valid HTTP 200 carrying a wrong-shaped body becomes a failed fetch, not a crash |
| Timeout | `-d '{"kundli":"timeout"}'` | 200 in **~6.4s**, degraded. The upstream sleeps 10s; the client gives up at its 2s deadline and retries. It does **not** hang. |
| Slow but OK | `-d '{"kundli":"slow"}'` | 200, full answer, ~1s — under the client deadline, so it simply succeeds late |
| Required service down | `-d '{"user":"error"}'` | **503** with a clear message. No language, tone or subscription means there is nothing to personalize, and a generic reading dressed up as a personal one is worse than an error. |
| All degradable down | `-d '{"kundli":"error","horoscope":"error","panchang":"error"}'` | 200, an honest refusal, `sourcesUsed: []`, and **the LLM is never called** |
| Unknown user | ask with `"userId":"nope_999"` | **404**, not 503 — a missing user is an answer about the request, not an outage. The status code travels with the failure rather than being recovered by matching on message text. |
| Concurrency proof | all four services set to `slow` | **~1030ms** total, not ~4000ms. The fan-out is genuinely concurrent; latency is the slowest service, not the sum. |

Fault modes are `error` (500, retryable), `timeout` (sleeps past any sane
deadline), `slow` (succeeds, late), and `malformed` (200 with a body of the
wrong *type* — not merely empty, since an empty body is indistinguishable from a
sparse-but-valid record).

### The cache gotcha — read this before you conclude a fault did not fire

After a successful call the upstream response is cached. Set a fault, re-ask,
and you may still get the full answer. **That is stale-on-error working as
designed, not the fault failing.**

Two genuinely different behaviours are worth demonstrating, and the cache state
is exactly what separates them:

| Cache | + fault | Result |
|---|---|---|
| **cold** | kundli 500 | degraded: fewer sources, lower confidence |
| **warm** | kundli 500 | stale-on-error: full answer from last-known-good — verified **HIGH** confidence with all four sources while kundli was returning 500 |

The Makefile encodes the distinction, because it is the sequencing that matters:

```bash
make demo-degraded    # clears the engine cache first  -> degraded path
make demo-stale       # warms the cache, then faults   -> stale-on-error path
make demo-reset       # clear both faults and cache
```

The sequencing lives in the Makefile rather than in either service on purpose.
If the mock auto-cleared the engine's cache whenever a fault was set, the
stale-on-error path would become unreachable — and the scaffolding would be
reaching into the deliverable's API to do it. `DELETE /_cache` is a dev-only
affordance gated behind `ENABLE_ADMIN_ENDPOINTS`; exposed publicly it is a free
denial of service, since every request after a flush stampedes the upstreams at
once.

---

## Architecture

Full diagrams — ASCII and Mermaid — plus the boot sequence, failure paths and
extension seams are in **[ARCHITECTURE.md](ARCHITECTURE.md)**.

```
   Client
     │  POST /personalize   ·   POST /debug/personalization
     ▼
 ┌──────────────────────────────────────────────────────────────────────────┐
 │ 1 MIDDLEWARE       request-id ▸ JSON logging ▸ latency                   │
 ├──────────────────────────────────────────────────────────────────────────┤
 │ 2 INTENT CLASSIFIER   domain gate ▸ time-scope ▸ phrases ▸ terms ▸ neg.  │
 │                       evidence vector {intent: float} ▸ decision policy  │
 ├──────────────────────────────────────────────────────────────────────────┤
 │ 3 CONTEXT AGGREGATOR      asyncio.gather — latency = slowest, not sum    │
 │     ┌──────┐   ┌────────┐   ┌───────────┐   ┌──────────┐                 │
 │     │ user │   │ kundli │   │ horoscope │   │ panchang │ all four at once│
 │     └──┬───┘   └───┬────┘   └─────┬─────┘   └────┬─────┘                 │
 │        └───────────┴──────┬───────┴──────────────┘                       │
 │            ┌──────────────▼───────────────────────────────┐              │
 │            │ CACHE  per-service TTL · stale-on-error ·    │──▶ :8001     │
 │            │        single-flight                         │              │
 │            └──────────────────────────────────────────────┘              │
 ├──────────────────────────────────────────────────────────────────────────┤
 │ 4 PERSONALIZATION ENGINE                                                 │
 │     PLANNER  (pure — config only)  ▸  SELECTOR  (resolves vs. reality)   │
 │        ├─ selected                                                       │
 │        ├─ excluded          ─┐                                           │
 │        ├─ unavailable        ├─ three distinct absence reasons           │
 │        └─ droppedForBudget  ─┘                                           │
 │══════════ /debug/personalization RETURNS HERE — never calls the LLM ═════│
 │ 5 PROMPT BUILDER      selected items only, via renderers, never raw JSON │
 │ 6 LLM PROVIDER        anthropic │ openai │ mock          (swappable)     │
 │ 7 CONFIDENCE SCORER   coverage × intent certainty, deterministic         │
 │ 8 RESPONSE            {answer, confidence, sourcesUsed}                  │
 └──────────────────────────────────────────────────────────────────────────┘
     ▲                                                    ▲
     │ boot: config/*.yaml compiled to inverted index      │ boot: registry
     │       and item-keyed weights (fail-fast)            │ cross-validation
```

The request path, in order:

1. **Middleware** stamps a request id (honouring an inbound `X-Request-ID`) into
   a `ContextVar`, so every log line in the request correlates without threading
   an id through the classifier and the engine. Logs method, path, status and
   latency.
2. **Classify.** Tokenize, extract the time scope, run the in-domain gate, match
   phrases then terms, apply the negation discount, accumulate into a score
   vector, then apply a separate decision policy.
3. **Fan out** concurrently over all four services. Each client returns a value
   rather than raising, so one failure cannot unwind the others. Output is a
   `ContextBundle`: data, a failure map, and a stale set.
4. **Plan** (pure, config only) then **resolve** (against what actually arrived).
5. **Debug stops here.**
6. **Build the prompt** from selected items only, each through its registered
   renderer. Log prompt size.
7. **Generate**, **score confidence** deterministically, derive `sourcesUsed`
   from selection.

`/personalize` and `/debug/personalization` share one `_run_pipeline()`. Debug is
not a parallel implementation — it is the same code stopping early. A debug
endpoint that reimplemented the decision path would eventually describe
behaviour the live path no longer has, which is worse than having no debug
endpoint at all.

---

## The Personalization Engine

This is the graded core, so it gets the most space.

### Config is authored as tiers; boot compiles tiers to weights

An astrologer should be able to review *categories* — "is the 10th house primary
for career?" — without arguing about decimals. So `config/personalization.yaml`
is authored in exactly the assignment's own tier vocabulary:

```yaml
weights:
  primary: 1.0
  secondary: 0.5

rules:
  career:
    primary:   [house_10, horoscope_career]
    secondary: [dasha_current, panchang_today]
    exclude:   [horoscope_relationship]
```

At startup, `app/engine/compiler.py` flips this inside out into the shape the
runtime actually asks questions of:

```python
item_weights = {
    "house_10":         {career: 1.0, finance: 0.5},   # merged across intents
    "horoscope_career": {career: 1.0},
    "dasha_current":    {career: 0.5, relationship: 0.5, finance: 0.5},
    ...
}
exclusions = {
    career: frozenset({"horoscope_relationship"}),
    ...
}
```

Config is intent-keyed because that is the shape a human reviews. Runtime is
item-keyed because it walks candidate items asking "what is your weight under
the currently active intents?". Rather than make every request pay for that
mismatch, the flip happens once. The tunable numbers live in one `weights:`
block, so a global policy change is one edit rather than a find-and-replace
across every intent.

Exclusions compile to a **separate map**, not a negative weight. A subtractive
penalty can be outvoted by enough positive weight from a co-active intent; a
hard zero applied last cannot. "Do not discuss relationships" has to survive
contact with a 0.6 / 0.4 merge.

### The score formula

```
score(item) = Σ over active intents [ intent_weight × tier_weight(item, intent) ]
            + time_scope_modifier(item, scope)
            then hard-zeroed if excluded by ANY active intent   ← applied last
```

Sort descending (alphabetical tie-break, so a plan never reshuffles between
runs), then take while the token budget lasts.

**Single intent collapses back to exactly plain tiers.** One intent at weight
1.0 makes every score exactly 1.0 or 0.5 — item for item identical to tier
membership. This is asserted as a test, because if it ever drifts, the claim
below is false. Scoring is not a different model from tiers; it is tiers with
the merge arithmetic written down.

### Why scoring won

Treating the assignment's example table as *literal runtime behaviour* breaks on
three things the design was already committed to. Scoring dissolves all three at
once; tiers need a hand-written special case for each.

| Problem | With flat tier lists | With scoring |
|---|---|---|
| **Multi-intent merge** | Union the lists? What if career *excludes* what finance *includes*? | Weighted sum. An item secondary for *both* intents can legitimately out-rank one that is primary for the weaker intent alone. Verified: `dasha_current` at 0.5 beats `horoscope_finance` on a 0.6/0.4 career-finance merge. |
| **Time scope** | Panchang is decisive for "today" and actively misleading for "this year" — but it sits in one fixed tier. | An additive modifier, orthogonal to the tier. |
| **Budget ordering** | Which secondary drops first? Undefined. | The ranking already answers it. |
| **Backfill** | "Primary came back short, go fetch from secondary" — explicit logic, and that logic is where ordering bugs live. | Keep walking the ranked list. Nothing to write. |

Backfill is the argument that actually decided it. If `house_10` is unavailable
there is nothing to repair: the walk continues and a 0.5 item takes the place a
1.0 item could not.

### The Context Registry lives in code

Each of the twelve registry entries needs two *functions* — one to pull its
slice out of a service payload, one to render it as a short natural phrase — and
functions are logic. Config expresses policy; it must never express logic. So
the registry is a Python list in `app/engine/registry.py` and YAML references
entries by `key` alone.

```python
_item("house_10", "10th House", "kundli",
      _dict_field("houses", "10", requires=("lord", "strength")),
      _house_renderer("10"), _SAMPLE_HOUSE)
```

At boot the two halves are cross-validated: every key named in
`personalization.yaml` — in any tier, and in `time_scope_modifiers` — must exist
in the registry, or the process refuses to start with the offending key in the
message. This is the one bug class that is otherwise invisible: a rename in one
place and not the other does not raise, it silently drops a source.

Two consequences worth stating:

- **An extractor returning `None` is the entire mechanism for partial failure
  within a healthy service.** Kundli answers 200 but omits `houses.10`? The item
  is simply unavailable, decided by the same branch as a whole-service outage.
  No special case anywhere downstream.
- **Renderers emit a short natural phrase, never raw JSON.** Dumping
  `{"lord": "Moon", "strength": "Strong"}` at a model spends tokens on braces
  and quotes and reads worse in the output than `10th House: lord Moon, strength
  Strong`.

### Time scope as an orthogonal dimension

The spec maps intent to context. Intent alone is not enough: the same career
question wants different data depending on its horizon. Time scope is extracted
by its own signal (and its tokens are *consumed*, so "this year" cannot also
score as intent evidence) and applied as an additive modifier.

| Item | today | this_week | this_month | this_year |
|---|---|---|---|---|
| `panchang_today` | +0.5 | +0.1 | −0.3 | −0.4 |
| `dasha_current` | −0.2 | −0.1 | +0.2 | +0.4 |
| `horoscope_*` | +0.3 | +0.1 | −0.1 | −0.3 |

Today's tithi is the single most useful fact for "today" and actively misleading
for "this year" — by then it describes a day that has long passed. A mahadasha
is the mirror image: it runs for years, so it explains an annual arc and says
almost nothing about Tuesday.

Only items whose *value* changes with the horizon appear here. Natal data —
houses, lagna, moon sign, birth details — is fixed at birth, so a modifier on it
would be noise dressed as tuning. Modifiers are capped at ±0.5
(`modifier_cap`, enforced at boot) so a modifier may reorder items *within* a
tier but can never let a secondary out-rank a primary — that would be the tier
system voting against itself.

### Three distinct absence reasons

An item can be missing from the prompt for three completely different reasons,
and the spec names only one:

| Bucket | Meaning | Who decided |
|---|---|---|
| `excludedContext` | the rules actively judged it harmful | config |
| `unavailableContext` | an upstream let us down, or a slice was missing from a healthy response | reality |
| `droppedForBudget` | relevant and available, but it did not fit | the budget |

Collapsing these into one list throws away the interesting part. "We
deliberately kept relationship context out of a career prompt" and "kundli was
down" and "we ran out of tokens" are three different operational stories with
three different fixes. `/debug/personalization` reports all three separately;
`/personalize` keeps `excludedContext` as the spec asks.

**Exclusion is an answer-quality control; budget is a cost control.** They look
alike in config and do completely different jobs. Leaving the relationship
horoscope in a career prompt makes the model weave romance into an appraisal
question — that is not a cost problem.

Note what is *not* reported: an item that simply never scored for this intent.
Reporting that as "excluded" would make a deliberate decision indistinguishable
from an item that was never a candidate. The debug endpoint's `ranked` list
already shows every item with its score and reasons.

### The two phases

Splitting the engine in two is what makes it context-driven rather than a static
lookup.

1. **Planner — pure.** Sees intent, weights, time scope, the user profile and
   config. Sees *nothing* about which services succeeded. Produces the ranked
   candidate list plus language, tone and `maxWords`.
2. **Selector — resolved.** Walks the ranking against what actually arrived,
   spending the token budget.

Config decides what is *relevant*; resolution decides what is *possible*. If the
planner consumed availability, the same question would yield different plans on
different days and `/debug/personalization` would stop being reproducible — the
ranking would silently encode which upstream happened to be down when you ran
it.

### The non-context dimensions

- **`language`** comes from `user.language`, not from the question's language.
  Profile is consistent and honours an explicit setting; question-language is
  responsive but needs detection (a new failure mode) and can flip-flop between
  turns. The extension point is a `respond_in: profile | question | auto` knob.
  Moot in practice under the English-only cut — the field is read and threaded
  through the plan and the prompt because the spec asks for it as a
  personalization dimension, but it is effectively always `en`.
- **`tone`** is `user.tonePreference` with a **per-intent guardrail**. The same
  preference that is delightful on a career question is irresponsible on a
  health one: "motivational" must not become cheerleading about a Weak 6th
  house. Every *active* intent gets a veto, not just the primary — on a
  0.6 career / 0.4 health merge the answer still discusses health, so health's
  cap still binds. Question-sentiment adaptation ("I'm scared about my health")
  is the documented extension point; it needs sentiment detection and is out of
  scope.
- **`maxWords` = min(subscription cap, intent target, context-volume cap)**,
  floored. The third term is a **hallucination control, not a product setting**.
  Ask for 250 words when the fan-out yielded two thin facts and the model will
  not return 80 — it will invent 250 words' worth of astrology. Capping supply to
  the evidence is cheaper than detecting the invention afterwards. This is why
  the verified example above shows 180, not the premium tier's 250. An unknown
  subscription tier fails closed to the most restrictive configured cap:
  under-delivering to a paying user is a support ticket, over-delivering to a
  free one is a billing problem.

### Confidence and sourcesUsed

`confidence` is **deterministic, never the model's self-report** — models are
badly calibrated when asked how confident they are and will happily rate a
hallucination 9/10. It is derived from primary-source coverage (how many of the
top three ranked candidates reached the prompt) capped by the classifier's own
decision reason: `CONFIDENT_MATCH` can reach HIGH, `AMBIGUOUS_MERGED` and
`NO_EVIDENCE_DEFAULT` cap at MEDIUM, `OUT_OF_DOMAIN` is LOW. Perfect data
answering the wrong question is not a confident answer.

Astrological house strength was considered as an input and **rejected**. It
conflates two different things: confidence means "how well-grounded is this
response in the data we have", whereas house strength is a property of the
reading itself. "Your 7th house is weak", delivered from complete, fresh,
unambiguous data, is a HIGH confidence answer. Conflating them would make every
difficult reading look like a system failure, and users would learn to read the
confidence badge as a horoscope rather than as a quality signal.

`sourcesUsed` is derived from selection, never from the model's account of what
it used. Asking a model to cite invites hallucinated citations, and it is
unnecessary: what we sent is a fact we own. The list is true by construction.

### Extensibility test

- **A seventh intent**: one YAML block referencing existing registry keys, plus
  its lexicon. **Zero code.**
  ```yaml
  education:
    primary:   [house_10, horoscope_career]
    secondary: [dasha_current, moon_sign]
    exclude:   [horoscope_relationship]
  ```
  (The `Intent` enum is the one Python edit, because it is the type the whole
  pipeline is checked against.)
- **A new context source**: one registry entry — key, display name, service,
  extractor, renderer, sample — plus a reference from `personalization.yaml`.
  Boot cross-validation catches the half-done version.
- **A new upstream service**: a client subclass (a path and a shape check; every
  resilience behaviour is in the base class), one line in `CLIENT_TYPES`, one
  policy block in `services.yaml`.
- **A new personalization dimension**: one field on the plan plus one config
  table.

---

## Intent classification

The classifier accumulates weighted evidence into a score vector
(`dict[Intent, float]`) and then applies a **separate** decision policy to that
vector. The two halves never touch each other's concerns: a matcher cannot
decide, and the policy cannot look at text.

**The score vector is the extensibility seam**, and it is deliberately not
collapsed into "the winning term" even though exactly one signal source feeds it
today. An embedding classifier, a user-history prior, or a click-through signal
would each contribute into the same vector and change nothing downstream — the
policy, the weights contract, and every consumer stay as they are. Collapsing
the indirection would turn each of those from an addition into a rewrite. That
indirection *is* the deliverable.

### Two independent numbers

A single threshold would drift every time the lexicon grew, so the policy reads
two numbers that answer two different questions.

**`absolute_evidence` — did we match anything at all?**
Sum of the **max weight per matched term**, not the sum across all intents.

> The obvious alternative is to sum every intent's contribution. But `salary`
> scores 0.5 career *and* 0.8 finance, so it would contribute 1.3 to a gate that
> is only asking "did the user raise a topic we know about". That means the terms
> we trust *least* — the polysemous ones that fire for several intents — inflate
> the gate the *most*, which is backwards. Taking the max makes one match count
> once.

**`relative_dominance` — is one intent clearly winning?**
`top1 / (top1 + top2)`, not `top1 / sum(all)`.

> The share-of-total form conflates "clear winner" with "few competitors". A 4:1
> leader over three rivals scores only 0.667 and reads as ambiguous purely
> because there are more buckets to divide by — so every intent added would
> quietly re-tune a threshold nobody touched. The margin form only ever compares
> the two contenders and is stable as intents are added, which matters precisely
> because *adding intents is the extensibility story*.

Outcomes: `OUT_OF_DOMAIN` (refuse), `NO_EVIDENCE_DEFAULT` (→ `general`, a correct
default rather than an error), `CONFIDENT_MATCH` (single intent at weight 1.0),
`AMBIGUOUS_MERGED` (top two blended by their relative strength).

Verified live: *"Will I get a salary hike this year?"* → `AMBIGUOUS_MERGED`,
`{finance: 0.615, career: 0.385}`, dominance 0.615. A salary question is
legitimately both, and answering it as either alone drops half the context the
user needed.

### The in-domain gate

Runs **before** intent scoring, as a separate decision with its own evidence.
Without it there is no answer the pipeline can give that is not wrong: *"What is
the capital of France?"* matched the finance term `capital` and returned
**finance at maximum confidence**. Deleting `capital` from the lexicon does not
fix the class of bug, it moves it — with nothing matching, the fallback path
confidently generates an astrology reading for a geography question. Both
branches fail, because both assume the question belongs *somewhere* in the intent
simplex. The fix is a third outcome neither branch can express.

The gate counts distinct markers across three groups — personal markers (`my`,
`i`, `we`), astro terms (`kundli`, `dasha`, `panchang`, `forecast`), and zodiac
signs — and requires one. One, not two, because two would reject "kundli
reading?" — terse but plainly in domain — and a false reject (a refusal) costs
more than a false accept (a generic reading).

It deliberately does **not** reuse the intent lexicons. `cancer` is in-domain as
a sign and out of every lexicon as a disease, which is precisely the distinction
the gate exists to make. A boot-time linter enforces this: any zodiac sign
reachable from an intent lexicon — as a term, a stem, or a phrase token — kills
the process. Otherwise "What does my Cancer moon mean?" classifies as **health at
maximum confidence**, which for this product specifically is a real production
bug. The same hazard class covers `will`, `interest`, `bond`, `trust`, `house`,
`venus` and `capital`; each is simply absent from the lexicons.

`general` is likewise **not a scored intent** — it is the *absence* of one, a
label emitted only by the fallback path. As a scored bucket it let syntactic
noise (`focus`, `advice`, `summarize`) contaminate dominance, dragging a clean
health question from 1.00 to 0.80 and, with two such tokens, producing a
meaningless `AMBIGUOUS_MERGED (health + general)`. The config schema refuses to
load a `general` lexicon and the scoring loop skips it — two guards, because the
bug is silent.

### Negation is a discount, not a veto

A cue applies ×0.3 within a three-token window. It never zeroes.

1. **No fixed window is correct in general.** "not *really all that* worried
   about work" pushes the topic six tokens past the cue; widening the window to
   catch it starts swallowing the next clause. The window will therefore be
   wrong in both directions, and a discount caps the cost of being wrong either
   way — a mis-fire shaves a match instead of deleting it, and a miss leaves it
   slightly overweight instead of intact.
2. **Negation scopes over sentiment, not topic.** "Will I not lose my job?" is
   still a career question. Zeroing `job` there would answer it with generic
   filler. The user raised the subject either way, which is all the classifier is
   being asked to determine.

Relatedly, the evidence gate reads each match's *undiscounted* peak. Gating on
the discounted weight would recreate the exact failure the discount exists to
avoid: a question with one negated topical term would fall under `min_evidence`
and answer generically — boolean zeroing wearing a multiplier.

### Matching mechanics

- **Token-level, never substring.** `art` must not fire inside `heart`, and no
  amount of careful word choice makes substring matching safe once a lexicon is
  edited by non-engineers.
- **`*` marks a stem prefix** (`promot*` covers promotion/promoted/promotions).
  This avoids a stemmer dependency and its bugs. Stems are matched by scanning
  the *token's* prefixes, bounded by word length rather than lexicon size.
- **Longest-match-wins with consumption.** A matched phrase consumes its
  constituent tokens, so `love life` cannot also score bare `life`. Without this
  a `marri*` / `marriage` pair scored 2.3 instead of 1.2 — one word manufacturing
  a confident classification. A boot-time linter now rejects any lexicon where
  one entry is a prefix of another within the same intent.
- **Compiled to an inverted index at startup** — `token → [(intent, weight)]` —
  so classification costs O(tokens), not O(intents × terms). The alternative gets
  slower with exactly the change we most want to be cheap.

### The tokenizer contract is pinned first

Every other rule silently depends on it, because negation windows and phrase
consumption are measured in *these* token indices.

- **Contractions expand before tokenization.** A cue like `do not care about`
  can never match if `don't` is still one token at match time — and worse, every
  negation offset downstream would be measured against a token list that
  disagrees with the one the cues were written for. Apostrophe-free variants
  (`dont`, `ill`, `wont`) are deliberately *not* expanded: "ill" is a health word
  before it is "I will", and mis-expanding it would strip the strongest health
  signal in the lexicon.
- **Possessives are stripped**, so `today's` reaches the time-scope patterns as
  `today`. Without this, "Can you summarize today's guidance?" gets no horizon at
  all.
- **No orphan single-character tokens**, except `i` and `a` — `i` is the
  strongest personal marker the domain gate has.
- **Config entries go through the identical pipeline**, so a pattern written
  `this year's` cannot fail to match its own normalized token stream.

### Output

A rich object, not a label: primary intent, weights summing to 1.0, ranked
runners-up, **matched terms per intent**, time scope, the two numbers, and a
decision-reason enum. Two payoffs — a real reasoning trace for the debug
endpoint, and, in production, logged traces that bootstrap a labelled dataset for
a learned classifier without a separate annotation project.

---

## Assumptions

- **English input only.** Decided on measurement, not convenience — see *What was
  measured*. This deletes the Hinglish lexicon, transliteration, Devanagari
  tokenizer handling and the Hindi post-positional negation problem in one cut.
  It is an explicit scope boundary with evidence behind it, not an untested gap.
- **Upstream services are mocked locally.** Their payload shapes are taken
  verbatim from the assignment; the engine treats them as opaque upstream JSON.
- **The horoscope service has no date field**, so its content is assumed to be
  *daily*. The time-scope modifiers for `horoscope_*` are written on that
  assumption (positive for "today", negative for "this year").
- **"Next few months" maps to `this_month`.** A vague multi-month horizon is
  closer to a monthly transit read than to an annual dasha read.
- **Out-of-scope questions get a graceful refusal at LOW confidence**, not an
  answer, and never cost an LLM call.
- **Multi-intent questions are merged**, not forced into a single winner.
  "Will I get a salary hike?" is genuinely career *and* finance; picking one
  discards half the relevant context, and picking it by whichever hand-set
  weight happens to be larger is a coin flip dressed as a decision. Merging
  makes the ambiguity visible in `intentWeights` and lets confidence drop to
  reflect it.
- **Time scope is a second, orthogonal dimension** beyond the spec's intent-only
  mapping. Intent alone cannot distinguish "how is my career today" from "how is
  my career this year", yet they want different sources: today's panchang is
  decisive for one and noise for the other, while the dasha is the reverse.
  Folding the horizon into the intent table would mean an intent per
  intent-horizon pair, which multiplies the config instead of extending it.
- **No API key is assumed present.** The mock provider is the default and the app
  logs loudly at startup which provider it resolved, so a demo run is never
  ambiguously real-or-mocked.
- **Single instance, single worker.** The cache is per-process, so a second
  worker would make cache hits a coin flip and `DELETE /_cache` would clear only
  whichever worker served that request. Sidestepped rather than solved — see
  *Production concerns left out*.

Hand-set weights, the absent stemmer and the token-cost estimate are deliberate
simplifications rather than assumptions; each is stated with its cost under
*What was intentionally simplified*.

---

## Trade-offs

### Decisions taken

| Decision | Alternative | Why |
|---|---|---|
| Scored context selection | Flat tier lists as literal runtime behaviour | Multi-intent merge, time scope, budget ordering and backfill all fall out for free. Tiers need a hand-written special case for each. Single-intent still collapses to exactly tiers, so nothing is lost. |
| Config in tiers, compiled to weights at boot | Numeric weights in the file a domain expert edits | An astrologer reviews categories, not decimals. The numbers live in one block. |
| Registry in code, YAML references keys | Registry in YAML | Every entry needs an extractor and a renderer, which are functions. Logic in YAML builds a bad programming language with no type checker and no debugger. |
| Boot-time validation, fail-fast | Tolerate and log | A dangling key does not raise at runtime, it silently drops a source. Better a stack trace with a filename at deploy than a subtly wrong reading at 3am. |
| Rules-only classification | Embedding or LLM classifier | A deterministic path has to exist anyway as the fallback for timeouts and rate limits, so the rules classifier gets built either way — the only question is whether a second tier earns its cost on top. Measured: rules handle all five sample questions and the paraphrase case at zero latency; embeddings scored 16/20 and added a model download. Cut on the numbers, with the seam left in place. |
| Deterministic confidence | LLM self-reported confidence | Models are badly calibrated at self-reporting and will rate a hallucination 9/10. This is also reproducible without a network call, which is what lets the debug endpoint exist. |
| `sourcesUsed` from selection | Ask the model to cite | Asking a model to cite invites invented citations. What we sent is a fact we own. |
| Language from profile | Language from the question | Consistent and honours an explicit setting. Question-language needs detection (a new failure mode) and can flip-flop between turns. |
| Real mock services on a socket | In-process stubs | With monkeypatched stubs the HTTP client, the retry loop and the timeout never execute — the resilience layer would be untested code that happens to compile. |
| Per-service cache TTLs | One global TTL | Panchang is global and date-keyed, kundli is per-user and near-immutable, horoscope is per-user per-day, the profile changes whenever preferences do. One TTL would be wrong in four different ways. |
| Focused tests on selection and partial failure | Broad coverage | Those are the two places where a bug is both most likely and least visible — a wrong context set still produces a fluent answer, so nothing looks broken. |

### Rejected alternatives

**One LLM call doing classify + answer together.** Architecturally fatal. You
cannot select context before knowing the intent, so a single call forces sending
*all* context — directly violating the "optimize what you send instead of
sending everything" criterion. The two-phase split is what makes selection
possible at all.

**LLM-driven intent classification as the primary path.** A deterministic
fallback is needed for timeouts and rate limits regardless, so the rules
classifier gets built either way; the only question is whether the LLM layer
earns its cost on top. It does not on the happy path — all five of the spec's
sample questions classify correctly on rules alone, at zero latency and zero
cost.

**An LLM as the tier-2 escalation.** Embeddings beat it on determinism (same
input, same vector), latency (~10ms local vs 300–800ms), and auditability (cosine
scores you can print vs an opaque verdict). Auditability is decisive here,
because `/debug/personalization` exists specifically to explain reasoning.

**A vector DB of vocabulary embeddings.** The vocabulary-explosion problem comes
from indexing the wrong side. You embed the ~6 intents (roughly ten hand-written
exemplar questions each ≈ 60 vectors — a Python list), not every word a user
might type. No DB, no index, no ANN. A vector DB *would* be warranted for RAG
over shastra texts, per-user conversation memory, or a taxonomy of thousands of
intents; worth naming as plausible v2 work.

**Word-level embeddings.** Wrong granularity. They lose composition, so negation
("not worried about work, it's my health") looks nearly identical to the positive
case — and polysemy bites too (`job` sits near `task` and `chore`).

**DB-backed config.** Loses atomic deploy-with-code and boot-time validation: a
row can reference a context key the running build does not have. A DB *does* win
for non-engineer editing without a deploy, A/B testing weights, per-tenant
overrides and 30-second hot-patching — which is exactly why the `ConfigSource`
seam exists.

**A circuit breaker.** Deliberately omitted. At four upstreams and this traffic,
bounded retries plus a short timeout plus stale-on-error already bound the
damage, and single-flight already prevents the stampede a breaker would be
protecting against. A breaker would add state, a half-open policy and a tuning
surface for little gain. Named here so it reads as judgment rather than
oversight.

### What was intentionally simplified

- **No embedding / semantic tier.** Classification is rules-only. The *seam* is
  preserved and is the whole point (see *Intent classification*), and the
  decision was made on measurements, not assertion.
- **Multilingual input cut entirely — English only.** Not a scoping shrug: the
  embedding simulation showed romanized Hinglish fails *confidently*, matching
  sentence frame rather than meaning (0.71–0.87 similarity on the wrong intent).
  Shipping it half-working would be worse than not shipping it, because a
  confident misclassification silently selects the wrong context. **Cost:** a
  Hindi or Hinglish question is treated as noise and falls through to `general`.
- **No stemmer, no spell correction, no fuzzy matching.** A stemmer is a
  dependency with its own failure modes (over-stemming merges unrelated terms),
  and the `*` prefix marker in config covers the inflections that actually occur
  for ~15 terms. **Cost:** typos miss entirely — "chnage my job" scores nothing
  and lands in `general`. Fuzzy matching is an additive signal on the existing
  score vector when it is worth the false-positive risk.
- **Token costs are a `len // 4` character estimate**, not a real tokenizer.
  A real one means either a provider dependency at import time or shipping a
  vocabulary file, for a number that only orders a ranking. **Cost:** roughly
  ±25% error, so the budget binds a little early or late. It never affects
  *which* items rank above which, only where the cut falls.
- **Item token costs are computed once at import** from a representative sample
  rather than measured per request. This is what keeps phase-1 planning pure:
  if cost depended on the actual payload, the same question would produce
  different rankings on different days and the debug endpoint would stop being
  reproducible. **Cost:** an unusually long horoscope string is under-counted.
- **Weights and thresholds are hand-set with no feedback loop.** There is no
  labelled data to fit them to, and inventing a dataset to justify a number is
  worse than admitting the number is provisional. **Cost:** stated plainly in
  *Known limitations* — they are defensible, not tuned. The logged classification
  traces are the mechanism that would build the dataset in production.
- **Single-turn LLM interface** — no streaming, no tools, no chat history.
  The endpoint is request/response, so anything else is speculative surface.
  Keeping it this narrow is also why the mock provider is a faithful stand-in
  rather than an approximation. **Cost:** a 250-word answer has real perceived
  latency with no first-token feedback, and follow-up questions carry no memory
  of the previous turn.
- **Tests are focused, not broad.** Coverage percentage measures lines executed,
  not behaviours protected; the two suites here target the places where a bug
  survives review. **Cost:** the prompt builder and the LLM factory have no
  golden-output tests, so a regression in prompt composition would pass CI.

---

## What was measured, not assumed

Three validation exercises. Two of them changed the design; one of them killed a
feature that was already planned.

### 1. A paper trace of 11 questions, before implementation

Five of the spec's sample questions plus six adversarial ones, traced through the
classifier on paper before any code existed. Result: **5 clean passes, 3 fragile
passes, 3 failures.** The exercise's job was to find structural defects, and it
did. It produced, directly:

- **the out-of-domain gate** — "What is the capital of France?" returned finance
  at maximum confidence, and deleting the offending term only moved the bug;
- **the dominance formula change** — from share-of-total to margin form;
- **removing `general` as a scored intent** — it was contaminating dominance with
  syntactic noise, and removing it also deleted a whole lexicon section;
- **the evidence double-counting fix** — max weight per matched term, not sum
  across intents;
- **the tokenizer contract** — pinned first, because every other rule silently
  depends on it.

It also found the zodiac/health homonym collision (`cancer`), which is a real
production bug for this product specifically, and the observation that unbounded
recall debt is the *method*, not a bug: every paraphrase is a config entry,
forever, per language.

### 2. An embedding simulation — the tier that was cut on evidence

Not a thought experiment. A real run: `paraphrase-multilingual-MiniLM-L12-v2`,
32 exemplars across 4 intents, 20 test queries, max-similarity-per-intent
scoring. Result: **16/20**.

**English paraphrase worked, and fixed the exact rules-classifier failures.**
"Should I switch my career?" → career 0.727 with no `switch`/`career` keyword
pairing needed. "Should I hand in my notice?" → career 0.397. The negation case
("not worried about work, it's my health") → health 0.638, where rules were
knife-edge on window size. Out-of-domain was cleanly rejected — "capital of
France" 0.095, "reset my password" 0.212. Devanagari Hindi worked
(मेरी नौकरी कब लगेगी → career 0.763).

**Romanized Hinglish failed 4 of 5, and that is why the tier was cut.**

| Query | Model said | Should be |
|---|---|---|
| "Meri naukri kaisi rahegi?" | **health 0.872** | career |
| "meri tabiyat theek rahegi?" | **finance 0.859** | health |
| "mere rishte kaise rahenge?" | **finance 0.800** | relationship |
| "mujhe naya kaam milega kya?" | **relationship 0.714** | career |

The mechanism matters more than the score. **The model matches sentence *frame*,
not meaning.** "Meri naukri kaisi rahegi" matched the *health* exemplar "meri
sehat kaisi rahegi" at 0.872 — identical frame `meri X kaisi rahegi`, different
content word. The single Hinglish success was near-verbatim string overlap with
its exemplar, not comprehension. Romanized Hindi is out-of-distribution for the
model; all Latin-script Hindi looks alike to it. And the failures score **high
(0.71–0.87) with thin margins (0.06–0.12)** — confidently wrong, which is worse
than falling through.

That evidence produced two decisions:

- **The embedding tier is not built.** Rules already handle English paraphrase
  adequately once the lexicon carries paraphrase vocabulary, and this imposes no
  `torch` download on a reviewer. The seam stays.
- **Input is English-only**, stated as a measured boundary rather than an
  untested gap. Transliteration (romanized → Devanagari) before embedding is the
  only route that would make a semantic tier work for Hinglish; it is an extra
  dependency and an extra failure mode, and it is out of scope.

A useful byproduct: a three-band threshold structure emerged (`<0.25`
out-of-domain, `0.25–0.40` in-domain but general, `>0.40` confident intent), with
true-OOD topping out at 0.212 against an in-domain minimum of 0.397 — a healthy
gap, so the gate is viable whenever the tier is built.

### 3. A live end-to-end sweep

15 scenario groups run against the running stack — healthy path, each fault mode,
each criticality tier, cache-warm vs cache-cold, all three fixture users.
**13/13 assertions passed**, and it surfaced one real gap, now documented below
under *Known limitations* (the out-of-domain gate is lexical).

The focused unit suite passes in full (`make test`), covering the classifier
defects the paper trace found, the engine's selection logic, and the aggregator's
partial-failure behaviour.

---

## Known limitations

**The out-of-domain gate is lexical, and it admits some non-astrology
questions.** "How do I reset my password?" contains the personal marker `my`,
passes the gate, and receives a general reading. This is **structural, not a
tuning problem**: "What should I prioritize this week?" is lexically identical —
a personal marker, no astro term, no intent evidence — and it *must* stay in
domain, because it is one of the spec's own sample questions. Both classify as
`in_domain: true`, `NO_EVIDENCE_DEFAULT`, `absoluteEvidence: 0.0`. No threshold
over these features separates them. Separating them needs either an unbounded
negative vocabulary (every non-astrology topic a user might raise) or a semantic
tier, which is the measured-and-cut option above.

**The thresholds are provisional, not tuned.** `min_evidence = 0.8` and
`dominance = 0.65` are defensible starting values and nothing more. Across the
11-case paper trace, `min_evidence` changed the final intent **zero** times and
`dominance` was exercised by exactly **one** input; six cases sat at a degenerate
dominance of 1.00. Tuning them properly needs a few hundred labelled real
questions with the boundary region, negation and out-of-domain deliberately
oversampled — which we do not have and cannot fabricate. The logged
classification traces are the mechanism that would build that set in production.

**Phrases are not stem-aware, despite the `*` in the config.** The stem marker
works for single terms but not inside a phrase: `chang* my job` compiles to the
literal token sequence `("chang", "my", "job")`, so it fires on "chang my job"
and never on "changing my job". The example question still classifies correctly
via the bare `job` term at 1.2, which is precisely why the gap is easy to miss —
and it is the same "silent phrase miss" the paper trace flagged. It does bite
where no bare term backs the phrase up: `feel* unwell` never matches "feeling
unwell", so *"I am feeling unwell lately"* falls through to `general`. The fix is
to make phrase keys stem-aware in `compile_lexicons`; the config is already
written for it.

**Token costs are a `len // 4` estimate, not a real tokenizer** (±25%). Fine for
a guard rail, wrong for an accounting ledger. The production upgrade is the
provider's own tokenizer — Anthropic's count-tokens endpoint, `tiktoken` for
OpenAI — worth doing once budgets are enforced in money rather than in a config
constant.

**There is no global request deadline.** Total latency under a timing-out
upstream is bounded per service by `timeout × (retries + 1) + backoff` — about
**6.4s** at current settings. A misconfigured retry count in `services.yaml`
could exceed a client SLA without anything in the engine noticing. A wall-clock
budget for the whole fan-out is the right fix.

**Single worker; the in-memory cache is per-process.** See *Production concerns
left out*.

**Confidence samples the top three ranked candidates.** Under every intent in the
shipped config at least three items score positive, so an excluded (zero-scored)
item cannot enter that sample. A future intent with fewer than three positive
candidates would let a rule-excluded item into the coverage denominator and
depress confidence for a decision that was deliberate. Latent, not active.

---

## What I would improve with another day

Ordered by value.

1. **The embedding tier, behind the existing `Classifier` interface.** The seam
   is already there — it is one more signal extractor contributing into the same
   score vector, with the decision policy and every downstream consumer
   untouched. The simulation says it buys English paraphrase; the same simulation
   says to gate it off anything that looks like romanized Hinglish.
2. **A real tokenizer for budget accuracy.** Replaces the ±25% estimate, and
   removes the need for the representative samples used to cost registry items.
3. **A labelled question set, to actually tune the thresholds.** A few hundred
   real questions with the boundary region oversampled. Until that exists,
   `min_evidence` and `dominance` are honest guesses and are labelled as such.
4. **Golden-output tests on the prompt builder.** Selection regressions are the
   dangerous kind precisely because the answer still reads fine — a byte-exact
   snapshot of the assembled prompt for a fixed question is what catches "the
   10th house quietly stopped being included".
5. **Stem-aware phrase matching**, closing the gap named above.
6. **Streaming responses**, so a 250-word reading does not arrive as one silent
   pause.

---

## Production concerns left out

- **Single instance is a single point of failure.** The fix is several instances
  behind a load balancer — but that requires **Redis first**, because the cache
  is an in-memory dict. With multiple workers today, cache hits become
  nondeterministic and `DELETE /_cache` clears only the worker that served the
  request. The `TTLCache` interface is what a Redis implementation would sit
  behind.
- **No circuit breaker** — deliberate, not an oversight. Retries, timeouts and
  graceful degradation cover this scale, and single-flight already prevents the
  stampede. Reasoning is under *Rejected alternatives*.
- **No authentication, rate limiting or per-user quota.** `/personalize` is open,
  and it costs money per call.
- **No metrics and no distributed tracing.** There are structured JSON logs with
  a correlation id, per-service outcome and latency, and prompt size — enough to
  diagnose a single request, not enough to see a trend. Prometheus counters and
  OpenTelemetry spans are the obvious next layer.
- **No prompt-injection defence on the user's question.** The question is
  interpolated into the user message. The system prompt's grounding rules make
  the model resistant to inventing *astrological* detail, but nothing stops a
  question from containing instructions.
- **No LLM cost controls.** No per-user budget, no spend cap, no daily ceiling.
  The context token budget bounds the *input* side only.
- **Config reload requires a restart.** The `ConfigSource` seam exists for a DB
  or config-service loader; hot reload is not built. A running instance cannot be
  re-tuned without a deploy.
- **`DELETE /_cache` is gated behind `ENABLE_ADMIN_ENDPOINTS`** and registered
  conditionally, so it is absent from the route table and the OpenAPI schema when
  disabled. Exposed publicly it is a free denial of service: every request after
  a flush stampedes the upstreams at once.
- **No graceful shutdown drain.** In-flight requests are not waited on.

---

## Project layout

```
.
├── app/                       the deliverable
│   ├── main.py                wiring + lifespan: parse, validate, compile, once
│   ├── domain.py              shared vocabulary every layer speaks; imports no layer
│   ├── config.py              deployment settings from env/.env (URLs, keys, budget)
│   ├── config_schema.py       typed shapes for config/*.yaml, validated at startup
│   ├── config_loader.py       ConfigSource seam + the boot-time linters
│   ├── confidence.py          deterministic confidence + sourcesUsed derivation
│   ├── api/                   wire contracts and the two endpoints; one shared pipeline
│   ├── middleware/            request-id ContextVar, JSON formatter, latency logging
│   ├── classifier/            tokenizer → domain gate → signals → rules (decision policy)
│   ├── engine/                registry (code) · compiler (boot) · planner (pure) · selector
│   ├── prompt/                assembles system + user strings; logs prompt size
│   ├── llm/                   LLMClient seam + anthropic / openai / deterministic mock
│   └── services/              UpstreamClient base (timeout·retry·cache) + 4 clients + fan-out
├── config/                    the behavioural config a domain expert edits
│   ├── intents.yaml           lexicon, phrases, domain gate, time scope, thresholds
│   ├── personalization.yaml   tiers per intent, modifiers, tone caps, length caps
│   └── services.yaml          per-service timeout, retries, TTL, criticality
├── mock_services/             scaffolding: stand-in backends + runtime fault injection
├── tests/                     focused on selection logic and partial-failure behaviour
├── ARCHITECTURE.md            diagrams, boot sequence, failure paths, extension seams
├── docker-compose.yml         engine :8000 + mocks :8001, single worker each
├── Dockerfile                 one image, two roles
└── Makefile                   run, ask, debug, and the three fault demos
```

---

## Testing

```bash
make test                    # or: ./.venv/bin/python -m pytest -q
```

Tests load the **real** `config/*.yaml` rather than hand-built fixtures. A test
that passes against a fabricated config proves the code works on a config nobody
ships; loading the real one also means a bad edit to the YAML fails the suite.

Coverage is deliberately focused on the two places where bugs are both most
likely and least visible.

| File | Targets | Why here |
|---|---|---|
| `tests/test_classifier.py` | the spec's five sample questions; the out-of-domain hole; the `cancer` homonym; substring matching; paraphrase; negation shifting topic *and* negation not suppressing the only topic; genuine career/finance ambiguity; the margin-form dominance; degenerate input; the debug trace | Every case is a regression test for a specific defect the paper trace found — not coverage for its own sake. |
| `tests/test_engine_selection.py` | single-intent collapsing to plain tiers; exclusion as a hard zero; multi-intent reinforcing shared context; time scope reordering; planner purity; tone guardrails; subscription and context-volume length caps; a missing slice in a healthy service; backfill; the budget not stopping at the first item that does not fit; the three absence reasons staying distinct | Selection is where a wrong answer still *reads* correct. A bad context set produces a fluent response, so nothing looks broken — the test is the only thing that notices. |
| `tests/test_aggregator_failures.py` | fan-out is concurrent not serial; degradable failure still yields a bundle; required failure is typed; a 404 carries its status so the API answers 404 not 503; 4xx is not retried but 5xx is; a malformed 200 is a failure not an exception; stale cache served on upstream death; single-flight collapsing concurrent misses; the cache preventing a second call | Every one of these is a behaviour that a `try/except` around an HTTP call would appear to handle and would not. |
