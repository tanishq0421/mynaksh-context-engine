ENGINE ?= http://localhost:8000
MOCKS  ?= http://localhost:8001
USER   ?= user_101
Q      ?= Should I consider changing my job in the next few months?

# Fault demo knobs. CLEAR_CACHE distinguishes the two failure stories:
#   true  -> cold cache: the answer degrades (fewer sources, lower confidence)
#   false -> warm cache: stale-on-error serves last-known-good
# Sequencing lives here rather than in either service, so the mock never has to
# reach into the engine's API to clear its cache.
SERVICE     ?= kundli
FAULT       ?= error
CLEAR_CACHE ?= true

.PHONY: help up down logs test ask debug demo-healthy demo-degraded demo-stale demo-reset

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

up:  ## Build and start the engine + mock services
	docker compose up --build -d
	@echo "engine $(ENGINE)  mocks $(MOCKS)"

down:  ## Stop everything
	docker compose down

logs:  ## Tail engine logs
	docker compose logs -f engine

test:  ## Run the test suite locally
	python -m pytest -q

ask:  ## Ask a question. USER=user_102 Q="..." make ask
	@curl -sS -X POST $(ENGINE)/personalize \
		-H 'Content-Type: application/json' \
		-d '{"userId":"$(USER)","question":"$(Q)"}' | python3 -m json.tool

debug:  ## Show how the engine interpreted the question (no LLM call)
	@curl -sS -X POST $(ENGINE)/debug/personalization \
		-H 'Content-Type: application/json' \
		-d '{"userId":"$(USER)","question":"$(Q)"}' | python3 -m json.tool

demo-healthy: demo-reset  ## Baseline: all four services up
	@$(MAKE) --no-print-directory ask

demo-degraded:  ## Cold cache + failed service -> fewer sources, lower confidence
	@curl -sS -X DELETE $(ENGINE)/_cache > /dev/null
	@curl -sS -X POST $(MOCKS)/_faults -H 'Content-Type: application/json' \
		-d '{"$(SERVICE)":"$(FAULT)"}' > /dev/null
	@echo "== $(SERVICE) faulted ($(FAULT)), cache cleared =="
	@$(MAKE) --no-print-directory ask

demo-stale:  ## Warm cache + failed service -> stale-on-error still answers fully
	@curl -sS -X DELETE $(ENGINE)/_cache > /dev/null
	@curl -sS -X DELETE $(MOCKS)/_faults > /dev/null
	@$(MAKE) --no-print-directory ask > /dev/null   # warm the cache first
	@curl -sS -X POST $(MOCKS)/_faults -H 'Content-Type: application/json' \
		-d '{"$(SERVICE)":"$(FAULT)"}' > /dev/null
	@echo "== $(SERVICE) faulted ($(FAULT)), cache left warm =="
	@$(MAKE) --no-print-directory ask

demo-reset:  ## Clear all faults and the cache
	@curl -sS -X DELETE $(MOCKS)/_faults > /dev/null || true
	@curl -sS -X DELETE $(ENGINE)/_cache > /dev/null || true
	@echo "== faults and cache cleared =="
