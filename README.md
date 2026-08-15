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

Also: `make ask`, `make debug`, `make demo-degraded`, `make demo-stale`, `make test`.

---

## Behaviour, verified live

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

Config is what a domain expert reads:

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
- **`maxWords = min(subscription, intent, context volume)`.** The third is a
  hallucination control: ask for 250 words when two thin facts survived and the
  model invents the rest.
- **Adding an intent is one YAML block, zero code.** A dangling context key
  fails at startup, not at 3am.

---

## Failure handling, verified

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
| Tiers in config, weights at boot | Numbers in the file | An astrologer reviews categories, not decimals |
| Registry in code | Registry in YAML | Extractors and renderers are functions; logic in YAML is a language with no type checker |
| Rules-only classification | Embedding / LLM classifier | A deterministic fallback is needed anyway. Measured: rules handle every sample question at zero latency; embeddings scored 16/20 and add a model download |
| Deterministic confidence | Ask the model | Models are badly calibrated at self-reporting |
| `sourcesUsed` from selection | Ask the model to cite | Asking a model to cite invites invented citations |
| Mock services on a socket | In-process stubs | Otherwise the retry loop and timeouts are code that compiles but never runs |

**Rejected:**

- **One LLM call doing classify + answer.** Fatal: you cannot select context
  before knowing intent, so it forces sending everything.
- **A vector DB of vocabulary.** You embed the six intents (~60 vectors, a
  Python list), not every word a user might type.
- **DB-backed config.** Loses atomic deploy-with-code and boot validation; a row
  can reference a key the running build lacks.
- **Circuit breaker.** Retries + timeouts + stale-on-error already bound the
  damage at four upstreams; single-flight prevents the stampede it would guard.

## Known limitations

- **The out-of-domain gate is lexical.** "How do I reset my password?" contains
  "my" and passes. Structural, not tunable: "What should I prioritize this week?"
  is lexically identical and must stay in-domain.
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

## Layout

```
app/
  classifier/   tokenizer, signals, domain gate, rules   → intent + time scope
  engine/       registry, compiler, planner, selector    → what context, and why
  services/     base client, cache, 4 clients, aggregator → concurrent fan-out
  prompt/       builder                                   → selected context only
  llm/          base, factory, anthropic, openai, mock    → swappable provider
  api/          router, request/response                  → the two endpoints
config/         intents · personalization · services      → behaviour, as data
mock_services/  the four upstreams + fault injection      → scaffolding
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
