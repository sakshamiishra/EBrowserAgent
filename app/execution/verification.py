"""Independently check planned assertions against observed browser state."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from app.api.schemas import (
    AssertTextAction,
    AssertUrlAction,
    AssertVisibleAction,
    VerifiedAssertion,
)
from app.browser.locator import (
    AssertionLookupDiagnostics,
    DynamicLocatorResolver,
    LocatorResolutionError,
)
from app.browser.snapshot import PageSnapshot
from app.security.url_policy import validate_target_url


class AssertionVerifier:
    """Verify assertions without relying on action success or model claims."""

    def __init__(self, page: Any, resolver: DynamicLocatorResolver | None = None) -> None:
        self.page = page
        self.resolver = resolver or DynamicLocatorResolver(page)

    async def verify(
        self,
        action: AssertVisibleAction | AssertTextAction | AssertUrlAction,
        snapshot: PageSnapshot,
        *,
        index: int,
    ) -> VerifiedAssertion:
        lookup = AssertionLookupDiagnostics(snapshot_truncated=snapshot.elements_truncated)
        try:
            passed = await self._evaluate(action, snapshot, lookup)
        except LocatorResolutionError as exc:
            reason_by_category = {
                "missing": "not_found_in_complete_snapshot",
                "snapshot_truncated": "snapshot_truncated",
                "live_no_match": "live_no_match",
                "live_not_visible": "live_not_visible",
                "ambiguous": "live_ambiguous",
            }
            reason_code = lookup.reason_code or reason_by_category.get(
                exc.category, "verification_error"
            )
            status = (
                "FAIL"
                if reason_code in {
                    "not_found_in_complete_snapshot",
                    "live_not_visible",
                }
                else "INCONCLUSIVE"
            )
            explanation_by_reason = {
                "not_found_in_complete_snapshot": "The target was absent from a complete snapshot.",
                "snapshot_truncated": "The target could not be checked because the element snapshot was truncated.",
                "live_no_match": "The target had no matching live elements.",
                "live_not_visible": "Matching live elements were not visible.",
                "live_ambiguous": "The target matched multiple visible live elements.",
                "verification_error": "The assertion could not be checked reliably.",
            }
            return VerifiedAssertion(
                index=index,
                action_type=action.type,
                required=action.required,
                status=status,
                reason_code=reason_code,
                target_kind=lookup.target_kind,
                snapshot_match=lookup.snapshot_match,
                snapshot_truncated=lookup.snapshot_truncated,
                candidate_count=lookup.candidate_count,
                visible_count=lookup.visible_count,
                explanation=explanation_by_reason[reason_code],
            )
        except Exception:
            return VerifiedAssertion(
                index=index,
                action_type=action.type,
                required=action.required,
                status="INCONCLUSIVE",
                reason_code="verification_error",
                target_kind=lookup.target_kind,
                snapshot_match=lookup.snapshot_match,
                snapshot_truncated=lookup.snapshot_truncated,
                candidate_count=lookup.candidate_count,
                visible_count=lookup.visible_count,
                explanation="The assertion could not be checked reliably.",
            )
        reason_code = "verified" if passed else "condition_not_met"
        return VerifiedAssertion(
            index=index,
            action_type=action.type,
            required=action.required,
            status="PASS" if passed else "FAIL",
            reason_code=reason_code,
            target_kind=lookup.target_kind,
            snapshot_match=lookup.snapshot_match,
            snapshot_truncated=lookup.snapshot_truncated,
            candidate_count=lookup.candidate_count,
            visible_count=lookup.visible_count,
            explanation=(
                "The expected condition was verified."
                if passed
                else "The expected condition was not present in the observed page state."
            ),
        )

    async def _evaluate(
        self,
        action: AssertVisibleAction | AssertTextAction | AssertUrlAction,
        snapshot: PageSnapshot,
        lookup: AssertionLookupDiagnostics,
    ) -> bool:
        if isinstance(action, AssertVisibleAction):
            matches = await self.resolver.find_visible(
                action.target, snapshot, assertion_diagnostics=lookup
            )
            if len(matches) > 1:
                lookup.reason_code = "live_ambiguous"
                raise LocatorResolutionError(
                    "ambiguous", "Visibility assertion target matched multiple visible elements"
                )
            return bool(matches)

        if isinstance(action, AssertTextAction):
            if action.target is None:
                if action.expected_text in snapshot.visible_text:
                    return True
                if snapshot.visible_text_truncated:
                    raise RuntimeError("The observed text snapshot was truncated")
                return False
            matches = await self.resolver.find_visible(
                action.target, snapshot, assertion_diagnostics=lookup
            )
            if not matches:
                return False
            if len(matches) > 1:
                lookup.reason_code = "live_ambiguous"
                raise LocatorResolutionError(
                    "ambiguous", "Text assertion target matched multiple visible elements"
                )
            locator = matches[0]
            is_value_control = await locator.evaluate(
                "element => element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement"
            )
            if is_value_control:
                # This reads the live DOM value independently after the action.
                # The value is compared in memory and is never added to reports.
                current_value = await locator.input_value()
                return current_value == action.expected_text
            text = await locator.inner_text()
            return isinstance(text, str) and action.expected_text in text

        expected_url = await validate_target_url(str(action.expected_url))
        current_url = str(self.page.url)
        if action.exact:
            return current_url == expected_url
        expected = urlsplit(expected_url)
        current = urlsplit(current_url)
        if (
            current.scheme.lower() not in {"http", "https"}
            or not current.hostname
            or current.username is not None
            or current.password is not None
        ):
            return False
        expected_port = expected.port or (443 if expected.scheme.lower() == "https" else 80)
        current_port = current.port or (443 if current.scheme.lower() == "https" else 80)
        if (
            current.scheme.lower() != expected.scheme.lower()
            or current.hostname.lower() != expected.hostname.lower()
            or current_port != expected_port
        ):
            return False
        base_path = expected.path.rstrip("/") or "/"
        if base_path != "/" and current.path != base_path and not current.path.startswith(base_path + "/"):
            return False
        if expected.query and current.query != expected.query:
            return False
        if expected.fragment and current.fragment != expected.fragment:
            return False
        return True
