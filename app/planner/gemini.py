"""Gemini implementation of the replaceable planning-provider interface."""

from __future__ import annotations

import asyncio
import re
import socket
from typing import Any

from app.config import Settings, get_settings
from app.planner.prompts import SYSTEM_INSTRUCTIONS, build_planning_input
from app.planner.provider import (
    PlannerProviderError,
    PlannerProviderResponse,
    ProviderPlanningRequest,
    SAFE_PROVIDER_REASON_CODES,
)


# The provider schema intentionally avoids Pydantic discriminated unions,
# HttpUrl and custom validators. Gemini's JSON Schema support is a subset; the
# canonical StructuredActionPlan is always applied after generation by service.py.
GEMINI_PLAN_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "version": {"type": "INTEGER", "enum": [1]},
        "goal": {"type": "STRING"},
        "actions": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "type": {
                        "type": "STRING",
                        "enum": [
                            "navigate", "click", "fill", "select", "wait",
                            "assert_visible", "assert_text", "assert_url",
                        ],
                    },
                    "url": {"type": "STRING"},
                    "target": {
                        "type": "OBJECT",
                        "properties": {
                            "kind": {
                                "type": "STRING",
                                "enum": ["role", "label", "placeholder", "text", "attribute", "css"],
                            },
                            "value": {"type": "STRING"},
                            "role": {"type": "STRING"},
                        },
                        "required": ["kind", "value"],
                    },
                    "value": {"type": "STRING"},
                    "value_ref": {"type": "STRING"},
                    "option": {"type": "STRING"},
                    "milliseconds": {"type": "INTEGER"},
                    "expected_text": {"type": "STRING"},
                    "expected_url": {"type": "STRING"},
                    "required": {"type": "BOOLEAN"},
                    "exact": {"type": "BOOLEAN"},
                },
                "required": ["type"],
            },
        },
    },
    "required": ["version", "goal", "actions"],
}


def _structured_value(obj: object, name: str) -> object | None:
    """Read a structured SDK attribute without touching exception text/body."""

    try:
        return getattr(obj, name, None)
    except Exception:
        return None


def _http_status(exc: Exception) -> int | None:
    candidates = [
        _structured_value(exc, "status_code"),
        _structured_value(exc, "code"),
        _structured_value(_structured_value(exc, "response"), "status_code"),
    ]
    for candidate in candidates:
        if isinstance(candidate, int) and not isinstance(candidate, bool) and 100 <= candidate <= 599:
            return candidate
        if isinstance(candidate, str) and candidate.isdecimal():
            value = int(candidate)
            if 100 <= value <= 599:
                return value
    return None


def _safe_reason(exc: Exception, api_key: str) -> str | None:
    """Expose only a small allow-list of structured provider reason codes."""

    for name in ("reason", "error_code", "code", "status"):
        value = _structured_value(exc, name)
        if not isinstance(value, str):
            value = _structured_value(value, "name") if value is not None else None
        if not isinstance(value, str):
            continue
        normalized = value.upper()
        if (
            normalized in SAFE_PROVIDER_REASON_CODES
            and (not api_key or api_key.casefold() not in normalized.casefold())
            and re.fullmatch(r"[A-Z_]{2,40}", normalized)
        ):
            return normalized
    return None


def _classify_provider_exception(
    exc: Exception, api_key: str
) -> tuple[str, int | None, str | None]:
    """Classify by status/type; never use the provider's free-form message."""

    status = _http_status(exc)
    if status == 408:
        category = "timeout"
    elif status in (401, 403):
        category = "authentication_authorization"
    elif status == 429:
        category = "rate_limit_quota"
    elif status in (400, 404):
        category = "invalid_request_or_model"
    elif status is not None and 500 <= status <= 599:
        category = "server_error"
    elif isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or any(
        token in type(exc).__name__.casefold() for token in ("timeout", "timedout")
    ):
        category = "timeout"
    elif isinstance(exc, (ConnectionError, OSError, socket.gaierror)) or any(
        token in type(exc).__name__.casefold()
        for token in ("connection", "connecterror", "network", "socket", "transport")
    ):
        category = "network_connectivity"
    else:
        category = "unknown_provider_error"
    return category, status, _safe_reason(exc, api_key)


class GeminiProvider:
    """Generate plan JSON using Google's asynchronous Gen AI SDK."""

    def __init__(self, settings: Settings | None = None, client: Any | None = None) -> None:
        self._settings = settings
        self._client = client

    async def generate_plan(self, request: ProviderPlanningRequest) -> PlannerProviderResponse:
        if (
            not isinstance(request, ProviderPlanningRequest)
            or not request.is_sanitization_verified
        ):
            raise PlannerProviderError(
                "Gemini request contents failed sanitizer integrity verification"
            )
        settings = self._settings or get_settings()
        if not settings.gemini_api_key.strip():
            raise PlannerProviderError("Gemini API key is not configured")
        if self._client is None:
            try:
                from google import genai
            except ImportError as exc:
                raise PlannerProviderError(
                    "Gemini provider requires the google-genai package"
                ) from exc
            try:
                self._client = genai.Client(api_key=settings.gemini_api_key)
            except Exception as exc:
                category, status, reason = _classify_provider_exception(
                    exc, settings.gemini_api_key
                )
                raise PlannerProviderError(
                    "Gemini client initialization failed",
                    diagnostic_category=category,
                    status_code=status,
                    reason_code=reason,
                ) from None

        try:
            response = await asyncio.wait_for(
                self._client.aio.models.generate_content(
                    model=settings.gemini_model,
                    contents=build_planning_input(request),
                    config={
                        "system_instruction": SYSTEM_INSTRUCTIONS,
                        "response_mime_type": "application/json",
                        "response_json_schema": GEMINI_PLAN_SCHEMA,
                        "max_output_tokens": settings.gemini_max_output_tokens,
                    },
                ),
                timeout=settings.gemini_request_timeout_seconds,
            )
        except asyncio.TimeoutError:
            raise PlannerProviderError(
                "Gemini planning request timed out",
                diagnostic_category="timeout",
            ) from None
        except Exception as exc:
            category, status, reason = _classify_provider_exception(
                exc, settings.gemini_api_key
            )
            message = (
                "Gemini planning request timed out"
                if category == "timeout"
                else "Gemini planning request failed"
            )
            raise PlannerProviderError(
                message,
                diagnostic_category=category,
                status_code=status,
                reason_code=reason,
            ) from None

        prompt_feedback = getattr(response, "prompt_feedback", None)
        if getattr(prompt_feedback, "block_reason", None):
            raise PlannerProviderError("Gemini refused or blocked the planning request")
        candidates = getattr(response, "candidates", None)
        if not candidates:
            raise PlannerProviderError("Gemini returned no completed planning candidate")
        finish_reason = getattr(candidates[0], "finish_reason", None)
        finish_value = getattr(finish_reason, "value", finish_reason)
        if finish_value != "STOP":
            raise PlannerProviderError(
                "Gemini returned a blocked, incomplete, or unsuccessful planning response"
            )

        content = getattr(response, "text", None)
        if not isinstance(content, str) or not content.strip():
            raise PlannerProviderError("Gemini returned an empty planning response")
        if len(content) > 32_000:
            raise PlannerProviderError("Gemini response exceeded the supported output size")
        return PlannerProviderResponse(content=content)

    async def aclose(self) -> None:
        """Close SDK resources when the application shuts down the provider."""

        if self._client is None:
            return
        try:
            async_client = getattr(self._client, "aio", None)
            close = getattr(async_client, "aclose", None)
            if close is not None:
                await close()
        except Exception:
            raise PlannerProviderError("Gemini client cleanup failed") from None
