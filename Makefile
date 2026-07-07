# RoboTrader — all entry points. Run `make` for help.
# Safety notes:
#   * `paper` is the default trading target; `live` is deliberately guarded.
#   * `live` runs the tests first and the engine still requires the typed
#     confirmation phrase — the Makefile guard is a speed bump, not the lock.

VENV    := .venv
PY      := $(VENV)/bin/python
PIP     := $(PY) -m pip
YEAR    ?= $(shell date +%Y)
GUI_DIR := gui/web

.DEFAULT_GOAL := help
.PHONY: help install keys-paper keys-live test backtest paper live gui gui-build kill tax clean \
        docker-build docker-up docker-down docker-logs docker-live docker-kill deploy

help: ## Show this help
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  \033[1m%-12s\033[0m %s\n", $$1, $$2}'

$(VENV)/bin/activate: requirements.txt
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	@touch $(VENV)/bin/activate

install: $(VENV)/bin/activate ## Create .venv and install dependencies

keys-paper: install ## Store Alpaca PAPER keys in the OS keychain (prompts, no echo)
	$(PY) -m scripts.store_keys --mode paper

keys-live: install ## Store Alpaca LIVE keys (only after the paper->live gate passes)
	$(PY) -m scripts.store_keys --mode live

test: install ## Run the test suite
	$(PY) -m pytest tests/ -v

backtest: install ## Gate-qualifying backtest; writes journal/backtests/<run_id>/
	$(PY) -m scripts.run_backtest

paper: install ## Start the engine daemon in PAPER mode (the default mode)
	$(PY) -m service.engine --config config/paper.yaml

live: test ## Start the engine in LIVE mode (tests must pass; engine prompts for the phrase)
	@echo ""
	@echo "  *** LIVE MODE — REAL MONEY ***"
	@echo "  Confirm docs/GATES.md Gate 2 is fully checked off before continuing."
	@echo ""
	$(PY) -m service.engine --config config/live.yaml

gui: ## Start the dashboard dev server on :5173 (engine must be running)
	cd $(GUI_DIR) && npm run dev

gui-build: ## Build the dashboard; the engine then serves it at :8765
	cd $(GUI_DIR) && npm run build

kill: install ## KILL SWITCH: cancel all orders, flatten all positions, halt engine
	$(PY) -m scripts.kill --reason "manual via make kill"

tax: install ## Export realized gains/losses CSV, e.g. `make tax YEAR=2026`
	@mkdir -p exports
	$(PY) -m scripts.export_tax_csv --year $(YEAR) --out exports/8949_$(YEAR).csv

clean: ## Remove venv and caches (never touches journal/ or logs/)
	rm -rf $(VENV) .pytest_cache
	find . -type d -name __pycache__ -not -path './.git/*' -exec rm -rf {} +

## --- Docker deployment (see docs/DEPLOY.md) ---

docker-build: ## Build the Docker image (Python engine only — see gui-build for the dashboard)
	docker compose build

docker-up: docker-build ## Start the engine in PAPER mode, detached, restart-on-failure
	docker compose up -d

docker-down: ## Stop the containerized engine
	docker compose down

docker-logs: ## Tail the containerized engine's logs
	docker compose logs -f

docker-live: docker-build ## Start the engine in LIVE mode, attached (must type the confirmation phrase)
	docker compose run --rm engine python -m service.engine --config config/live.yaml

docker-kill: ## KILL SWITCH against the running container: cancel orders, flatten positions, halt
	docker compose exec engine python -m scripts.kill --reason "manual via make docker-kill"

deploy: ## Push + build GUI + sync + rebuild/restart on the droplet (deploy/push-update.sh)
	deploy/push-update.sh
