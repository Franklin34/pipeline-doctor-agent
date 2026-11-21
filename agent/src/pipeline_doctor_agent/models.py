"""Model selection: Gemini through ADK by default, other providers through LiteLLM."""

from __future__ import annotations

from google.adk.models import BaseLlm
from google.adk.models.google_llm import Gemini
from google.genai import types

# A provider hiccup (503 "high demand", 429, a gateway error) must not sink a whole run: retry the
# single model call with exponential backoff before the failure reaches the queue's retry.
RETRY = types.HttpRetryOptions(
    attempts=4,
    initial_delay=2.0,
    max_delay=30.0,
    exp_base=2.0,
    jitter=1.0,
    http_status_codes=[429, 500, 502, 503, 504],
)


def resolve_model(spec: str | BaseLlm) -> BaseLlm:
    """``"gemini-3.8-flash"`` becomes a Gemini client that retries transient errors; ``"litellm/<provider>/<model>"``
    uses ADK's LiteLLM integration; a ready ``BaseLlm`` instance (tests) is used as is."""
    if isinstance(spec, BaseLlm):
        return spec
    if spec.startswith("litellm/"):
        try:
            from google.adk.models.lite_llm import LiteLlm
        except ImportError as exc:  # pragma: no cover - depends on optional install
            raise RuntimeError(
                "LiteLLM models need the optional 'litellm' package: uv add litellm"
            ) from exc
        return LiteLlm(model=spec.removeprefix("litellm/"))
    return Gemini(model=spec, retry_options=RETRY)
