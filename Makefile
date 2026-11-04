# Convenience targets for local dev. Run `make help` to list.
.DEFAULT_GOAL := help
# VENV := .venv
# PY := $(VENV)/bin/python
# PIP := $(VENV)/bin/pip

.PHONY: help venv install infra-up infra-down up down migrate test test-be test-fe fe-dev clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# venv: ## Create the Python virtualenv
# 	python3 -m venv $(VENV)

# install: venv ## Install all Python packages + services (editable) and frontend deps
# 	$(PIP) install --upgrade pip
# 	$(PIP) install -e ./packages/common -e ./packages/db \
# 	  -e "./services/gateway[test]" \
# 	  -e "./services/orchestrator[test]" \
# 	  -e "./services/ingestion-worker[test]" \
# 	  -e "./services/agent-runtime[test]" email-validator
# 	cd frontend && npm install

install: ## Install python packages via uv workspace + frontend deps
	uv sync --all-packages
	cd frontend && npm install

infra-up: ## Start only Postgres + RabbitMQ
	docker compose -f deploy/docker-compose.yml --env-file .env up -d postgres rabbitmq

infra-down: ## Stop Postgres + RabbitMQ
	docker compose -f deploy/docker-compose.yml --env-file .env down

up: ## Build and start the whole stack in Docker
	docker compose -f deploy/docker-compose.yml --env-file .env up -d --build

down: ## Stop the whole stack
	docker compose -f deploy/docker-compose.yml --env-file .env down

migrate: ## Run DB migrations against the local Postgres (needs DATABASE_URL on localhost)
# 	cd packages/db && DATABASE_URL=postgresql+asyncpg://agent:agent@localhost:5432/agentstudio \
# 	  ../../$(PY) -m alembic upgrade head
	cd packages/db && DATABASE_URL=postgresql+asyncpg://agent:agent@localhost:5432/agentstudio \
	  uv run alembic upgrade head

test: test-be test-fe ## Run all tests

test-be: ## Run all backend test suites
# 	@for s in gateway orchestrator ingestion-worker agent-runtime; do \
# 	  echo "== $$s =="; (cd services/$$s && ../../$(PY) -m pytest -q) || exit 1; \
# 	done
	uv run pytest -q

test-fe: ## Run frontend tests
	cd frontend && npm test

fe-dev: ## Run the Next.js dev server
	cd frontend && npm run dev

clean: ## Remove venv and build artifacts
# 	rm -rf $(VENV) frontend/.next frontend/node_modules
	rm -rf .venv frontend/.next frontend/node_modules
