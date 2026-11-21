"""Where the repository to fix comes from."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Protocol

MAX_FILES = 2_000
MAX_BYTES = 50_000_000
_SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv", "node_modules"}


class SourceUnavailable(RuntimeError):
    """No checkout of this repository is available; the failure is ticketed without a fix."""


class RepoSource(Protocol):
    async def checkout(self, repo: str, sha: str | None, dest: Path) -> None:
        """Materialise the repository at ``sha`` into the (new, empty) directory ``dest``."""
        ...


class LocalRepoSource:
    """Mock mode: copies ``<root>/<owner>__<name>``. The directory stands for the failing commit.

    Symlinks are never followed or copied (a link could point at host files that would then be
    read into a prompt), and the copy is size-limited. A real git source arrives in Phase 10.
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    async def checkout(self, repo: str, sha: str | None, dest: Path) -> None:
        src = self.root / repo.replace("/", "__")
        if not src.is_dir():
            raise SourceUnavailable(f"no local checkout of {repo} under {self.root}")
        files = size = 0
        for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
            dirnames[:] = [
                d for d in dirnames if d not in _SKIP_DIRS and not Path(dirpath, d).is_symlink()
            ]
            target = dest / Path(dirpath).relative_to(src)
            target.mkdir(parents=True, exist_ok=True)
            for name in filenames:
                f = Path(dirpath, name)
                if f.is_symlink():
                    continue
                files += 1
                size += f.stat().st_size
                if files > MAX_FILES or size > MAX_BYTES:
                    raise SourceUnavailable("repository is too large for an automatic fix")
                shutil.copyfile(f, target / name)
