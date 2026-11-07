"""Manually verify one configured Anthropic planner request without a browser."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.browser.snapshot import PageSnapshot
from app.config import get_settings
from app.planner.factory import create_planner_provider
from app.planner.provider import (
    PlannerProviderError,
    SAFE_PROVIDER_DIAGNOSTIC_CATEGORIES,
    SAFE_PROVIDER_REASON_CODES,
)
from app.planner.service import (
    PlannerService,
    PlanningProviderError,
    PlanningValidationError,
)
from app.planner.provider import InternalPlanningInput


def _safe_failure(exc: PlanningProviderError | PlannerProviderError) -> str:
    details = []
    category = getattr(exc, "diagnostic_category", None)
    status = getattr(exc, "status_code", None)
    reason = getattr(exc, "reason_code", None)
    error_type = getattr(exc, "provider_error_type", None)
    error_code = getattr(exc, "provider_error_code", None)
    provider_message = getattr(exc, "provider_message", None)
    request_id = getattr(exc, "request_id", None)
    model_id = getattr(exc, "model_id", None)
    if isinstance(category, str) and category in SAFE_PROVIDER_DIAGNOSTIC_CATEGORIES:
        details.append(f"category: {category}")
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        details.append(f"HTTP status: {status}")
    if isinstance(reason, str) and reason in SAFE_PROVIDER_REASON_CODES:
        details.append(f"provider reason: {reason}")
    if isinstance(error_type, str) and isinstance(error_code, str):
        details.append(f"provider error: {error_type}/{error_code}")
    elif isinstance(error_type, str):
        details.append(f"provider error: {error_type}")
    elif isinstance(error_code, str):
        details.append(f"provider code: {error_code}")
    if isinstance(provider_message, str) and provider_message:
        details.append(f"message: {provider_message}")
    if isinstance(request_id, str):
        details.append(f"request ID: {request_id}")
    if isinstance(model_id, str):
        details.append(f"model: {model_id}")
    return ("Anthropic planner request failed" +
            (f" ({', '.join(details)})" if details else "."))[:800]


async def _run_smoke_test() -> int:
    try:
        settings = get_settings()
    except Exception:
        print("Could not load application configuration. Check non-secret settings.")
        return 2

    if settings.ai_provider != "anthropic":
        print("Set AI_PROVIDER=anthropic to run this smoke test.")
        return 2
    if not settings.anthropic_api_key.strip():
        print("Anthropic is not configured: ANTHROPIC_API_KEY is missing.")
        return 2

    snapshot = PageSnapshot(
        url="https://example.test/",
        title="Example account portal",
        visible_text="Welcome. Select Login to continue.",
        elements=[{
            "kind": "button",
            "role": "button",
            "accessible_name": "Login",
            "text": "Login",
        }],
    )
    request = InternalPlanningInput(
        instruction="Click the Login button and verify that the login form is displayed.",
        snapshot=snapshot,
    )

    provider = None
    failure: str | None = None
    cleanup_failed = False
    try:
        provider = create_planner_provider(settings)
        await PlannerService(provider).plan(request)
    except PlanningValidationError:
        failure = "Anthropic returned a response that failed canonical plan validation."
    except (PlanningProviderError, PlannerProviderError) as exc:
        failure = _safe_failure(exc)
    except Exception:
        failure = "The smoke test encountered an unexpected local error."
    finally:
        if provider is not None:
            try:
                await provider.aclose()
            except Exception:
                cleanup_failed = True

    if failure is not None:
        print(f"SMOKE TEST FAILED: {failure}")
        if cleanup_failed:
            print("Anthropic client cleanup also failed.")
        return 1
    if cleanup_failed:
        print("SMOKE TEST FAILED: Anthropic client cleanup failed.")
        return 1

    print("Anthropic planner smoke test succeeded: a canonical plan was generated.")
    print("No browser was launched and no website was visited.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(_run_smoke_test()))
    except KeyboardInterrupt:
        print("SMOKE TEST FAILED: interrupted before completion.")
        raise SystemExit(130)
