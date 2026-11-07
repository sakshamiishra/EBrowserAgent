"""Anthropic Claude adapter for the provider-neutral planning interface."""

from __future__ import annotations

import asyncio
import re
import socket
from collections.abc import Mapping
from typing import Any

from app.config import Settings, get_settings
from app.planner.prompts import SYSTEM_INSTRUCTIONS, build_planning_input
from app.planner.provider import (
    PlannerProviderError,
    PlannerProviderResponse,
    ProviderPlanningRequest,
)


ANTHROPIC_MAX_OUTPUT_TOKENS = 4_096

# Keep this schema within Anthropic's structured-output JSON Schema subset.
# Canonical Pydantic validation remains authoritative for role/kind invariants,
# field bounds, URL safety, wait limits, and action ordering.
def _action_variant(
    action_type: str,
    properties: dict[str, Any],
) -> dict[str, Any]:
    all_properties = {"type": {"type": "string", "enum": [action_type]}, **properties}
    return {
        "type": "object",
        "properties": all_properties,
        "required": list(all_properties),
        "additionalProperties": False,
    }


ANTHROPIC_PLAN_SCHEMA: dict[str, Any] = {
    "definitions": {
        "target": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["role", "label", "placeholder", "text", "attribute", "css"],
                },
                "value": {"type": "string"},
                # Omission becomes role=None; canonical Pydantic validation
                # enforces role for role targets and forbids it otherwise.
                "role": {"type": "string"},
            },
            "required": ["kind", "value"],
            "additionalProperties": False,
        },
    },
    "type": "object",
    "properties": {
        "version": {"type": "integer", "enum": [1]},
        "goal": {"type": "string"},
        "actions": {
            "type": "array",
            "minItems": 1,
            "items": {
                "anyOf": [
                    _action_variant("navigate", {"url": {"type": "string"}}),
                    _action_variant("click", {"target": {"$ref": "#/definitions/target"}}),
                    _action_variant("fill", {
                        "target": {"$ref": "#/definitions/target"},
                        "value": {"type": "string"},
                        "value_ref": {"type": "null"},
                    }),
                    _action_variant("fill", {
                        "target": {"$ref": "#/definitions/target"},
                        "value": {"type": "null"},
                        "value_ref": {"type": "string"},
                    }),
                    _action_variant("select", {
                        "target": {"$ref": "#/definitions/target"},
                        "option": {"type": "string"},
                    }),
                    _action_variant("wait", {"milliseconds": {"type": "integer"}}),
                    _action_variant("assert_visible", {
                        "target": {"$ref": "#/definitions/target"},
                        "required": {"type": "boolean"},
                    }),
                    _action_variant("assert_text", {
                        "expected_text": {"type": "string"},
                        "target": {
                            "anyOf": [
                                {"$ref": "#/definitions/target"},
                                {"type": "null"},
                            ],
                        },
                        "required": {"type": "boolean"},
                    }),
                    _action_variant("assert_url", {
                        "expected_url": {"type": "string"},
                        "exact": {"type": "boolean"},
                        "required": {"type": "boolean"},
                    }),
                ]
            },
        },
    },
    "required": ["version", "goal", "actions"],
    "additionalProperties": False,
}


_REASON_BY_STATUS = {
    400: "INVALID_ARGUMENT",
    401: "UNAUTHENTICATED",
    403: "PERMISSION_DENIED",
    404: "NOT_FOUND",
    408: "DEADLINE_EXCEEDED",
    429: "RESOURCE_EXHAUSTED",
}

_SAFE_ERROR_TYPES = frozenset({
    "invalid_request_error",
    "authentication_error",
    "billing_error",
    "permission_error",
    "not_found_error",
    "conflict_error",
    "request_too_large",
    "rate_limit_error",
    "api_error",
    "timeout_error",
    "overloaded_error",
})
# Anthropic documents this Messages API details.error_code. Other endpoint- or
# feature-specific codes are deliberately suppressed until explicitly reviewed.
_SAFE_ERROR_CODES = frozenset({"enforced_spend_limit_reached"})
_SAFE_REQUEST_ID_RE = re.compile(r"req_[A-Za-z0-9_-]{1,96}")
SAFE_SUPPRESSED_PROVIDER_MESSAGE = (
    "Provider error details suppressed to protect planner input."
)


def _safe_status(exc: Exception) -> int | None:
    """Extract only a valid numeric HTTP status from structured SDK fields."""

    candidates: list[object] = []
    try:
        response = getattr(exc, "response", None)
    except Exception:
        response = None
    for owner, name in ((exc, "status_code"), (response, "status_code")):
        try:
            candidates.append(getattr(owner, name, None))
        except Exception:
            continue
    for value in candidates:
        if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
            return value
    return None


def _classify_anthropic_error(
    exc: Exception,
) -> tuple[str, int | None, str | None]:
    """Map SDK exception metadata to the shared safe diagnostic allow-lists."""

    status = _safe_status(exc)
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
    elif status is not None and 400 <= status <= 499:
        category = "invalid_request_or_model"
    else:
        name = type(exc).__name__.casefold()
        if "timeout" in name or "timedout" in name:
            category = "timeout"
        elif (
            isinstance(exc, (ConnectionError, OSError, socket.gaierror))
            or any(token in name for token in ("connection", "connecterror", "network", "transport"))
        ):
            category = "network_connectivity"
        else:
            category = "unknown_provider_error"

    reason = _REASON_BY_STATUS.get(status)
    if reason is None and status is not None and 500 <= status <= 599:
        reason = "UNAVAILABLE"
    return category, status, reason


def _safe_string_field(owner: object, field: str) -> str | None:
    try:
        value = getattr(owner, field, None)
    except Exception:
        return None
    return value if isinstance(value, str) else None


def _mapping_field(owner: Mapping[object, object], field: str) -> object | None:
    try:
        return owner.get(field)
    except Exception:
        return None


def _allowlisted_error_value(value: object, allowlist: frozenset[str]) -> str | None:
    """Keep only documented provider metadata; never echo unknown strings."""

    if value is None:
        return None
    return value if isinstance(value, str) and value in allowlist else "unknown"


def _error_details(exc: Exception) -> tuple[object | None, object | None, str | None, str | None]:
    """Read only the typed fields in Anthropic's documented error envelope."""

    try:
        body = getattr(exc, "body", None)
    except Exception:
        body = None
    error = _mapping_field(body, "error") if isinstance(body, Mapping) else None
    error = error if isinstance(error, Mapping) else {}

    error_type = _mapping_field(error, "type")
    if error_type is None:
        error_type = _safe_string_field(exc, "type")
    details = _mapping_field(error, "details")
    error_code = (
        _mapping_field(details, "error_code") if isinstance(details, Mapping) else None
    )
    if error_code is None:
        # Missing or malformed documented metadata is represented by a fixed
        # sentinel. Never accept the undocumented top-level error.code field.
        error_code = "unknown"
    message = _mapping_field(error, "message")
    request_id = _mapping_field(body, "request_id") if isinstance(body, Mapping) else None
    if not isinstance(request_id, str):
        request_id = _safe_string_field(exc, "request_id")
    if not isinstance(request_id, str):
        request_id = _safe_string_field(exc, "_request_id")
    if not isinstance(request_id, str):
        try:
            response = getattr(exc, "response", None)
            headers = getattr(response, "headers", None)
            candidate = headers.get("request-id") if headers is not None else None
        except Exception:
            candidate = None
        request_id = candidate if isinstance(candidate, str) else None

    return (
        error_type,
        error_code,
        message if isinstance(message, str) else None,
        request_id,
    )


def _safe_provider_message(message: str | None) -> str | None:
    """Suppress provider free text because it may quote any part of the prompt.

    Structured diagnostics are preserved separately. No substring redaction can
    reliably distinguish harmless provider prose from partial planner input.
    """

    if not isinstance(message, str) or not message:
        return None
    return SAFE_SUPPRESSED_PROVIDER_MESSAGE


def _raise_provider_error(
    exc: Exception,
    *,
    operation: str,
    api_key: str = "",
    model_id: str | None = None,
) -> PlannerProviderError:
    category, status, reason = _classify_anthropic_error(exc)
    error_type, error_code, provider_message, request_id = _error_details(exc)
    message = (
        "Anthropic planning request timed out"
        if category == "timeout" and "request" in operation
        else f"Anthropic {operation} failed"
    )
    return PlannerProviderError(
        message,
        diagnostic_category=category,
        status_code=status,
        reason_code=reason,
        provider_error_type=_allowlisted_error_value(error_type, _SAFE_ERROR_TYPES),
        provider_error_code=_allowlisted_error_value(error_code, _SAFE_ERROR_CODES),
        provider_message=_safe_provider_message(provider_message),
        request_id=(
            request_id
            if isinstance(request_id, str) and _SAFE_REQUEST_ID_RE.fullmatch(request_id)
            and (not api_key or api_key not in request_id)
            else None
        ),
        model_id=(
            model_id if isinstance(model_id, str) and (not api_key or api_key not in model_id)
            else None
        ),
    )


def _field(obj: object, name: str) -> object | None:
    try:
        return getattr(obj, name, None)
    except Exception:
        return None


class AnthropicProvider:
    """Generate structured action-plan JSON through Anthropic's async SDK."""

    def __init__(self, settings: Settings | None = None, client: Any | None = None) -> None:
        self._settings = settings
        self._client = client

    async def generate_plan(self, request: ProviderPlanningRequest) -> PlannerProviderResponse:
        if (
            not isinstance(request, ProviderPlanningRequest)
            or not request.is_sanitization_verified
        ):
            raise PlannerProviderError(
                "Anthropic request contents failed sanitizer integrity verification"
            )
        settings = self._settings or get_settings()
        if not settings.anthropic_api_key.strip():
            raise PlannerProviderError("Anthropic API key is not configured")
        if not settings.anthropic_model.strip():
            raise PlannerProviderError("Anthropic model is not configured")

        if self._client is None:
            try:
                from anthropic import AsyncAnthropic
            except ImportError:
                raise PlannerProviderError(
                    "Anthropic provider requires the anthropic package"
                ) from None
            try:
                self._client = AsyncAnthropic(
                    api_key=settings.anthropic_api_key,
                    timeout=settings.anthropic_request_timeout_seconds,
                    max_retries=0,
                )
            except Exception as exc:
                raise _raise_provider_error(
                    exc,
                    operation="client initialization",
                    api_key=settings.anthropic_api_key,
                    model_id=settings.anthropic_model,
                ) from None

        planning_input = build_planning_input(request)
        try:
            response = await asyncio.wait_for(
                self._client.messages.create(
                    model=settings.anthropic_model,
                    max_tokens=ANTHROPIC_MAX_OUTPUT_TOKENS,
                    system=SYSTEM_INSTRUCTIONS,
                    messages=[{
                        "role": "user",
                        "content": planning_input,
                    }],
                    output_config={
                        "effort": "low",
                        "format": {
                            "type": "json_schema",
                            "schema": ANTHROPIC_PLAN_SCHEMA,
                        },
                    },
                ),
                timeout=settings.anthropic_request_timeout_seconds,
            )
        except asyncio.TimeoutError:
            raise PlannerProviderError(
                "Anthropic planning request timed out",
                diagnostic_category="timeout",
                model_id=settings.anthropic_model,
            ) from None
        except Exception as exc:
            raise _raise_provider_error(
                exc,
                operation="planning request",
                api_key=settings.anthropic_api_key,
                model_id=settings.anthropic_model,
            ) from None

        stop_reason = _field(response, "stop_reason")
        if stop_reason != "end_turn":
            raise PlannerProviderError(
                "Anthropic returned a refused, truncated, or incomplete planning response"
            )

        blocks = _field(response, "content")
        if not isinstance(blocks, (list, tuple)):
            raise PlannerProviderError("Anthropic returned an unsupported response format")
        text_parts: list[str] = []
        for block in blocks:
            block_type = _field(block, "type")
            if block_type == "text":
                text = _field(block, "text")
                if not isinstance(text, str):
                    raise PlannerProviderError("Anthropic returned an unsupported text response")
                text_parts.append(text)
            elif block_type not in {"thinking", "redacted_thinking"}:
                raise PlannerProviderError("Anthropic returned an unsupported response format")

        content = "".join(text_parts)
        if not content.strip():
            raise PlannerProviderError("Anthropic returned an empty planning response")
        if len(content) > 32_000:
            raise PlannerProviderError("Anthropic response exceeded the supported output size")
        return PlannerProviderResponse(content=content)

    async def aclose(self) -> None:
        """Close the async Anthropic client, normalizing errors safely."""

        if self._client is None:
            return
        try:
            close = getattr(self._client, "aclose", None)
            if close is not None:
                await close()
        except Exception:
            raise PlannerProviderError("Anthropic client cleanup failed") from None
