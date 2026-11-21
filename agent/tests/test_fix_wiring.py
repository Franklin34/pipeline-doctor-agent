from __future__ import annotations

from pipeline_doctor_agent.fix.factory import build_fix_service
from pipeline_doctor_agent.fix.service import FixService
from pipeline_doctor_agent.settings import Settings


def test_fixes_are_off_by_default() -> None:
    assert Settings().fix_enabled is False
    assert Settings.from_env({}).fix_enabled is False
    assert build_fix_service(Settings()) is None


def test_only_the_exact_string_true_enables_fixes() -> None:
    for value in ("", "1", "yes", "TRUE ", "enabled", "false"):
        assert Settings.from_env({"FIX_ENABLED": value}).fix_enabled is (
            value.strip().lower() == "true"
        )
    assert Settings.from_env({"FIX_ENABLED": "true"}).fix_enabled is True


def test_enabled_in_mock_mode_builds_the_service() -> None:
    assert isinstance(build_fix_service(Settings(fix_enabled=True)), FixService)


def test_enabled_outside_mock_mode_is_refused_until_a_real_git_source_exists() -> None:
    assert build_fix_service(Settings(fix_enabled=True, mcp_mode="real")) is None


def test_attempt_limit_is_capped_at_three() -> None:
    import pytest

    for bad in ("0", "4", "99", "x"):
        with pytest.raises(ValueError):
            Settings.from_env({"FIX_MAX_ATTEMPTS": bad})
    assert Settings.from_env({"FIX_MAX_ATTEMPTS": "2"}).fix_max_attempts == 2
    with pytest.raises(ValueError):
        Settings(fix_timeout_s=0)
