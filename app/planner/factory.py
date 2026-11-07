"""Construct the configured planner provider without fallback."""

from __future__ import annotations

from app.config import Settings
from app.planner.anthropic import AnthropicProvider
from app.planner.gemini import GeminiProvider
from app.planner.provider import PlannerProvider, PlannerProviderError


def create_planner_provider(settings: Settings) -> PlannerProvider:
    """Return exactly the configured provider, failing safely if unavailable."""

    if settings.ai_provider == "gemini":
        return GeminiProvider(settings=settings)
    if settings.ai_provider == "anthropic":
        if not settings.anthropic_api_key.strip():
            raise PlannerProviderError("Anthropic API key is not configured")
        if not settings.anthropic_model.strip():
            raise PlannerProviderError("Anthropic model is not configured")
        return AnthropicProvider(settings=settings)
    # Settings validates this field; keep the factory fail-closed if a custom
    # Settings-like object bypasses that constructor.
    raise PlannerProviderError("Configured AI provider is unsupported")
