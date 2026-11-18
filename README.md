# Pipeline Doctor

An AI agent system that investigates failed Jenkins pipelines, finds the root cause in GitHub, opens a **draft** pull request when a fix is safe, and creates or updates a Jira ticket. Humans keep control of every change that reaches the codebase: the agent never merges, never pushes to the default branch, and never changes infrastructure.

See [docs/architecture.md](docs/architecture.md) and [docs/ingress.md](docs/ingress.md), [docs/mcp-servers.md](docs/mcp-servers.md), [docs/preprocess.md](docs/preprocess.md).

## Quick start

```sh
make setup   # uv sync + git hooks
make check   # lint, types, tests (Python + Go)
```

Requires uv, Go 1.27 and Docker. uv manages Python 3.13.

## Status

| Phase | Scope | State |
|---|---|---|
| 0 | Foundations | done |
| 1 | Ingress + queue | done |
| 2 | MCP servers (mock) | done |
| 3 | Preprocessing | in review |
| 4 | Agents (diagnosis) | - |
| 5 | RAG | - |
| 6 | Jira tickets | - |
| 7 | Sandbox + fixes | - |
| 8 | Observability | - |
| 9 | Evals + security tests | - |
| 10 | Real integrations + deploy | - |
| 11 | Polish | - |
