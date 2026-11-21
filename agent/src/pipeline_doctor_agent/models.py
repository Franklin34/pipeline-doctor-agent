"""Model selection: Gemini through ADK by default, other providers through LiteLLM."""

from __future__ import annotations

from google.adk.models import BaseLlm


def resolve_model(spec: str | BaseLlm) -> str | BaseLlm:
    """``"gemini-2.5-flash"`` passes through (ADK resolves it); ``"litellm/<provider>/<model>"``
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
    return spec
