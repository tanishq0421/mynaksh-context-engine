# Prefer the project venv when it exists: bare `python` does not exist on macOS
# and would bypass the venv anyway, so `make test` would fail or silently run
# against the system interpreter.
PYTHON ?= $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)

ENGINE ?= http://localhost:8000
MOCKS  ?= http://localhost:8001
# NOT `USER`: that is a standard environment variable holding the login name,
# and make imports the environment, so `USER ?= ...` would never apply and every
# demo would ask about a user that does not exist.
USER_ID ?= user_101
Q      ?= Should I consider changing my job in the next few months?

# Fault demo knobs. The two failure stories differ by ORDERING, not a flag:
#   demo-degraded -> clear cache, then fault  => the answer degrades
#   demo-stale    -> fault after warming      => stale-on-error still answers
# That sequencing lives here rather than in either service, so the mock never
# has to reach into the engine's API to clear its cache.
SERVICE     ?= kundli
FAULT       ?= error

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
	$(PYTHON) -m pytest -q

ask:  ## Ask a question. make ask USER_ID=user_102 Q="..."
	@curl -sS -X POST $(ENGINE)/personalize \
		-H 'Content-Type: application/json' \
		-d '{"userId":"$(USER_ID)","question":"$(Q)"}' | python3 -m json.tool

debug:  ## Show how the engine interpreted the question (no LLM call)
	@curl -sS -X POST $(ENGINE)/debug/personalization \
		-H 'Content-Type: application/json' \
		-d '{"userId":"$(USER_ID)","question":"$(Q)"}' | python3 -m json.tool

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
