"""Provider-independent orchestration and canonical plan validation."""

from __future__ import annotations

import re
from pydantic import ValidationError

from app.api.schemas import AssertTextAction, ElementTarget, FillAction, StructuredActionPlan
from app.browser.snapshot import PageSnapshot
from app.browser.locator import snapshot_matching_elements, target_matches_snapshot
from app.planner.prompts import PlanningInputSanitizationError, build_provider_request
from app.planner.provider import (
    InternalPlanningInput,
    PlannerProvider,
    PlannerProviderError,
    PlannerProviderResponse,
    SAFE_PROVIDER_DIAGNOSTIC_CATEGORIES,
    SAFE_PROVIDER_REASON_CODES,
    _safe_diagnostic_message,
    _safe_diagnostic_token,
)


class PlanningError(RuntimeError):
    """Base exception for planning failures exposed to application callers."""


class PlanningProviderError(PlanningError):
    """Raised when the selected provider cannot return a usable response."""

    def __init__(
        self,
        message: str,
        *,
        category: str = "provider_failure",
        diagnostic_category: str = "unknown_provider_error",
        status_code: int | None = None,
        reason_code: str | None = None,
        provider_error_type: str | None = None,
        provider_error_code: str | None = None,
        provider_message: str | None = None,
        request_id: str | None = None,
        model_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.diagnostic_category = (
            diagnostic_category
            if isinstance(diagnostic_category, str)
            and diagnostic_category in SAFE_PROVIDER_DIAGNOSTIC_CATEGORIES
            else "unknown_provider_error"
        )
        self.status_code = (
            status_code
            if isinstance(status_code, int)
            and not isinstance(status_code, bool)
            and 100 <= status_code <= 599
            else None
        )
        self.reason_code = (
            reason_code
            if isinstance(reason_code, str) and reason_code in SAFE_PROVIDER_REASON_CODES
            else None
        )
        self.provider_error_type = _safe_diagnostic_token(
            provider_error_type, r"[a-z][a-z0-9_]{0,63}"
        )
        self.provider_error_code = _safe_diagnostic_token(
            provider_error_code, r"[A-Za-z0-9_.-]{1,64}"
        )
        self.provider_message = _safe_diagnostic_message(provider_message)
        self.request_id = _safe_diagnostic_token(request_id, r"req_[A-Za-z0-9_-]{1,96}")
        self.model_id = _safe_diagnostic_token(model_id, r"claude-[A-Za-z0-9._:-]{1,90}")


class PlanningValidationError(PlanningError):
    """Raised when provider output is not a valid canonical action plan."""


class PlannerService:
    """Use an injected provider and validate all output with the canonical model."""

    def __init__(self, provider: PlannerProvider) -> None:
        self._provider = provider

    async def plan(self, request: InternalPlanningInput) -> StructuredActionPlan:
        try:
            provider_request = build_provider_request(request)
        except (PlanningInputSanitizationError, ValueError, TypeError):
            raise PlanningValidationError(
                "Planning input could not be safely sanitized"
            ) from None

        try:
            response = await self._provider.generate_plan(provider_request)
        except PlannerProviderError as exc:
            raise PlanningProviderError(
                "Planning provider failed",
                category="provider_error",
                diagnostic_category=exc.diagnostic_category,
                status_code=exc.status_code,
                reason_code=exc.reason_code,
                provider_error_type=exc.provider_error_type,
                provider_error_code=exc.provider_error_code,
                provider_message=exc.provider_message,
                request_id=exc.request_id,
                model_id=exc.model_id,
            ) from None
        except Exception as exc:
            raise PlanningProviderError(
                "Planning provider failed",
                category=f"provider_exception:{type(exc).__name__}",
            ) from None

        if not isinstance(response, PlannerProviderResponse):
            raise PlanningProviderError(
                "Planning provider returned an invalid response type",
                category="invalid_response",
            ) from None

        try:
            plan = StructuredActionPlan.model_validate_json(response.content)
        except (ValidationError, ValueError, TypeError):
            raise PlanningValidationError(
                "Provider response did not match the supported action plan"
            ) from None

        if any(
            isinstance(action, FillAction)
            and not target_matches_snapshot(action.target, request.snapshot)
            for action in plan.actions
        ):
            raise PlanningValidationError(
                "Provider proposed a fill target not observed in the current snapshot"
            )

        fill_value_assertions = [
            (action, action.value if action.value is not None else request.test_data.get(action.value_ref or ""))
            for action in plan.actions
            if isinstance(action, FillAction)
        ]
        for assertion in plan.actions:
            if not isinstance(assertion, AssertTextAction) or assertion.target is None:
                continue
            assertion_matches = snapshot_matching_elements(assertion.target, request.snapshot)
            if len(assertion_matches) != 1 or assertion_matches[0].kind not in {"input", "textarea"}:
                continue
            matching_fills = [
                fill for fill, expected in fill_value_assertions
                if expected is not None and expected == assertion.expected_text
            ]
            if matching_fills and not any(
                len(fill_matches := snapshot_matching_elements(fill.target, request.snapshot)) == 1
                and fill_matches[0] == assertion_matches[0]
                for fill in matching_fills
            ):
                raise PlanningValidationError(
                    "Input-value assertion does not identify the corresponding filled field"
                )

        known_keys = request.test_data.keys()
        if any(
            action.value_ref not in known_keys
            for action in plan.actions
            if getattr(action, "value_ref", None) is not None
        ):
            raise PlanningValidationError(
                "Provider plan references unavailable test data"
            )
        if any(
            isinstance(action, FillAction)
            and action.value is not None
            and _targets_password_input(action.target, request.snapshot)
            for action in plan.actions
        ):
            raise PlanningValidationError(
                "Password fields must be filled with a test-data reference"
            )
        return plan


def _targets_password_input(target: ElementTarget, snapshot: PageSnapshot) -> bool:
    """Conservatively match a fill target to password metadata in the snapshot."""

    for item in snapshot.elements:
        if getattr(item, "kind", None) != "input" or (
            getattr(item, "input_type", None) or ""
        ).lower() != "password":
            continue
        value = target.value
        if target.kind == "role":
            matched = item.role == target.role and item.accessible_name == value
        elif target.kind == "label":
            matched = item.label == value or item.accessible_name == value
        elif target.kind == "placeholder":
            matched = item.placeholder == value
        elif target.kind == "text":
            matched = item.text == value or item.accessible_name == value
        elif target.kind == "attribute" and "=" in value:
            attr, expected = value.split("=", 1)
            field = {"name": "name", "data-testid": "test_id", "aria-label": "aria_label", "placeholder": "placeholder"}.get(attr.strip().lower())
            matched = bool(field and getattr(item, field) == expected)
        elif target.kind == "css":
            selector = re.fullmatch(
                r'(input)(?:\[(data-testid|name|aria-label|placeholder)="([^"\\\]\r\n]{1,160})"\])?',
                value,
            )
            if selector is None:
                matched = False
            elif selector.group(2):
                field = {"data-testid": "test_id", "name": "name", "aria-label": "aria_label", "placeholder": "placeholder"}[selector.group(2)]
                matched = getattr(item, field) == selector.group(3)
            else:
                matched = True
        else:
            matched = False
        if matched:
            return True
    return False
