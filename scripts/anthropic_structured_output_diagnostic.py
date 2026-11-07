"""Manually check Anthropic JSON output with a deliberately minimal schema.

This diagnostic uses synthetic content and makes one request only when run.
It does not use PlannerService or the production action-plan schema.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import get_settings
from app.planner.anthropic import _classify_anthropic_error


DIAGNOSTIC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"result": {"type": "string"}},
    "required": ["result"],
    "additionalProperties": False,
}
SYSTEM_INSTRUCTION = "Return only a JSON object matching the supplied schema."
USER_INSTRUCTION = 'Return a JSON object with result set to "READY".'
MAX_OUTPUT_TOKENS = 64


def _validates_minimal_schema(content: object) -> bool:
    if not isinstance(content, list):
        return False
    text = "".join(
        block.text
        for block in content
        if getattr(block, "type", None) == "text"
        and isinstance(getattr(block, "text", None), str)
    )
    if not text:
        return False
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    return (
        isinstance(value, dict)
        and set(value) == {"result"}
        and isinstance(value["result"], str)
    )


async def _run_diagnostic() -> int:
    try:
        settings = get_settings()
    except Exception:
        print("Result: FAILURE")
        print("HTTP status: unavailable")
        print("Error classification: configuration_error")
        print("Returned JSON validates: unavailable")
        return 2

    if not settings.anthropic_api_key.strip():
        print("Result: FAILURE")
        print("HTTP status: unavailable")
        print("Error classification: configuration_error")
        print("Returned JSON validates: unavailable")
        return 2

    client = None
    request_succeeded = False
    schema_valid = False
    http_status: int | None = None
    error_category: str | None = None
    cleanup_failed = False
    try:
        try:
            from anthropic import AsyncAnthropic

            client = AsyncAnthropic(
                api_key=settings.anthropic_api_key,
                timeout=settings.anthropic_request_timeout_seconds,
                max_retries=0,
            )
            response = await asyncio.wait_for(
                client.messages.create(
                    model=settings.anthropic_model,
                    max_tokens=MAX_OUTPUT_TOKENS,
                    system=SYSTEM_INSTRUCTION,
                    messages=[{"role": "user", "content": USER_INSTRUCTION}],
                    output_config={
                        "effort": "low",
                        "format": {
                            "type": "json_schema",
                            "schema": DIAGNOSTIC_SCHEMA,
                        },
                    },
                ),
                timeout=settings.anthropic_request_timeout_seconds,
            )
            # A successful non-streaming Messages API response has HTTP 200.
            http_status = 200
            request_succeeded = True
            schema_valid = _validates_minimal_schema(
                getattr(response, "content", None)
            )
            if not schema_valid:
                error_category = "response_validation_error"
        except Exception as exc:
            error_category, http_status, _reason = _classify_anthropic_error(exc)
    finally:
        if client is not None:
            try:
                await asyncio.wait_for(
                    client.close(),
                    timeout=settings.cleanup_grace_seconds,
                )
            except Exception:
                cleanup_failed = True

    success = request_succeeded and schema_valid and not cleanup_failed
    print(f"Result: {'SUCCESS' if success else 'FAILURE'}")
    print(f"Configured model: {settings.anthropic_model}")
    print(f"HTTP status: {http_status if http_status is not None else 'unavailable'}")
    print(f"Error classification: {error_category or 'none'}")
    print(f"Returned JSON validates: {'yes' if schema_valid else 'no'}")
    if cleanup_failed:
        print("Client cleanup: failed")
    return 0 if success else 1


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(_run_diagnostic()))
    except KeyboardInterrupt:
        print("Result: FAILURE")
        print("HTTP status: unavailable")
        print("Error classification: timeout")
        print("Returned JSON validates: unavailable")
        raise SystemExit(130)
