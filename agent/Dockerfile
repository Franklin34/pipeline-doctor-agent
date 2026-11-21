# Worker image. Build from the repository root:  docker build -f agent/Dockerfile -t pipeline-doctor-worker .
# Diagnosis and tickets only: the fix sandbox needs a Docker host, which Cloud Run does not have.

FROM golang:1.27 AS jenkins-mcp
WORKDIR /src
COPY mcp-servers/jenkins/go.mod mcp-servers/jenkins/go.sum ./
RUN go mod download
COPY mcp-servers/jenkins/ ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/jenkins-mcp ./cmd/jenkins-mcp

FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.13 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock .python-version ./
COPY agent/ agent/
COPY mcp-servers/github/ mcp-servers/github/
COPY mcp-servers/jira/ mcp-servers/jira/
COPY rag/ rag/
COPY sandbox/ sandbox/
COPY evals/ evals/
RUN uv sync --frozen --no-dev --no-editable --package pipeline-doctor-agent --package pd-mcp-jira

FROM python:3.13-slim
RUN useradd --system --uid 10001 --no-create-home worker
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY --from=jenkins-mcp /out/jenkins-mcp /usr/local/bin/jenkins-mcp
COPY fixtures/ /app/fixtures/
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 JENKINS_MCP_COMMAND=/usr/local/bin/jenkins-mcp FIXTURES_DIR=/app/fixtures
USER 10001
EXPOSE 9102
CMD ["python", "-m", "pipeline_doctor_agent.worker"]
