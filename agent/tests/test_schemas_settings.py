from __future__ import annotations

import pytest
from pipeline_doctor_agent.schemas import (
    CodeFindings,
    Diagnosis,
    PipelineFailedEvent,
    parse_model_json,
)
from pipeline_doctor_agent.settings import Settings
from pydantic import ValidationError

GOOD = {
    "category": "code",
    "root_cause": "a specific enough root cause",
    "evidence": ["assert failed"],
    "suspect_commit": "C3D4E5F",
    "confidence": 0.9,
    "fixable": True,
}


def test_diagnosis_accepts_and_normalises() -> None:
    assert Diagnosis.model_validate(GOOD).suspect_commit == "c3d4e5f"


@pytest.mark.parametrize(
    "patch",
    [
        {"category": "magic"},
        {"confidence": 1.5},
        {"confidence": -0.1},
        {"evidence": []},
        {"evidence": ["  "]},
        {"root_cause": "short"},
        {"suspect_commit": "not-a-sha"},
        {"unexpected": "field"},
        {"fixable": "maybe"},
        {"root_cause": "x" * 1001},
    ],
)
def test_diagnosis_rejects_invalid(patch: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Diagnosis.model_validate(GOOD | patch)


def test_parse_model_json_accepts_dict_json_and_fenced_json() -> None:
    import json

    assert parse_model_json(Diagnosis, GOOD).confidence == 0.9
    assert parse_model_json(Diagnosis, json.dumps(GOOD)).confidence == 0.9
    assert parse_model_json(Diagnosis, "```json\n" + json.dumps(GOOD) + "\n```").confidence == 0.9
    with pytest.raises(ValidationError):
        parse_model_json(Diagnosis, "I think it is a code problem")


def test_code_findings_limits() -> None:
    with pytest.raises(ValidationError):
        CodeFindings.model_validate({"candidate_commits": [{"sha": "zzz", "reason": "x"}]})


def test_event_ignores_unknown_fields_but_validates_known_ones() -> None:
    e = PipelineFailedEvent.model_validate(
        {"idempotency_key": "j:1", "job_name": "j", "build_number": 1, "future_field": 1}
    )
    assert e.build_number == 1
    with pytest.raises(ValidationError):
        PipelineFailedEvent.model_validate(
            {"idempotency_key": "j:1", "job_name": "j;rm", "build_number": 1}
        )
    with pytest.raises(ValidationError):
        PipelineFailedEvent.model_validate(
            {"idempotency_key": "j:1", "job_name": "j", "build_number": 0}
        )


def test_settings_from_env_and_validation() -> None:
    s = Settings.from_env(
        {"FIX_THRESHOLD": "0.9", "MAX_LLM_CALLS_PER_RUN": "7", "MAX_ATTEMPTS": "5"}
    )
    assert (s.fix_threshold, s.max_llm_calls, s.max_attempts) == (0.9, 7, 5)
    for bad in (
        {"FIX_THRESHOLD": "1.5"},
        {"FIX_THRESHOLD": "abc"},
        {"MAX_LLM_CALLS_PER_RUN": "0"},
        {"RUN_TIMEOUT_SECONDS": "0"},
    ):
        with pytest.raises(ValueError):
            Settings.from_env(bad)


def test_knowledge_settings() -> None:
    s = Settings.from_env(
        {"DATABASE_URL": "postgresql://x/y", "EMBEDDER": "hash", "EMBEDDING_DIM": "256"}
    )
    assert (s.database_url, s.embedder, s.embedding_dim) == ("postgresql://x/y", "hash", 256)
    assert Settings.from_env({}).database_url is None
    with pytest.raises(ValueError):
        Settings.from_env({"EMBEDDER": "magic"})


def test_gemini_names_get_a_client_that_retries_transient_errors() -> None:
    from google.adk.models.google_llm import Gemini
    from pipeline_doctor_agent.fake_llm import ScriptedLlm
    from pipeline_doctor_agent.models import resolve_model

    m = resolve_model("gemini-3.8-flash")
    assert isinstance(m, Gemini) and m.model == "gemini-3.8-flash"
    assert m.retry_options is not None and m.retry_options.attempts == 4
    assert {429, 503} <= set(m.retry_options.http_status_codes or [])
    fake = ScriptedLlm(scripts={})
    assert resolve_model(fake) is fake  # a ready instance (tests) is used as is


def test_the_schema_sent_to_the_model_has_no_additional_properties_but_validation_stays_strict() -> (
    None
):
    import json

    import pytest
    from pipeline_doctor_agent.schemas import CodeFindings, Diagnosis
    from pydantic import ValidationError

    for model in (Diagnosis, CodeFindings):
        assert "additionalProperties" not in json.dumps(
            model.model_json_schema()
        )  # incl. nested models
    good = {
        "category": "code",
        "root_cause": "x" * 10,
        "evidence": ["evidence line"],
        "suspect_commit": None,
        "confidence": 0.5,
        "fixable": False,
    }
    Diagnosis.model_validate(good)
    with pytest.raises(ValidationError):
        Diagnosis.model_validate(
            {**good, "invented_field": 1}
        )  # a drifting model is still rejected
