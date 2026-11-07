"""Manually verify the configured Gemini planner without launching a browser."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.browser.snapshot import PageSnapshot
from app.config import get_settings
from app.planner.gemini import GeminiProvider
from app.planner.provider import (
    InternalPlanningInput,
    SAFE_PROVIDER_DIAGNOSTIC_CATEGORIES,
    SAFE_PROVIDER_REASON_CODES,
)
from app.planner.service import (
    PlannerService,
    PlanningProviderError,
    PlanningValidationError,
)


def _format_action(action: object, index: int) -> str:
    action_type = getattr(action, "type", "unknown").upper()
    details = [action_type]

    target = getattr(action, "target", None)
    if target is not None:
        description = f'{target.kind} {target.value!r}'
        if target.role:
            description += f" (role {target.role!r})"
        details.append(f"target {description}")

    if action_type == "FILL":
        details.append("using a test-data reference" if getattr(action, "value_ref", None) else "literal value omitted")
    elif action_type == "WAIT":
        details.append(f"{action.milliseconds} ms")
    elif action_type == "ASSERT_TEXT":
        details.append("text assertion")
    elif action_type == "NAVIGATE":
        details.append("destination omitted")

    return f"{index}. " + " — ".join(details)


async def _run_smoke_test() -> int:
    try:
        settings = get_settings()
    except Exception:
        print("Could not load Gemini configuration. Check the non-secret settings.")
        return 2

    if not settings.gemini_api_key.strip():
        print("Gemini is not configured: GEMINI_API_KEY is missing.")
        return 2

    snapshot = PageSnapshot(
        url="https://example.test/",
        title="Example Account Portal",
        visible_text="Welcome. Select Login to continue.",
        elements=[
            {
                "kind": "button",
                "role": "button",
                "accessible_name": "Login",
                "text": "Login",
            },
        ],
    )
    request = InternalPlanningInput(
        instruction="Click the Login button and verify that the login form is displayed.",
        snapshot=snapshot,
        test_data={"smoke_email": "smoke.user@example.test"},
    )

    provider = GeminiProvider(settings=settings)
    plan = None
    failure_message: str | None = None
    cleanup_failed = False
    try:
        plan = await PlannerService(provider).plan(request)
    except PlanningValidationError:
        failure_message = (
            "Gemini returned a plan that did not pass the canonical action-plan validation."
        )
    except PlanningProviderError as exc:
        # These are fixed, structured fields only; never display exception text.
        failure_message = "The Gemini planner request failed."
        category = getattr(exc, "diagnostic_category", None)
        status = getattr(exc, "status_code", None)
        reason = getattr(exc, "reason_code", None)
        diagnostics = []
        if isinstance(category, str) and category in SAFE_PROVIDER_DIAGNOSTIC_CATEGORIES:
            diagnostics.append(f"category: {category}")
        if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
            diagnostics.append(f"HTTP status: {status}")
        if isinstance(reason, str) and reason in SAFE_PROVIDER_REASON_CODES:
            diagnostics.append(f"provider reason: {reason}")
        if diagnostics:
            failure_message += " (" + ", ".join(diagnostics) + ")"
    except Exception:
        failure_message = "The smoke test encountered an unexpected local error."
    finally:
        try:
            await provider.aclose()
        except PlannerProviderError:
            cleanup_failed = True

    if cleanup_failed:
        failure_message = "The Gemini client could not be closed cleanly."
    if failure_message is not None:
        print(f"SMOKE TEST FAILED: {failure_message}")
        return 1

    assert plan is not None
    print("Gemini planner smoke test succeeded: a canonical plan was generated.")
    print(f"Goal: {plan.goal}")
    print("Actions:")
    for index, action in enumerate(plan.actions, start=1):
        print(f"  {_format_action(action, index)}")
    print("No browser was launched and no website was visited.")
    return 0


def main() -> None:
    try:
        exit_code = asyncio.run(_run_smoke_test())
    except KeyboardInterrupt:
        print("Smoke test interrupted.")
        exit_code = 130
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
