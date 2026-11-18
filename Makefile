# Compatible with GNU Make 3.81 (macOS default).
GO_MODULES := services/ingress mcp-servers/jenkins
PY_SRC := agent/src mcp-servers/github/src mcp-servers/jira/src rag/src sandbox/src evals/src

.PHONY: help setup fmt lint typecheck test test-go check up down
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

test: ## Python tests
	uv run pytest

test-go: ## Go tests with the race detector
	@for m in $(GO_MODULES); do (cd $$m && go test -race ./...) || exit 1; done

check: lint typecheck test test-go ## Everything CI runs

COMPOSE := docker compose --env-file .env -f deploy/compose/docker-compose.yml

up: ## Start the local stack (needs .env)
	$(COMPOSE) up --build -d

down: ## Stop the local stack
	$(COMPOSE) down
