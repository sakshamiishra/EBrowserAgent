"""Manually compare Anthropic structured output with a simpler plan schema.

This is a diagnostic candidate only. It does not replace the production schema.
When manually run, it sends one request with synthetic page data and retries off.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.api.schemas import StructuredActionPlan
from app.browser.snapshot import PageSnapshot
from app.config import get_settings
from app.planner.anthropic import (
    ANTHROPIC_MAX_OUTPUT_TOKENS,
    ANTHROPIC_PLAN_SCHEMA as CANDIDATE_PLAN_SCHEMA,
    SYSTEM_INSTRUCTIONS,
    _classify_anthropic_error,
)
from app.planner.prompts import build_planning_input, build_provider_request
from app.planner.provider import InternalPlanningInput


MAX_DIAGNOSTIC_OUTPUT_TOKENS = ANTHROPIC_MAX_OUTPUT_TOKENS


async def _run_diagnostic() -> int:
    try:
        settings = get_settings()
    except Exception:
        print("Result: FAILURE")
        print("HTTP status: unavailable")
        print("Error classification: configuration_error")
        print("Canonical plan validates: unavailable")
        return 2
    if not settings.anthropic_api_key.strip():
        print("Result: FAILURE")
        print("HTTP status: unavailable")
        print("Error classification: configuration_error")
        print("Canonical plan validates: unavailable")
        return 2

    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        print("Result: FAILURE")
        print("HTTP status: unavailable")
        print("Error classification: unknown_provider_error")
        print("Canonical plan validates: unavailable")
        return 2

    client = None
    status: int | None = None
    category: str | None = None
    request_succeeded = False
    plan_valid = False
    cleanup_failed = False
    try:
        try:
            client = AsyncAnthropic(
                api_key=settings.anthropic_api_key,
                timeout=settings.anthropic_request_timeout_seconds,
                max_retries=0,
            )
            synthetic_request = InternalPlanningInput(
                instruction="Check that the page displays the word READY.",
                snapshot=PageSnapshot(
                    url="https://example.test/",
                    title="Synthetic diagnostic page",
                    visible_text="READY",
                    elements=[],
                ),
            )
            planning_input = build_planning_input(
                build_provider_request(synthetic_request)
            )
            response = await asyncio.wait_for(
                client.messages.create(
                    model=settings.anthropic_model,
                    max_tokens=MAX_DIAGNOSTIC_OUTPUT_TOKENS,
                    system=SYSTEM_INSTRUCTIONS,
                    messages=[{"role": "user", "content": planning_input}],
                    output_config={
                        "effort": "low",
                        "format": {
                            "type": "json_schema",
                            "schema": CANDIDATE_PLAN_SCHEMA,
                        },
                    },
                ),
                timeout=settings.anthropic_request_timeout_seconds,
            )
            status = 200
            request_succeeded = True
            text = "".join(
                block.text
                for block in getattr(response, "content", [])
                if getattr(block, "type", None) == "text"
                and isinstance(getattr(block, "text", None), str)
            )
            try:
                StructuredActionPlan.model_validate_json(text)
                plan_valid = True
            except Exception:
                category = "response_validation_error"
        except Exception as exc:
            category, status, _reason = _classify_anthropic_error(exc)
    finally:
        if client is not None:
            try:
                await asyncio.wait_for(
                    client.close(),
                    timeout=settings.cleanup_grace_seconds,
                )
            except Exception:
                cleanup_failed = True

    success = request_succeeded and plan_valid and not cleanup_failed
    print(f"Result: {'SUCCESS' if success else 'FAILURE'}")
    print(f"Configured model: {settings.anthropic_model}")
    print(f"HTTP status: {status if status is not None else 'unavailable'}")
    print(f"Error classification: {category or 'none'}")
    print(f"Canonical plan validates: {'yes' if plan_valid else 'no'}")
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
        print("Canonical plan validates: unavailable")
        raise SystemExit(130)
