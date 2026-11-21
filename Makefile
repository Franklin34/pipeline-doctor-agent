# Compatible with GNU Make 3.81 (macOS default).
GO_MODULES := services/ingress mcp-servers/jenkins
PY_SRC := agent/src mcp-servers/github/src mcp-servers/jira/src rag/src sandbox/src evals/src

.PHONY: help setup fmt lint typecheck test test-pg test-go eval eval-live demo tf-validate check up down ingest
.DEFAULT_GOAL := help

help: ## Show targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

setup: ## Install Python deps and git hooks
	uv sync --all-packages
	@command -v pre-commit >/dev/null && pre-commit install || echo "pre-commit not installed; skipping hooks"

fmt: ## Format Python and Go
	uv run ruff format .
	@for m in $(GO_MODULES); do (cd $$m && gofmt -w .); done

lint: ## Lint Python; check Go formatting and vet
	uv run ruff check .
	uv run ruff format --check .
	@for m in $(GO_MODULES); do (cd $$m && test -z "$$(gofmt -l .)" && go vet ./...) || exit 1; done

typecheck: ## mypy (strict)
	uv run mypy $(PY_SRC)

test: ## Python tests (set PD_SKIP_DOCKER_TESTS=1 to skip the real-Docker ones)
	uv run pytest

PG_NAME := pd-pgvector-test
PG_URL := postgresql://postgres:pdtest@127.0.0.1:55432/pdtest

test-pg: ## RAG tests on a throwaway pgvector container (needs Docker; removed afterwards)
	@docker rm -f $(PG_NAME) >/dev/null 2>&1 || true
	docker run -d --name $(PG_NAME) -e POSTGRES_PASSWORD=pdtest -e POSTGRES_DB=pdtest -p 127.0.0.1:55432:5432 pgvector/pgvector:pg16 >/dev/null
	@until docker exec $(PG_NAME) pg_isready -U postgres -d pdtest >/dev/null 2>&1; do sleep 1; done
	@PD_TEST_DATABASE_URL=$(PG_URL) uv run pytest rag agent/tests/test_knowledge.py; status=$$?; docker rm -f $(PG_NAME) >/dev/null; exit $$status

ingest: ## Index knowledge/ into the database in DATABASE_URL
	uv run python -m pipeline_doctor_agent.knowledge ingest --dir knowledge

test-go: ## Go tests with the race detector
	@for m in $(GO_MODULES); do (cd $$m && go test -race ./...) || exit 1; done

eval: ## Golden dataset with the scripted model (free, deterministic); fails on a safety miss or regression
	uv run python -m pd_evals

eval-live: ## Golden dataset with the REAL model (needs credentials; costs money)
	uv run python -m pd_evals --live

demo: ## Narrated offline run of the real pipeline (scripted model; no keys, no network)
	uv run python -m pd_evals.demo

tf-validate: ## terraform fmt + validate (no cloud access, no state)
	cd deploy/terraform && terraform fmt -check -recursive && terraform init -backend=false -input=false >/dev/null && terraform validate

check: lint typecheck test test-go ## Everything CI runs
	$(MAKE) eval

COMPOSE := docker compose --env-file .env -f deploy/compose/docker-compose.yml

up: ## Start the local stack (needs .env)
	$(COMPOSE) up --build -d

down: ## Stop the local stack
	$(COMPOSE) down
