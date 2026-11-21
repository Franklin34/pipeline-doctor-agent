from __future__ import annotations

import os
from pathlib import Path

import pytest
from pipeline_doctor_agent.fix.edits import EditError, apply_edits, unified_diff
from pipeline_doctor_agent.fix.patch_policy import check_edits, is_test_path, read_from
from pipeline_doctor_agent.fix.source import MAX_FILES, LocalRepoSource, SourceUnavailable
from pipeline_doctor_agent.schemas import Category, FixEdit

FILES = {
    "app/pricing.py": "def apply_discount(price, rate):\n    return price * rate\n",
    "tests/test_pricing.py": "def test_a():\n    assert 1 == 1\n",
    "requirements.txt": "pytest==9.2.0\nrich==14.0.0\n",
    "Jenkinsfile": "pipeline {}\n",
    ".github/workflows/ci.yml": "name: ci\n",
    "conftest.py": "x = 1\n",
}
READ = read_from(FILES)


def edit(path: str, old: str, new: str) -> FixEdit:
    return FixEdit(path=path, old_text=old, new_text=new)


GOOD = edit("app/pricing.py", "price * rate", "price * (1 - rate)")


def test_a_clean_fix_has_no_violations() -> None:
    assert check_edits([GOOD], Category.CODE, READ) == []


@pytest.mark.parametrize(
    ("e", "category", "needle"),
    [
        (edit("tests/test_pricing.py", "1 == 1", "True"), Category.CODE, "tests may not be edited"),
        (
            edit("app/pricing.py", "price * rate", "price * rate  # x"),
            Category.TEST,
            "only test files",
        ),
        (edit("Jenkinsfile", "pipeline {}", "pipeline { x }"), Category.CODE, "protected path"),
        (
            edit(".github/workflows/ci.yml", "name: ci", "name: pwn"),
            Category.CODE,
            "protected path",
        ),
        (edit("conftest.py", "x = 1", "x = 2"), Category.CODE, "protected path"),
        (edit("app/new_file.py", "a", "b"), Category.CODE, "does not exist"),
        (edit("../outside.py", "a", "b"), Category.CODE, "safe repository-relative"),
        (edit("/etc/passwd", "a", "b"), Category.CODE, "safe repository-relative"),
        (edit("app\\pricing.py", "a", "b"), Category.CODE, "safe repository-relative"),
        (edit("app/pricing.py", "price * rate", "price * rate"), Category.CODE, "identical"),
        (
            edit("requirements.txt", "pytest==9.2.0", "pytest==9.1.0"),
            Category.CODE,
            "only change for a 'dependency'",
        ),
        (
            edit("requirements.txt", "rich==14.0.0", "rich==14.0.0\nevil-pkg==1.0"),
            Category.DEPENDENCY,
            "added or removed",
        ),
        (edit("requirements.txt", "rich==14.0.0", ""), Category.DEPENDENCY, "added or removed"),
        (
            edit(
                "app/pricing.py",
                "price * rate",
                "import subprocess\n    return subprocess.run(['x'])",
            ),
            Category.CODE,
            "runs commands",
        ),
        (
            edit("app/pricing.py", "price * rate", "__import__('os').system('x')"),
            Category.CODE,
            "dynamic code|runs commands",
        ),
        (edit("app/pricing.py", "price * rate", "eval('1')"), Category.CODE, "dynamic code"),
        (
            edit("app/pricing.py", "price * rate", "urllib.request.urlopen('http://x')"),
            Category.CODE,
            "network access",
        ),
        (
            edit("app/pricing.py", "price * rate", "price * rate\n    # @pytest.mark.skip"),
            Category.CODE,
            "skips or neuters tests",
        ),
    ],
)
def test_each_violation_is_caught(e: FixEdit, category: Category, needle: str) -> None:
    problems = check_edits([e], category, READ)
    assert problems and any(__import__("re").search(needle, p) for p in problems), problems


def test_a_dependency_version_bump_is_allowed_for_dependency_failures() -> None:
    assert (
        check_edits(
            [edit("requirements.txt", "rich==14.0.0", "rich==13.9.0")], Category.DEPENDENCY, READ
        )
        == []
    )


def test_test_failures_may_edit_tests_but_not_config() -> None:
    assert (
        check_edits([edit("tests/test_pricing.py", "1 == 1", "2 == 2")], Category.TEST, READ) == []
    )
    assert check_edits([edit("conftest.py", "x = 1", "x = 2")], Category.TEST, READ)


def test_size_limits() -> None:
    big = "x\n" * 150
    files = {"a.py": big * 2}
    problems = check_edits(
        [edit("a.py", big, big.replace("x", "y"))], Category.CODE, read_from(files)
    )
    assert any("larger than" in p for p in problems)
    too_many = [FixEdit(path="a.py", old_text=str(i), new_text=str(i + 1)) for i in range(5)]
    assert (
        check_edits(too_many, Category.CODE, read_from({"a.py": "\n".join(map(str, range(5)))}))
        == []
    )


def test_is_test_path() -> None:
    for p in (
        "tests/test_a.py",
        "test/x.py",
        "pkg/tests/helpers.py",
        "test_x.py",
        "x_test.py",
        "web/a.spec.ts",
    ):
        assert is_test_path(p), p
    for p in ("app/pricing.py", "contest.py", "src/latest.py", "app/attestation.py"):
        assert not is_test_path(p), p


# ---- edits ---------------------------------------------------------------------------------------


def test_edits_apply_exactly_and_produce_a_diff() -> None:
    new = apply_edits(FILES, [GOOD])
    assert new == {
        "app/pricing.py": "def apply_discount(price, rate):\n    return price * (1 - rate)\n"
    }
    diff = unified_diff(FILES, new)
    assert "--- a/app/pricing.py" in diff and "+    return price * (1 - rate)" in diff


def test_edits_are_all_or_nothing_and_must_match_once() -> None:
    with pytest.raises(EditError, match="not found"):
        apply_edits(FILES, [edit("app/pricing.py", "no such text", "x")])
    with pytest.raises(EditError, match="appears 2 times"):
        apply_edits({"a.py": "x = 1\nx = 1\n"}, [edit("a.py", "x = 1", "x = 2")])
    with pytest.raises(EditError, match="was not provided"):
        apply_edits(FILES, [edit("missing.py", "a", "b")])
    with pytest.raises(EditError):
        apply_edits(
            FILES, [GOOD, edit("app/pricing.py", "no such text", "x")]
        )  # the first edit is not kept


def test_sequential_edits_in_one_file_apply_in_order() -> None:
    files = {"a.py": "a = 1\nb = 2\n"}
    out = apply_edits(files, [edit("a.py", "a = 1", "a = 10"), edit("a.py", "b = 2", "b = 20")])
    assert out["a.py"] == "a = 10\nb = 20\n"


def test_an_edit_that_changes_nothing_yields_no_files() -> None:
    assert apply_edits({"a.py": "x\n"}, [edit("a.py", "x", "y"), edit("a.py", "y", "x")]) == {}


# ---- repository source ---------------------------------------------------------------------------


async def test_local_source_copies_the_repo_and_skips_git_and_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "repos"
    repo = root / "acme__app"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "a.py").write_text("x = 1\n")
    (repo / ".git").mkdir()
    (repo / ".git" / "config").write_text("secret")
    outside = tmp_path / "outside.txt"
    outside.write_text("host file")
    os.symlink(outside, repo / "link.txt")
    os.symlink(tmp_path, repo / "dirlink")
    dest = tmp_path / "dest"
    dest.mkdir()
    await LocalRepoSource(root).checkout("acme/app", "abc1234", dest)
    assert (dest / "app" / "a.py").read_text() == "x = 1\n"
    assert (
        not (dest / ".git").exists()
        and not (dest / "link.txt").exists()
        and not (dest / "dirlink").exists()
    )


async def test_local_source_errors(tmp_path: Path) -> None:
    root = tmp_path / "repos"
    root.mkdir()
    dest = tmp_path / "d"
    dest.mkdir()
    with pytest.raises(SourceUnavailable, match="no local checkout"):
        await LocalRepoSource(root).checkout("acme/missing", None, dest)
    big = root / "acme__big"
    big.mkdir()
    for i in range(MAX_FILES + 1):
        (big / f"f{i}.txt").write_text("x")
    with pytest.raises(SourceUnavailable, match="too large"):
        await LocalRepoSource(root).checkout("acme/big", None, dest)
