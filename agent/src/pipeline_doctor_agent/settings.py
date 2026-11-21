"""Runtime settings, read from the environment (see .env.example)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field


def _float(env: Mapping[str, str], key: str, default: float) -> float:
    try:
        return float(env.get(key, default))
    except ValueError as exc:
        raise ValueError(f"{key} must be a number") from exc


def _int(env: Mapping[str, str], key: str, default: int) -> int:
    try:
        return int(env.get(key, default))
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer") from exc


@dataclass(frozen=True)
class Settings:
    small_model: str = "gemini-2.5-flash"
    large_model: str = "gemini-2.5-pro"
    fix_threshold: float = 0.8
    max_llm_calls: int = 30
    run_timeout_s: float = 300.0
    token_budget: int = 200_000
    github_repo: str | None = None  # default repo when the event carries none
    # queue worker
    redis_url: str = "redis://localhost:6379/0"
    stream: str = "pipeline-failed"
    group: str = "pipeline-doctor"
    consumer: str = "worker-1"
    max_attempts: int = 3
    min_idle_ms: int = 60_000
    block_ms: int = 2_000
    # MCP servers (mock mode needs only fixtures)
    mcp_mode: str = "mock"
    fixtures_dir: str = "fixtures"
    jenkins_mcp_command: str = "jenkins-mcp"
    # knowledge base (RAG); None disables it and the knowledge agent becomes a no-op
    # automatic fixes: OFF unless explicitly enabled
    fix_enabled: bool = False
    fix_max_attempts: int = 3
    fix_timeout_s: float = 600.0
    sandbox_image: str = "python:3.13-slim"
    sandbox_memory: str = "512m"
    sandbox_cpus: float = 1.0
    sandbox_test_timeout_s: float = 120.0
    repos_dir: str = "fixtures/repos"  # mock mode: local checkouts named owner__name
    jira_mcp_command: str | None = None  # default: python -m pd_mcp_jira
    github_mcp_command: str | None = None  # default: python -m pd_mcp_github
    github_web_url: str = "https://github.com"
    database_url: str | None = None
    embedder: str = "gemini"  # gemini | hash (offline, deterministic, not semantic)
    embedding_model: str = "gemini-embedding-001"
    embedding_dim: int = 768
    # real GitHub (GITHUB_MODE=real). The token is kept out of repr() so it cannot reach a log by accident.
    github_mode: str | None = None  # None: follow MCP_MODE
    github_token: str | None = field(default=None, repr=False)
    github_allowed_repos: str = (
        ""  # comma-separated owner/name list: the only repositories reachable
    )
    github_api_url: str = "https://api.github.com"
    # observability
    metrics_addr: str = "127.0.0.1"  # bind address of /metrics; 0.0.0.0 only inside a container
    metrics_port: int = 9102  # 0 disables the metrics endpoint
    otlp_endpoint: str | None = None  # None: spans stay in the process
    price_in_per_mtok: float = 0.0  # USD per million prompt tokens, for the cost estimate
    price_out_per_mtok: float = 0.0

    def __post_init__(self) -> None:
        if not 0.0 <= self.fix_threshold <= 1.0:
            raise ValueError("FIX_THRESHOLD must be between 0 and 1")
        if (
            min(self.max_llm_calls, self.token_budget, self.max_attempts) < 1
            or self.run_timeout_s <= 0
        ):
            raise ValueError("run limits must be positive")
        if not 1 <= self.fix_max_attempts <= 3:
            raise ValueError("FIX_MAX_ATTEMPTS must be between 1 and 3")
        if self.fix_timeout_s <= 0 or self.sandbox_test_timeout_s <= 0 or self.sandbox_cpus <= 0:
            raise ValueError("fix and sandbox limits must be positive")
        if self.effective_github_mode not in {"mock", "real"}:
            raise ValueError("GITHUB_MODE must be mock or real")
        if self.embedder not in {"gemini", "hash"}:
            raise ValueError("EMBEDDER must be gemini or hash")
        if (
            not 0 <= self.metrics_port <= 65535
            or min(self.price_in_per_mtok, self.price_out_per_mtok) < 0
        ):
            raise ValueError("METRICS_PORT must be 0-65535 and prices must not be negative")

    @property
    def effective_github_mode(self) -> str:
        return self.github_mode or self.mcp_mode

    def github_env(self) -> dict[str, str]:
        """Environment for the GitHub MCP subprocess. The token goes ONLY to this server: the Jenkins
        and Jira servers never receive it."""
        env = {"MCP_MODE": self.effective_github_mode, "FIXTURES_DIR": self.fixtures_dir}
        if self.effective_github_mode == "real":
            env |= {
                "GITHUB_TOKEN": self.github_token or "",
                "GITHUB_ALLOWED_REPOS": self.github_allowed_repos,
                "GITHUB_API_URL": self.github_api_url,
            }
        return env

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        e = os.environ if env is None else env
        d = cls()
        return cls(
            small_model=e.get("SMALL_MODEL", d.small_model),
            large_model=e.get("LARGE_MODEL", d.large_model),
            fix_threshold=_float(e, "FIX_THRESHOLD", d.fix_threshold),
            max_llm_calls=_int(e, "MAX_LLM_CALLS_PER_RUN", d.max_llm_calls),
            run_timeout_s=_float(e, "RUN_TIMEOUT_SECONDS", d.run_timeout_s),
            token_budget=_int(e, "RUN_TOKEN_BUDGET", d.token_budget),
            github_repo=e.get("GITHUB_REPO") or None,
            redis_url=e.get("REDIS_URL", d.redis_url),
            stream=e.get("QUEUE_STREAM", d.stream),
            group=e.get("QUEUE_GROUP", d.group),
            consumer=e.get("QUEUE_CONSUMER", d.consumer),
            max_attempts=_int(e, "MAX_ATTEMPTS", d.max_attempts),
            min_idle_ms=_int(e, "QUEUE_MIN_IDLE_MS", d.min_idle_ms),
            block_ms=_int(e, "QUEUE_BLOCK_MS", d.block_ms),
            mcp_mode=e.get("MCP_MODE", d.mcp_mode),
            fixtures_dir=e.get("FIXTURES_DIR", d.fixtures_dir),
            jenkins_mcp_command=e.get("JENKINS_MCP_COMMAND", d.jenkins_mcp_command),
            fix_enabled=e.get("FIX_ENABLED", "false").strip().lower() == "true",
            fix_max_attempts=_int(e, "FIX_MAX_ATTEMPTS", d.fix_max_attempts),
            fix_timeout_s=_float(e, "FIX_TIMEOUT_SECONDS", d.fix_timeout_s),
            sandbox_image=e.get("SANDBOX_IMAGE", d.sandbox_image),
            sandbox_memory=e.get("SANDBOX_MEMORY", d.sandbox_memory),
            sandbox_cpus=_float(e, "SANDBOX_CPUS", d.sandbox_cpus),
            sandbox_test_timeout_s=_float(
                e, "SANDBOX_TEST_TIMEOUT_SECONDS", d.sandbox_test_timeout_s
            ),
            repos_dir=e.get("REPOS_DIR", d.repos_dir),
            jira_mcp_command=e.get("JIRA_MCP_COMMAND") or None,
            github_mcp_command=e.get("GITHUB_MCP_COMMAND") or None,
            github_web_url=e.get("GITHUB_WEB_URL", d.github_web_url).rstrip("/"),
            database_url=e.get("DATABASE_URL") or None,
            embedder=e.get("EMBEDDER", d.embedder),
            embedding_model=e.get("EMBEDDING_MODEL", d.embedding_model),
            embedding_dim=_int(e, "EMBEDDING_DIM", d.embedding_dim),
            github_mode=e.get("GITHUB_MODE") or None,
            github_token=e.get("GITHUB_TOKEN") or None,
            github_allowed_repos=e.get("GITHUB_ALLOWED_REPOS", d.github_allowed_repos),
            github_api_url=e.get("GITHUB_API_URL", d.github_api_url),
            metrics_addr=e.get("METRICS_ADDR", d.metrics_addr),
            metrics_port=_int(e, "METRICS_PORT", d.metrics_port),
            otlp_endpoint=e.get("OTEL_EXPORTER_OTLP_ENDPOINT") or None,
            price_in_per_mtok=_float(e, "PRICE_IN_PER_MTOK", d.price_in_per_mtok),
            price_out_per_mtok=_float(e, "PRICE_OUT_PER_MTOK", d.price_out_per_mtok),
        )
