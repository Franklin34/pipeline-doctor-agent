"""Build the real FixService from settings (mock mode: local repositories, Docker sandbox)."""

from __future__ import annotations

import logging
from pathlib import Path

from pd_sandbox import DockerSandbox, Limits

from pipeline_doctor_agent.fix.github_source import GitHubTarballSource
from pipeline_doctor_agent.fix.proposer import AdkProposer
from pipeline_doctor_agent.fix.service import FixService
from pipeline_doctor_agent.fix.source import LocalRepoSource, RepoSource
from pipeline_doctor_agent.settings import Settings
from pipeline_doctor_agent.ticket import default_github_client

log = logging.getLogger("pipeline_doctor.fix")


def build_fix_service(settings: Settings) -> FixService | None:
    """None unless FIX_ENABLED=true. Fixes are opt-in: the default is diagnose and ticket only."""
    if not settings.fix_enabled:
        return None
    mode = settings.effective_github_mode
    source: RepoSource
    if mode == "mock":
        source = LocalRepoSource(Path(settings.repos_dir))
    elif settings.github_token and settings.github_allowed_repos:
        source = GitHubTarballSource(
            settings.github_token,
            frozenset(r.strip() for r in settings.github_allowed_repos.split(",") if r.strip()),
            api_url=settings.github_api_url,
        )
    else:
        log.warning(
            "FIX_ENABLED ignored: real GitHub mode needs GITHUB_TOKEN and GITHUB_ALLOWED_REPOS"
        )
        return None
    sandbox = DockerSandbox(
        settings.sandbox_image,
        Limits(
            memory=settings.sandbox_memory,
            cpus=settings.sandbox_cpus,
            test_timeout_s=settings.sandbox_test_timeout_s,
        ),
    )
    return FixService(
        settings,
        source=source,
        sandbox=sandbox,
        proposer_factory=lambda guard: AdkProposer(settings.large_model, guard),
        github=lambda: default_github_client(settings),
    )
