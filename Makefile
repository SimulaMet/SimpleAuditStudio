# SimpleAudit Studio — common development tasks.
#
# Local (non-Docker) development uses the venv at .venv and the .env file
# (loaded automatically by manage.py). Docker targets use docker compose.

VENV := .venv
PY := $(VENV)/bin/python
DJANGO := $(PY) manage.py

.PHONY: help venv local-setup local-web local-worker docker-up docker-down \
        docker-logs docker-build test test-smoke migrate worker e2e seed seed-demo

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

venv: ## Create the local virtualenv and install dependencies
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -r requirements.txt

local-setup: ## Local dev: venv + .env + migrations + bootstrap + seed (idempotent)
	@test -d $(VENV) || $(MAKE) venv
	@test -f .env || cp .env.local.example .env
	$(DJANGO) migrate
	$(DJANGO) bootstrap_platform
	$(DJANGO) seed_platform

local-web: ## Run the web server locally (http://localhost:8000)
	$(DJANGO) runserver

local-worker: ## Run a Hatchet worker locally (needs Postgres + Hatchet up)
	$(DJANGO) run_worker --pool cpu

docker-up: ## Build and start the full Docker Compose stack
	docker compose up -d --build

docker-down: ## Stop the Docker Compose stack
	docker compose down

docker-logs: ## Tail logs from the Docker Compose stack
	docker compose logs -f --tail=100

docker-build: ## Rebuild images (run after template/static changes)
	docker compose build web worker

test: ## Run the full test suite on SQLite (fast, no external deps)
	SIMPLEAUDIT_LOCAL_SQLITE=1 $(DJANGO) test infra

test-smoke: ## Run only the all-pages smoke test
	SIMPLEAUDIT_LOCAL_SQLITE=1 $(DJANGO) test infra.tests.test_smoke_all_pages

migrate: ## Apply database migrations
	$(DJANGO) migrate

seed: ## Seed scenario packs + default model connections (idempotent)
	$(DJANGO) seed_platform

seed-demo: ## Seed demo audit runs with real model execution (needs SIMULACHAT_API_KEY)
	$(DJANGO) seed_demo_audits

worker: ## Alias for local-worker
	$(MAKE) local-worker

e2e: ## Run the API E2E smoke driver against a running stack
	$(PY) deploy/e2e_smoke.py http://localhost:8000
