# Makefile — developer entrypoints for the annotation platform.
#
# Backend commands prefer the local venv (backend/.venv/bin/...) when
# present, falling back to running the same command inside the `backend`
# compose service so `make test`/`make lint`/etc. work with zero local
# Python setup.

SHELL := /bin/bash
.DEFAULT_GOAL := help

COMPOSE := docker compose
# The running stack. After `make dev-licence` it also trusts a throwaway key and
# runs with a Business licence (dev only; .dev/ is gitignored, delete it to stop).
STACK := $(COMPOSE)$(if $(wildcard .dev/compose.licence.yml), -f docker-compose.yml -f .dev/compose.licence.yml)

# Demo superuser created by `make seed` / `make reset`. Dev-only defaults;
# override on the command line: make reset email=me@x.io password=secret
email ?= admin@example.com
password ?= admin-dev-password

# `make loadtest` workload profile (loadtest/lib/config.js).
PROFILE ?= default

BACKEND_VENV := backend/.venv/bin
BACKEND_PY   := $(if $(wildcard $(BACKEND_VENV)/python),$(BACKEND_VENV)/python,$(COMPOSE) run --rm backend python)
BACKEND_RUFF := $(if $(wildcard $(BACKEND_VENV)/ruff),$(BACKEND_VENV)/ruff,$(COMPOSE) run --rm backend ruff)
BACKEND_MYPY := $(if $(wildcard $(BACKEND_VENV)/mypy),$(BACKEND_VENV)/mypy,$(COMPOSE) run --rm backend mypy)
BACKEND_PYTEST := $(if $(wildcard $(BACKEND_VENV)/pytest),$(BACKEND_VENV)/pytest,$(COMPOSE) run --rm backend pytest)
BACKEND_ALEMBIC := $(if $(wildcard $(BACKEND_VENV)/alembic),$(BACKEND_VENV)/alembic,$(COMPOSE) run --rm backend alembic)

# ruff, mypy and pytest all read their configuration from
# backend/pyproject.toml and address `app` and `tests` relatively, so they
# have to run with backend/ as the working directory. The compose fallback
# already starts in /app; the local venv does not, hence the `cd`.
BACKEND_LOCAL := $(wildcard $(BACKEND_VENV)/python)
backend_cmd = $(if $(BACKEND_LOCAL),cd backend && .venv/bin/$(1),$(COMPOSE) run --rm backend $(1))

.PHONY: help
help: ## Show this help
	@echo "Available targets:"
	@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z_-]+:.*##/ { printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

.PHONY: install
install: ## Install backend, SDK and model service (dev extras) and frontend dependencies locally
	python3 -m venv backend/.venv
	backend/.venv/bin/pip install --upgrade pip
	backend/.venv/bin/pip install -e "backend[dev,otel]"
	python3 -m venv sdk/.venv
	sdk/.venv/bin/pip install -e "sdk[dev]"
	python3 -m venv model-service/.venv
	model-service/.venv/bin/pip install -e "model-service[dev,llm]"
	cd frontend && npm ci

.PHONY: dev
dev: ## Start the full stack (postgres, redis, backend, worker, frontend)
	$(STACK) up --build

.PHONY: dev-licence
dev-licence: ## Dev/CI only: a throwaway key and a Business licence the stack trusts (.dev/); then make dev
	mkdir -p .dev
	$(if $(BACKEND_LOCAL),cd backend && .venv/bin/python -m app.services.licensing.devlicence --out ../.dev,$(COMPOSE) run --rm --no-deps --user "$$(id -u):$$(id -g)" -v "$$PWD/.dev:/out" backend python -m app.services.licensing.devlicence --out /out)

.PHONY: down
down: ## Stop the stack and remove containers
	$(COMPOSE) down

.PHONY: seed
seed: ## Seed the demo project (sample photos, connector, schema, superuser); override email=/password=
	$(STACK) run --rm backend python -m app.demo seed \
		--admin-email "$(email)" --admin-password "$(password)"

.PHONY: reset
reset: ## Start over: drop all data volumes, rebuild, start the stack in the background, and seed
	$(COMPOSE) down -v
	$(STACK) up -d --build --wait
	$(MAKE) seed email="$(email)" password="$(password)"
	@echo
	@echo "Fresh stack is up. Log in at http://localhost:5173/ as $(email) / $(password)"

.PHONY: logs
logs: ## Tail logs for all services
	$(COMPOSE) logs -f

.PHONY: migrate
migrate: ## Apply database migrations (alembic upgrade head)
	$(BACKEND_ALEMBIC) upgrade head

.PHONY: revision
revision: ## Create a new alembic revision: make revision m="add foo"
	$(BACKEND_ALEMBIC) revision --autogenerate -m "$(m)"

.PHONY: test
test: ## Run backend and frontend test suites
	$(call backend_cmd,pytest --cov=app --cov-report=term-missing)
	cd frontend && npm run test

.PHONY: e2e
e2e: ## Playwright end-to-end tests against the running, seeded stack (see frontend/e2e/README.md)
	cd frontend && npx playwright test

.PHONY: loadtest
loadtest: ## k6 load test against the running, seeded stack (loadtest/README.md); PROFILE=smoke|default|stress, K6_ARGS="-e ANNOTATORS=80"
	$(COMPOSE) --profile loadtest run --rm -e PROFILE=$(PROFILE) -e EMAIL="$(email)" -e PASSWORD="$(password)" k6 run $(K6_ARGS) /scripts/annotate.js

.PHONY: mlflow
mlflow: ## Local MLflow server for the ML platform integration (API-6) on :5001; rebuild backend + worker with BACKEND_EXTRAS=mlflow
	$(COMPOSE) --profile mlflow up -d mlflow

.PHONY: emulators
emulators: ## AWS (S3 + Secrets Manager via Moto) and GCS emulators plus a webhook echo receiver, next to Azurite (docs/LOCAL_CLOUDS.md)
	$(COMPOSE) --profile emulators up -d --wait azurite s3 gcs webhook-sink mailpit

.PHONY: seed-emulators
seed-emulators: emulators ## Demo projects on the S3 and GCS emulators and a webhook to the echo receiver; run after `make seed`
	$(COMPOSE) run --rm backend python -m app.demo seed-emulators

.PHONY: integration
integration: emulators ## Connector, secrets and webhook tests against the local cloud emulators (needs `make install`)
	@test -x backend/.venv/bin/pytest || { echo "make integration needs the local venv: run make install"; exit 1; }
	cd backend && RUN_INTEGRATION=1 .venv/bin/pytest tests/test_integration_emulators.py

.PHONY: lint
lint: ## Lint backend (ruff) and frontend (eslint)
	$(call backend_cmd,ruff check .)
	cd frontend && npm run lint

.PHONY: format
format: ## Format backend (ruff format) and frontend sources
	$(call backend_cmd,ruff format .)

.PHONY: openapi
openapi: ## Regenerate docs/openapi.json and the SDK models generated from it (API-3)
	@# Local venvs only: inside the compose image the file would land in the container.
	@test -x backend/.venv/bin/python -a -x sdk/.venv/bin/python || { echo "make openapi needs the local venvs: run make install"; exit 1; }
	cd backend && .venv/bin/python -m app.openapi_dump
	cd sdk && .venv/bin/python tools/generate_models.py

.PHONY: typecheck
typecheck: ## Type-check backend (mypy) and frontend (tsc)
	$(call backend_cmd,mypy app)
	cd frontend && npm run typecheck

.PHONY: build
build: ## Build all container images
	$(COMPOSE) build

.PHONY: clean
clean: ## Remove containers, volumes, and local build/cache artifacts
	$(COMPOSE) down -v
	rm -rf backend/.venv frontend/node_modules frontend/dist
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
