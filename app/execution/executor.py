"""Execute only canonical, explicitly supported browser actions."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from pydantic import TypeAdapter, ValidationError

from app.api.schemas import (
    Action,
    ClickAction,
    ExecutionStepResult,
    FillAction,
    NavigateAction,
    SelectAction,
    WaitAction,
)
from app.browser.locator import (
    DynamicLocatorResolver,
    FillLookupDiagnostics,
    LocatorResolutionError,
)
from app.browser.snapshot import PageSnapshot
from app.security.url_policy import URLPolicyError


_ACTION_ADAPTER = TypeAdapter(Action)


def _playwright_action_error_category(exc: Exception) -> str | None:
    """Classify only known Playwright infrastructure failures, without exposing text."""

    try:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    except ImportError:
        return None

    if isinstance(exc, PlaywrightTimeoutError):
        return "timeout"
    if not isinstance(exc, PlaywrightError):
        return None
    if type(exc).__name__ == "TargetClosedError":
        return "closed_page"

    # Match a short list of known Playwright diagnostics for categorization only.
    # The raw message is never returned, logged, or included in a chained error.
    message = getattr(exc, "message", "")
    if not isinstance(message, str):
        return "browser_action"
    normalized = message.casefold()
    if any(phrase in normalized for phrase in (
        "element is not attached to the dom",
        "element is detached from the dom",
        "element has been detached",
    )):
        return "detached_element"
    if any(phrase in normalized for phrase in (
        "target closed",
        "page has been closed",
        "browser has been closed",
        "browser context has been closed",
    )):
        return "closed_page"
    if any(phrase in normalized for phrase in (
        "protocol error",
        "failed to send message to driver",
        "connection closed",
    )):
        return "infrastructure"
    return "browser_action"


_SAFE_ACTION_FAILURE_MESSAGES = {
    "timeout": "The browser action timed out.",
    "detached_element": "The selected page element detached before the action completed.",
    "closed_page": "The browser page or context closed during the action.",
    "infrastructure": "The browser could not complete the action reliably.",
    "browser_action": "The browser action could not be completed.",
}


class ActionExecutionError(RuntimeError):
    """Value-free action failure that is safe to include in a run report."""

    def __init__(self, category: str, message: str, *, fill_diagnostic: Any = None) -> None:
        super().__init__(message)
        self.category = category
        self.fill_diagnostic = fill_diagnostic


class SafeActionExecutor:
    """Run a bounded number of schema-validated actions against one page."""

    def __init__(
        self,
        page: Any,
        *,
        action_timeout_seconds: float = 35.0,
        max_actions: int = 50,
        locator_resolver: DynamicLocatorResolver | None = None,
    ) -> None:
        if action_timeout_seconds <= 0 or max_actions <= 0:
            raise ValueError("Execution limits must be positive")
        self.page = page
        self.action_timeout_seconds = action_timeout_seconds
        self.max_actions = max_actions
        self.executed_actions = 0
        self.locator_resolver = locator_resolver or DynamicLocatorResolver(page)

    async def execute(
        self,
        action: Action | dict[str, object],
        snapshot: PageSnapshot,
        test_data: Mapping[str, str],
        *,
        index: int,
    ) -> ExecutionStepResult:
        """Validate and execute one action; assertions belong to the verifier."""

        try:
            validated = _ACTION_ADAPTER.validate_python(action)
        except ValidationError:
            raise ActionExecutionError("invalid_action", "Action failed schema validation") from None
        if not isinstance(validated, (ClickAction, FillAction, SelectAction, NavigateAction, WaitAction)):
            raise ActionExecutionError(
                "unsupported_action", "Assertion actions are handled by the independent verifier"
            )
        if self.executed_actions >= self.max_actions:
            raise ActionExecutionError("action_limit", "The run reached its action limit")

        self.executed_actions += 1
        fill_lookup = FillLookupDiagnostics() if isinstance(validated, FillAction) else None
        try:
            await asyncio.wait_for(
                self._perform(validated, snapshot, test_data, fill_lookup=fill_lookup),
                timeout=self.action_timeout_seconds,
            )
        except asyncio.TimeoutError:
            if fill_lookup is not None:
                fill_lookup.reason_code = (
                    "action_error" if fill_lookup.reason_code == "resolved"
                    else fill_lookup.reason_code or "locator_error"
                )
            raise ActionExecutionError(
                "timeout",
                "The browser action timed out",
                fill_diagnostic=(fill_lookup.to_model() if fill_lookup is not None else None),
            ) from None
        except LocatorResolutionError as exc:
            if fill_lookup is not None and fill_lookup.reason_code is None:
                fill_lookup.reason_code = "locator_error"
            diagnostic = fill_lookup.to_model() if fill_lookup is not None else None
            raise ActionExecutionError(
                exc.category, str(exc), fill_diagnostic=diagnostic
            ) from None
        except URLPolicyError:
            raise ActionExecutionError("unsafe_navigation", "Navigation was rejected by URL policy") from None
        except ActionExecutionError:
            raise
        except Exception as exc:
            # Playwright errors can include selectors, values, or URLs; never forward them.
            category = _playwright_action_error_category(exc) or "browser_action"
            if fill_lookup is not None and fill_lookup.reason_code == "resolved":
                fill_lookup.reason_code = "action_error"
            raise ActionExecutionError(
                category,
                _SAFE_ACTION_FAILURE_MESSAGES[category],
                fill_diagnostic=(fill_lookup.to_model() if fill_lookup is not None else None),
            ) from None

        return ExecutionStepResult(
            index=index,
            action_type=validated.type,
            status="PASS",
            explanation="Action completed successfully.",
        )

    async def _perform(
        self,
        action: ClickAction | FillAction | SelectAction | NavigateAction | WaitAction,
        snapshot: PageSnapshot,
        test_data: Mapping[str, str],
        *,
        fill_lookup: FillLookupDiagnostics | None = None,
    ) -> None:
        if isinstance(action, NavigateAction):
            from app.browser.manager import navigate_page

            await navigate_page(self.page, str(action.url))
            return
        if isinstance(action, WaitAction):
            await asyncio.sleep(action.milliseconds / 1_000)
            return

        value: str | None = None
        if isinstance(action, FillAction):
            value = action.value
            if action.value_ref is not None:
                if action.value_ref not in test_data:
                    raise ActionExecutionError(
                        "missing_test_data", "A referenced test-data value is unavailable"
                    )
                value = test_data[action.value_ref]

        if isinstance(action, ClickAction):
            await self.locator_resolver.perform(
                action.target, snapshot, lambda locator: locator.click()
            )
        elif isinstance(action, FillAction):
            async def fill(locator: Any) -> None:
                input_type = await locator.get_attribute("type")
                if input_type and input_type.lower() == "password" and action.value_ref is None:
                    raise ActionExecutionError(
                        "sensitive_value_ref_required",
                        "Password fields require a referenced test-data value",
                    )
                # Never include this value in a result, exception, or log.
                await locator.fill(value or "")

            await self.locator_resolver.perform(
                action.target,
                snapshot,
                fill,
                require_editable=True,
                allow_truncated_live_lookup=True,
                fill_diagnostics=fill_lookup,
            )
        elif isinstance(action, SelectAction):
            await self.locator_resolver.perform(
                action.target,
                snapshot,
                lambda locator: locator.select_option(label=action.option),
            )
