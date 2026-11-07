"""Resolve schema-validated targets against bounded page snapshots."""

from __future__ import annotations

import re
from dataclasses import dataclass
from collections.abc import Awaitable
from typing import Any, Callable, Literal, TypeVar

from pydantic import ValidationError

from app.api.schemas import ElementTarget, FillResolutionDiagnostic
from app.browser.inspector import inspect_page
from app.browser.snapshot import InteractiveElementSnapshot, PageSnapshot


class LocatorResolutionError(RuntimeError):
    """A target could not be resolved safely; messages never contain values."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


@dataclass(slots=True)
class AssertionLookupDiagnostics:
    """Bounded, value-free metadata about one assertion target lookup."""

    target_kind: str | None = None
    snapshot_match: Literal["matched", "missing"] | None = None
    snapshot_truncated: bool = False
    candidate_count: int | None = None
    visible_count: int | None = None
    reason_code: str | None = None


@dataclass(slots=True)
class FillLookupDiagnostics:
    """Allow-listed, value-free diagnostics for one fill target lookup."""

    target_kind: str | None = None
    target_role: str | None = None
    locator_strategy: str | None = None
    snapshot_match: Literal["matched", "missing"] | None = None
    snapshot_truncated: bool = False
    candidate_count: int | None = None
    visible_count: int | None = None
    reason_code: str | None = None

    def to_model(self) -> FillResolutionDiagnostic | None:
        if self.target_kind is None or self.locator_strategy is None or self.reason_code is None:
            return None
        return FillResolutionDiagnostic.model_validate({
            "target_kind": self.target_kind,
            "target_role": self.target_role,
            "locator_strategy": self.locator_strategy,
            "snapshot_match": self.snapshot_match,
            "snapshot_truncated": self.snapshot_truncated,
            "candidate_count": self.candidate_count,
            "visible_count": self.visible_count,
            "reason_code": self.reason_code,
        })


_SAFE_ROLES = {
    "button", "link", "textbox", "searchbox", "combobox", "checkbox", "radio",
    "option", "menuitem", "tab", "switch", "slider", "spinbutton", "heading",
    "img", "listbox",
}
_LOCATOR_STRATEGIES = {
    "role": "get_by_role",
    "label": "get_by_label",
    "placeholder": "get_by_placeholder",
    "text": "get_by_text",
    "attribute": "allowlisted_attribute",
    "css": "restricted_css",
}


T = TypeVar("T")


_ALLOWED_ATTRIBUTES = {
    "name": "name",
    "data-testid": "test_id",
    "aria-label": "aria_label",
    "placeholder": "placeholder",
}
_CSS_SIMPLE = re.compile(
    r'^(?P<tag>button|a|input|select|textarea)'
    r'(?:\[(?P<attribute>data-testid|name|aria-label|placeholder)="'
    r'(?P<value>[^"\\\]\r\n]{1,160})"\])?$'
)


def _snapshot_matches(
    target: ElementTarget, snapshot: PageSnapshot
) -> list[InteractiveElementSnapshot]:
    value = target.value
    if target.kind == "role":
        return [
            item for item in snapshot.elements
            if item.role == target.role and item.accessible_name == value
        ]
    if target.kind == "label":
        return [
            item for item in snapshot.elements
            if item.label == value or item.accessible_name == value
        ]
    if target.kind == "placeholder":
        return [item for item in snapshot.elements if item.placeholder == value]
    if target.kind == "text":
        return [
            item for item in snapshot.elements
            if item.text == value or item.accessible_name == value
        ]
    if target.kind == "attribute":
        attribute, expected = _parse_attribute_target(value)
        field = _ALLOWED_ATTRIBUTES[attribute]
        return [item for item in snapshot.elements if getattr(item, field) == expected]
    if target.kind == "css":
        match = _CSS_SIMPLE.fullmatch(value)
        if not match:
            return []
        kind = {"a": "link"}.get(match.group("tag"), match.group("tag"))
        attribute = match.group("attribute")
        if attribute:
            field = _ALLOWED_ATTRIBUTES[attribute]
            return [
                item for item in snapshot.elements
                if item.kind == kind and getattr(item, field) == match.group("value")
            ]
        return [item for item in snapshot.elements if item.kind == kind]
    return []


def snapshot_matching_elements(
    target: ElementTarget, snapshot: PageSnapshot
) -> list[InteractiveElementSnapshot]:
    """Return the observed elements matching one validated semantic target."""
    try:
        validated = ElementTarget.model_validate(target)
        return _snapshot_matches(validated, snapshot)
    except (ValidationError, LocatorResolutionError, TypeError, ValueError):
        return []


def target_matches_snapshot(target: ElementTarget, snapshot: PageSnapshot) -> bool:
    """Return whether a validated semantic target is present in observed metadata."""

    return bool(snapshot_matching_elements(target, snapshot))


def _parse_attribute_target(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise LocatorResolutionError(
            "unsupported", "Attribute targets must use an allowed attribute and value"
        )
    attribute, expected = value.split("=", 1)
    attribute = attribute.strip().lower()
    if attribute not in _ALLOWED_ATTRIBUTES or not expected:
        raise LocatorResolutionError(
            "unsupported", "Attribute target is not supported"
        )
    return attribute, expected


def _make_locator(page: Any, target: ElementTarget) -> Any:
    if target.kind == "role":
        return page.get_by_role(target.role, name=target.value, exact=True)
    if target.kind == "label":
        return page.get_by_label(target.value, exact=True)
    if target.kind == "placeholder":
        return page.get_by_placeholder(target.value, exact=True)
    if target.kind == "text":
        return page.get_by_text(target.value, exact=True)
    if target.kind == "attribute":
        attribute, expected = _parse_attribute_target(target.value)
        escaped = expected.replace("\\", "\\\\").replace('"', '\\"')
        return page.locator(f'[{attribute}="{escaped}"]')
    if target.kind == "css":
        if not _CSS_SIMPLE.fullmatch(target.value):
            raise LocatorResolutionError(
                "unsupported", "CSS target is outside the supported simple-selector subset"
            )
        return page.locator(target.value)
    raise LocatorResolutionError("unsupported", "Target kind is not supported")


@dataclass(slots=True)
class DynamicLocatorResolver:
    """Resolve a unique visible target, refreshing one stale snapshot once."""

    page: Any
    inspector: Callable[[Any], Any] = inspect_page
    max_candidates: int = 100

    async def resolve(
        self,
        target: ElementTarget | dict[str, object],
        snapshot: PageSnapshot,
        *,
        allow_truncated_live_lookup: bool = False,
        fill_diagnostics: FillLookupDiagnostics | None = None,
    ) -> Any:
        visible = await self.find_visible(
            target,
            snapshot,
            allow_truncated_live_lookup=allow_truncated_live_lookup,
            fill_diagnostics=fill_diagnostics,
        )
        if not visible:
            if fill_diagnostics is not None and fill_diagnostics.reason_code is None:
                fill_diagnostics.reason_code = "target_not_in_snapshot"
            raise LocatorResolutionError("missing", "Target was not found in the current page snapshot")
        if len(visible) != 1:
            if fill_diagnostics is not None:
                fill_diagnostics.reason_code = "live_ambiguous"
            raise LocatorResolutionError("ambiguous", "Target matches multiple visible elements")
        # Return the semantic locator, never a saved nth(index) locator. Playwright
        # strictness then fails safely if matching elements are inserted before use.
        return visible[0]

    async def perform(
        self,
        target: ElementTarget | dict[str, object],
        snapshot: PageSnapshot,
        operation: Callable[[Any], Awaitable[T]],
        *,
        require_editable: bool = False,
        allow_truncated_live_lookup: bool = False,
        fill_diagnostics: FillLookupDiagnostics | None = None,
    ) -> T:
        """Perform an operation on the exact element handle selected for this action."""

        locator = await self.resolve(
            target,
            snapshot,
            allow_truncated_live_lookup=allow_truncated_live_lookup,
            fill_diagnostics=fill_diagnostics,
        )
        try:
            match_count = await locator.count()
            if match_count != 1:
                if fill_diagnostics is not None:
                    fill_diagnostics.reason_code = "live_ambiguous"
                raise LocatorResolutionError(
                    "ambiguous", "The target changed before the action could be performed"
                )
            handle = await locator.element_handle()
            if handle is None:
                if fill_diagnostics is not None:
                    fill_diagnostics.reason_code = "locator_error"
                raise LocatorResolutionError(
                    "missing", "The target detached before the action could be performed"
                )
            # Confirm that the unique semantic locator still identifies the
            # captured node. Subsequent actions use only the handle, so a later
            # replacement can fail as detached but cannot retarget the action.
            same_element = await locator.evaluate(
                "(element, expected) => element === expected", handle
            )
            visible = await handle.is_visible()
            enabled = await handle.is_enabled()
            editable = await handle.is_editable() if require_editable else True
        except LocatorResolutionError:
            if fill_diagnostics is not None and fill_diagnostics.reason_code is None:
                fill_diagnostics.reason_code = "locator_error"
            raise
        except Exception:
            if fill_diagnostics is not None:
                fill_diagnostics.reason_code = "locator_error"
            raise LocatorResolutionError(
                "infrastructure", "The browser could not inspect the target reliably"
            ) from None
        if not same_element or not visible:
            if fill_diagnostics is not None:
                fill_diagnostics.reason_code = (
                    "live_not_visible" if same_element else "locator_error"
                )
            raise LocatorResolutionError(
                "ambiguous", "The target changed before the action could be performed"
            )
        if not enabled or not editable:
            if fill_diagnostics is not None:
                fill_diagnostics.reason_code = "target_not_interactable"
            raise LocatorResolutionError(
                "not_interactable", "The target is disabled or cannot be edited"
            )
        # Operation errors are intentionally left to the executor, which wraps
        # them without exposing Playwright's potentially sensitive message.
        return await operation(handle)

    async def find_visible(
        self,
        target: ElementTarget | dict[str, object],
        snapshot: PageSnapshot,
        *,
        assertion_diagnostics: AssertionLookupDiagnostics | None = None,
        allow_truncated_live_lookup: bool = False,
        fill_diagnostics: FillLookupDiagnostics | None = None,
    ) -> list[Any]:
        """Return visible matches, optionally resolving truncated snapshots for assertions."""

        try:
            validated = ElementTarget.model_validate(target)
        except ValidationError:
            raise LocatorResolutionError("invalid_target", "Target failed schema validation") from None
        if assertion_diagnostics is not None:
            assertion_diagnostics.target_kind = validated.kind
            assertion_diagnostics.snapshot_truncated = snapshot.elements_truncated
        if fill_diagnostics is not None:
            fill_diagnostics.target_kind = validated.kind
            fill_diagnostics.target_role = (
                validated.role if validated.role in _SAFE_ROLES else "other"
            ) if validated.kind == "role" else None
            fill_diagnostics.locator_strategy = _LOCATOR_STRATEGIES[validated.kind]
            fill_diagnostics.snapshot_truncated = snapshot.elements_truncated
        if validated.kind == "attribute":
            try:
                _parse_attribute_target(validated.value)
            except LocatorResolutionError:
                if fill_diagnostics is not None:
                    fill_diagnostics.reason_code = "locator_error"
                raise
        elif validated.kind == "css" and not _CSS_SIMPLE.fullmatch(validated.value):
            if fill_diagnostics is not None:
                fill_diagnostics.reason_code = "locator_error"
            raise LocatorResolutionError(
                "unsupported", "CSS target is outside the supported simple-selector subset"
            )

        current_snapshot = snapshot
        refreshed = False
        matched_but_invisible = False
        while True:
            matches = _snapshot_matches(validated, current_snapshot)
            if fill_diagnostics is not None:
                fill_diagnostics.snapshot_match = "matched" if matches else "missing"
                fill_diagnostics.snapshot_truncated = current_snapshot.elements_truncated
            if assertion_diagnostics is not None:
                assertion_diagnostics.snapshot_match = "matched" if matches else "missing"
                assertion_diagnostics.snapshot_truncated = current_snapshot.elements_truncated
            if matches:
                try:
                    locator = _make_locator(self.page, validated)
                    candidate_count, visible_count = await self._candidate_visibility(
                        locator, assertion_diagnostics, fill_diagnostics
                    )
                except LocatorResolutionError:
                    if fill_diagnostics is not None and fill_diagnostics.reason_code is None:
                        fill_diagnostics.reason_code = "locator_error"
                    raise
                except Exception:
                    if fill_diagnostics is not None:
                        fill_diagnostics.reason_code = "locator_error"
                    raise LocatorResolutionError(
                        "infrastructure", "The browser could not inspect the target reliably"
                    ) from None
                if fill_diagnostics is not None:
                    fill_diagnostics.candidate_count = min(candidate_count, 100)
                    fill_diagnostics.visible_count = min(visible_count, 100)
                if visible_count:
                    if fill_diagnostics is not None:
                        fill_diagnostics.reason_code = (
                            "live_ambiguous" if visible_count > 1 else "resolved"
                        )
                    if assertion_diagnostics is not None:
                        assertion_diagnostics.candidate_count = candidate_count
                        assertion_diagnostics.visible_count = visible_count
                        assertion_diagnostics.reason_code = (
                            "live_ambiguous" if visible_count > 1 else "verified"
                        )
                    return [locator] * visible_count
                matched_but_invisible = candidate_count > 0
                if fill_diagnostics is not None and candidate_count > 0:
                    fill_diagnostics.reason_code = "live_not_visible"
                elif fill_diagnostics is not None:
                    fill_diagnostics.reason_code = "locator_mismatch"
            if refreshed:
                if (
                    (assertion_diagnostics is not None or allow_truncated_live_lookup)
                    and not matches
                    and current_snapshot.elements_truncated
                ):
                    # The snapshot cannot establish absence. Make one bounded,
                    # exact live lookup using only the validated target and
                    # existing selector restrictions.
                    try:
                        locator = _make_locator(self.page, validated)
                        candidate_count, visible_count = await self._candidate_visibility(
                            locator, assertion_diagnostics, fill_diagnostics
                        )
                    except Exception:
                        if fill_diagnostics is not None and fill_diagnostics.reason_code is None:
                            fill_diagnostics.reason_code = "locator_error"
                        raise LocatorResolutionError(
                            "infrastructure", "The browser could not inspect the target reliably"
                        ) from None
                    if fill_diagnostics is not None:
                        fill_diagnostics.candidate_count = min(candidate_count, 100)
                        fill_diagnostics.visible_count = min(visible_count, 100)
                    if assertion_diagnostics is not None:
                        assertion_diagnostics.candidate_count = candidate_count
                        assertion_diagnostics.visible_count = visible_count
                    if candidate_count == 0:
                        if fill_diagnostics is not None:
                            snapshot_role_mismatch = (
                                validated.kind == "role"
                                and any(
                                    item.accessible_name == validated.value
                                    and item.role != validated.role
                                    for item in current_snapshot.elements
                                )
                            )
                            fill_diagnostics.reason_code = (
                                "role_or_name_mismatch" if snapshot_role_mismatch
                                else "live_no_match"
                            )
                        if assertion_diagnostics is not None:
                            assertion_diagnostics.reason_code = "live_no_match"
                            raise LocatorResolutionError(
                                "live_no_match", "The target had no live matches"
                            )
                        return []
                    if visible_count == 0:
                        if fill_diagnostics is not None:
                            fill_diagnostics.reason_code = "live_not_visible"
                        if assertion_diagnostics is not None:
                            assertion_diagnostics.reason_code = "live_not_visible"
                            raise LocatorResolutionError(
                                "live_not_visible", "Live matches were not visible"
                            )
                        return []
                    if visible_count > 1 and assertion_diagnostics is not None:
                        assertion_diagnostics.reason_code = "live_ambiguous"
                    if visible_count == 1 and assertion_diagnostics is not None:
                        assertion_diagnostics.reason_code = "verified"
                    if fill_diagnostics is not None:
                        fill_diagnostics.reason_code = (
                            "live_ambiguous" if visible_count > 1 else "resolved"
                        )
                    return [locator] * visible_count
                if assertion_diagnostics is not None:
                    if not matches and not current_snapshot.elements_truncated:
                        raise LocatorResolutionError(
                            "missing", "Target was not found in a complete page snapshot"
                        )
                    if matched_but_invisible:
                        assertion_diagnostics.reason_code = "live_not_visible"
                        raise LocatorResolutionError(
                            "live_not_visible", "The target was not visible"
                        )
                    if assertion_diagnostics.candidate_count == 0:
                        assertion_diagnostics.reason_code = "live_no_match"
                        raise LocatorResolutionError(
                            "live_no_match", "The target had no live matches"
                        )
                    if current_snapshot.elements_truncated:
                        raise LocatorResolutionError(
                            "snapshot_truncated",
                            "The target was not found in a complete element snapshot",
                        )
                    raise LocatorResolutionError(
                        "missing", "Target was not found in the current page snapshot"
                    )
                if fill_diagnostics is not None and fill_diagnostics.reason_code is None:
                    fill_diagnostics.reason_code = "target_not_in_snapshot"
                return []
            refreshed = True
            try:
                current_snapshot = await self.inspector(self.page)
            except Exception:
                if fill_diagnostics is not None:
                    fill_diagnostics.reason_code = "locator_error"
                raise LocatorResolutionError("inspection", "Could not refresh the page snapshot") from None

    async def _candidate_visibility(
        self,
        locator: Any,
        diagnostics: AssertionLookupDiagnostics | None = None,
        fill_diagnostics: FillLookupDiagnostics | None = None,
    ) -> tuple[int, int]:
        try:
            count = await locator.count()
            if count > self.max_candidates:
                if fill_diagnostics is not None:
                    fill_diagnostics.candidate_count = 100
                    fill_diagnostics.visible_count = None
                    fill_diagnostics.reason_code = "live_ambiguous"
                if diagnostics is not None:
                    diagnostics.candidate_count = min(self.max_candidates, 100)
                    diagnostics.visible_count = None
                    diagnostics.reason_code = "snapshot_truncated"
                raise LocatorResolutionError("ambiguous", "Target has too many page matches")
            visible = 0
            for index in range(count):
                candidate = locator.nth(index)
                if await candidate.is_visible():
                    visible += 1
                    if diagnostics is None and visible > 1:
                        break
            bounded_count = min(count, 100)
            if fill_diagnostics is not None:
                fill_diagnostics.candidate_count = bounded_count
                fill_diagnostics.visible_count = min(visible, 100)
            if diagnostics is not None:
                diagnostics.candidate_count = bounded_count
                diagnostics.visible_count = visible
            return bounded_count, visible
        except LocatorResolutionError:
            raise
        except TimeoutError:
            raise LocatorResolutionError(
                "infrastructure", "The browser could not inspect the target reliably"
            ) from None
        except Exception:
            raise LocatorResolutionError(
                "infrastructure", "The browser could not inspect the target reliably"
            ) from None
