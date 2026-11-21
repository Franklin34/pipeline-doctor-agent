"""Fetch the repository at the failing commit from GitHub, safely.

Uses the tarball endpoint instead of ``git clone``: no ``git`` binary, no credentials written to
disk, no hooks or submodules, and the archive is inspected before anything is written.

The archive comes from a repository the failing build's authors control, so it is hostile input:

* downloaded with a size cap, and the token is sent only to the API host, never on to the
  redirect target (``codeload``), which is fetched with a separate, header-less request;
* only regular files and directories are extracted: symlinks, hard links, devices and anything
  with an absolute path or ``..`` are dropped or refused;
* caps on file count and total size, enforced while reading, not after.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import httpx

from pipeline_doctor_agent.fix.source import MAX_BYTES, MAX_FILES, SourceUnavailable

MAX_DOWNLOAD = 50_000_000
SKIP_PARTS = {".git"}


class GitHubTarballSource:
    def __init__(
        self,
        token: str,
        allowed_repos: frozenset[str],
        *,
        api_url: str = "https://api.github.com",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not token:
            raise ValueError("a GitHub token is required to fetch repositories")
        self._token = token
        self.allowed = frozenset(r.lower() for r in allowed_repos)
        self.api = api_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=30.0, follow_redirects=False)

    async def checkout(self, repo: str, sha: str | None, dest: Path) -> None:
        if repo.lower() not in self.allowed:
            raise SourceUnavailable(f"repository {repo} is not on the allowed list")
        if not sha:
            raise SourceUnavailable("the failing commit is unknown")
        data = await self._download(repo, sha)
        _extract(data, dest)

    async def _download(self, repo: str, sha: str) -> bytes:
        try:
            first = await self._client.get(
                f"{self.api}/repos/{repo}/tarball/{sha}",
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "pipeline-doctor",
                },
            )
            if first.status_code in {301, 302, 303, 307, 308}:
                target = first.headers.get("location", "")
                if urlparse(target).scheme != "https":
                    raise SourceUnavailable("the archive redirect was not https")
                # The redirect goes to another host: no credentials are sent along.
                return await self._bounded(target)
            if first.status_code == 404:
                raise SourceUnavailable(f"commit {sha[:12]} of {repo} was not found")
            if first.status_code in {401, 403}:
                raise SourceUnavailable("GitHub refused access to the repository archive")
            raise SourceUnavailable(f"GitHub answered {first.status_code} for the archive")
        except httpx.HTTPError as exc:
            raise SourceUnavailable(
                f"could not download the repository ({type(exc).__name__})"
            ) from None

    async def _bounded(self, url: str) -> bytes:
        chunks: list[bytes] = []
        size = 0
        async with self._client.stream(
            "GET", url, headers={"User-Agent": "pipeline-doctor"}
        ) as resp:
            if resp.status_code != 200:
                raise SourceUnavailable(f"archive download answered {resp.status_code}")
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > MAX_DOWNLOAD:
                    raise SourceUnavailable("repository archive is too large for an automatic fix")
                chunks.append(chunk)
        return b"".join(chunks)


def _extract(data: bytes, dest: Path) -> None:
    """Extract regular files only, stripping the archive's single top directory."""
    files = total = 0
    root = dest.resolve()
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for member in tar:
                if member.name.startswith("/") or ".." in PurePosixPath(member.name).parts:
                    raise SourceUnavailable("the archive contains an unsafe path")
                parts = PurePosixPath(member.name).parts
                if len(parts) < 2:  # the top directory itself, or a stray file at the root
                    continue
                rel = PurePosixPath(*parts[1:])
                if rel.is_absolute() or ".." in rel.parts:
                    raise SourceUnavailable("the archive contains an unsafe path")
                if SKIP_PARTS & set(rel.parts):
                    continue
                if member.isdir():
                    continue
                if not member.isreg():  # symlink, hard link, device, fifo: never materialised
                    continue
                files += 1
                total += member.size
                if files > MAX_FILES or total > MAX_BYTES:
                    raise SourceUnavailable("repository is too large for an automatic fix")
                target = (root / rel).resolve()
                if root not in target.parents:
                    raise SourceUnavailable("the archive contains an unsafe path")
                target.parent.mkdir(parents=True, exist_ok=True)
                extracted = tar.extractfile(member)
                if extracted is None:
                    continue
                target.write_bytes(extracted.read(member.size + 1)[: member.size])
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise SourceUnavailable(
            f"the repository archive is unreadable ({type(exc).__name__})"
        ) from None
