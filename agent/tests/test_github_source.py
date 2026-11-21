"""Fetching a repository from GitHub for the fix flow: hostile archives, credentials, wiring."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import httpx
import pytest
from pipeline_doctor_agent.fix import github_source
from pipeline_doctor_agent.fix.factory import build_fix_service
from pipeline_doctor_agent.fix.github_source import GitHubTarballSource
from pipeline_doctor_agent.fix.source import LocalRepoSource, SourceUnavailable
from pipeline_doctor_agent.settings import Settings

TOKEN = "github_pat_" + "Y" * 40
REPO = "acme/demo-service"
SHA = "c" * 40


def tarball(entries: list[tuple[str, bytes | None, str]]) -> bytes:
    """(name, content, kind) with kind in file|dir|symlink|hardlink; symlink/hardlink content is the target."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content, kind in entries:
            info = tarfile.TarInfo(name)
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            elif kind in {"symlink", "hardlink"}:
                info.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE
                info.linkname = (content or b"").decode()
                tar.addfile(info)
            else:
                data = content or b""
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


GOOD = tarball(
    [
        ("acme-demo-service-ccc", None, "dir"),
        ("acme-demo-service-ccc/app/pricing.py", b"def f(): pass\n", "file"),
        ("acme-demo-service-ccc/requirements.txt", b"pytest==9.2.0\n", "file"),
        ("acme-demo-service-ccc/.git/config", b"[core]", "file"),
    ]
)


class Archive:
    """Fake of api.github.com (redirects) and codeload (serves the archive); records requests."""

    def __init__(self, body: bytes, api_status: int = 302) -> None:
        self.body, self.api_status = body, api_status
        self.requests: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if req.url.host == "api.github.com":
            if self.api_status == 302:
                return httpx.Response(
                    302,
                    headers={"location": f"https://codeload.github.com/{REPO}/legacy.tar.gz/{SHA}"},
                )
            return httpx.Response(self.api_status)
        return httpx.Response(200, content=self.body)


def source(archive: Archive, repos: frozenset[str] = frozenset({REPO})) -> GitHubTarballSource:
    return GitHubTarballSource(
        TOKEN, repos, client=httpx.AsyncClient(transport=httpx.MockTransport(archive))
    )


async def test_a_normal_archive_is_extracted_without_its_top_directory_or_git_data(
    tmp_path: Path,
) -> None:
    await source(Archive(GOOD)).checkout(REPO, SHA, tmp_path)
    assert (tmp_path / "app/pricing.py").read_text() == "def f(): pass\n"
    assert (tmp_path / "requirements.txt").exists()
    assert not (tmp_path / ".git").exists()


async def test_the_token_goes_to_the_api_host_and_is_not_forwarded_to_the_redirect_target(
    tmp_path: Path,
) -> None:
    a = Archive(GOOD)
    await source(a).checkout(REPO, SHA, tmp_path)
    api, cdn = a.requests
    assert api.headers["authorization"] == f"Bearer {TOKEN}"
    assert "authorization" not in cdn.headers and TOKEN not in str(cdn.url)


@pytest.mark.parametrize(
    "entries",
    [
        [("top/../../evil.py", b"x", "file")],  # path traversal
        [("/etc/passwd", b"x", "file")],  # absolute (a single root part is skipped; the next fails)
        [("top/a/../../../evil", b"x", "file")],
    ],
)
async def test_unsafe_paths_are_refused_and_nothing_lands_outside_the_destination(
    tmp_path: Path, entries: list[tuple[str, bytes | None, str]]
) -> None:
    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(SourceUnavailable, match="unsafe path"):
        await source(Archive(tarball(entries))).checkout(REPO, SHA, dest)
    outside = [p for p in tmp_path.rglob("*") if dest not in p.parents and p != dest]
    assert outside == [] and list(dest.rglob("*")) == []


async def test_links_and_special_files_are_never_materialised(tmp_path: Path) -> None:
    data = tarball(
        [
            ("top/real.py", b"ok", "file"),
            ("top/link", b"/etc/passwd", "symlink"),
            ("top/hard", b"top/real.py", "hardlink"),
        ]
    )
    await source(Archive(data)).checkout(REPO, SHA, tmp_path)
    assert sorted(p.name for p in tmp_path.rglob("*")) == ["real.py"]


async def test_too_many_files_and_too_much_data_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github_source, "MAX_FILES", 3)
    many = tarball([(f"top/f{i}.py", b"x", "file") for i in range(10)])
    with pytest.raises(SourceUnavailable, match="too large"):
        await source(Archive(many)).checkout(REPO, SHA, tmp_path / "a")
    monkeypatch.setattr(github_source, "MAX_FILES", 100)
    monkeypatch.setattr(github_source, "MAX_BYTES", 100)
    big = tarball([("top/big.bin", b"x" * 1000, "file")])
    with pytest.raises(SourceUnavailable, match="too large"):
        await source(Archive(big)).checkout(REPO, SHA, tmp_path / "b")


async def test_an_oversized_download_is_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github_source, "MAX_DOWNLOAD", 100)
    with pytest.raises(SourceUnavailable, match="too large"):
        await source(Archive(GOOD)).checkout(REPO, SHA, tmp_path)


async def test_garbage_is_reported_not_raised_raw(tmp_path: Path) -> None:
    with pytest.raises(SourceUnavailable, match="unreadable"):
        await source(Archive(b"this is not a tarball")).checkout(REPO, SHA, tmp_path)


@pytest.mark.parametrize(
    ("status", "text"), [(404, "not found"), (403, "refused"), (401, "refused"), (500, "500")]
)
async def test_api_errors_become_source_unavailable(tmp_path: Path, status: int, text: str) -> None:
    with pytest.raises(SourceUnavailable, match=text) as info:
        await source(Archive(GOOD, api_status=status)).checkout(REPO, SHA, tmp_path)
    assert TOKEN not in str(info.value)


async def test_a_repository_off_the_list_or_a_missing_sha_makes_no_request(tmp_path: Path) -> None:
    a = Archive(GOOD)
    with pytest.raises(SourceUnavailable, match="allowed list"):
        await source(a).checkout("evil/other", SHA, tmp_path)
    with pytest.raises(SourceUnavailable, match="unknown"):
        await source(a).checkout(REPO, None, tmp_path)
    assert a.requests == []


async def test_an_http_redirect_is_refused(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://codeload.example/x"})

    src = GitHubTarballSource(
        TOKEN, frozenset({REPO}), client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(SourceUnavailable, match="not https"):
        await src.checkout(REPO, SHA, tmp_path)


# ---- settings and wiring -------------------------------------------------------------------------------------


def test_the_token_is_not_in_the_settings_repr() -> None:
    s = Settings(github_token=TOKEN)
    assert TOKEN not in repr(s) and TOKEN not in str(s)


def test_the_token_reaches_only_the_real_github_server() -> None:
    mock = Settings(github_token=TOKEN)
    assert "GITHUB_TOKEN" not in mock.github_env() and mock.github_env()["MCP_MODE"] == "mock"
    real = Settings(github_mode="real", github_token=TOKEN, github_allowed_repos=REPO)
    env = real.github_env()
    assert (
        env["GITHUB_TOKEN"] == TOKEN
        and env["GITHUB_ALLOWED_REPOS"] == REPO
        and env["MCP_MODE"] == "real"
    )


def test_github_can_be_real_while_everything_else_stays_mock() -> None:
    s = Settings.from_env(
        {"GITHUB_MODE": "real", "GITHUB_TOKEN": TOKEN, "GITHUB_ALLOWED_REPOS": REPO}
    )
    assert s.effective_github_mode == "real" and s.mcp_mode == "mock"
    assert Settings.from_env({"MCP_MODE": "real"}).effective_github_mode == "real"
    with pytest.raises(ValueError, match="GITHUB_MODE"):
        Settings.from_env({"GITHUB_MODE": "staging"})


def test_the_factory_picks_the_source_from_the_mode(tmp_path: Path) -> None:
    on = {"fix_enabled": True, "repos_dir": str(tmp_path)}
    assert build_fix_service(Settings()) is None  # opt-in
    mock = build_fix_service(Settings(**on))
    assert mock is not None and isinstance(mock.source, LocalRepoSource)
    real = build_fix_service(
        Settings(**on, github_mode="real", github_token=TOKEN, github_allowed_repos=REPO)
    )
    assert real is not None and isinstance(real.source, GitHubTarballSource)
    # real mode without credentials or an allowlist never falls back to anything
    assert build_fix_service(Settings(**on, github_mode="real")) is None
    assert build_fix_service(Settings(**on, github_mode="real", github_token=TOKEN)) is None
