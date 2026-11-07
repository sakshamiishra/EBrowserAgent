"""Offline tests for the safe action executor."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.api.schemas import ElementTarget
from app.browser.locator import DynamicLocatorResolver
from app.browser.snapshot import PageSnapshot
from app.execution.executor import ActionExecutionError, SafeActionExecutor


class FakeLocator:
    def __init__(
        self, *, timeout: bool = False, input_type: str = "text",
        action_error: Exception | None = None, enabled: bool = True, editable: bool = True,
        visible: bool = True,
    ) -> None:
        self.filled: list[str] = []
        self.clicked = 0
        self.selected: list[dict[str, str]] = []
        self.timeout = timeout
        self.input_type = input_type
        self.action_error = action_error
        self.enabled = enabled
        self.editable = editable
        self.visible = visible

    def _raise_action_error(self) -> None:
        if self.action_error is not None:
            raise self.action_error

    async def count(self) -> int:
        return 1

    def nth(self, _index: int) -> "FakeLocator":
        return self

    async def is_visible(self) -> bool:
        return self.visible

    async def is_enabled(self) -> bool:
        return self.enabled

    async def is_editable(self) -> bool:
        return self.editable

    async def element_handle(self) -> "FakeLocator":
        return self

    async def evaluate(self, _expression: str, expected: object) -> bool:
        return expected is self

    async def get_attribute(self, name: str) -> str | None:
        return self.input_type if name == "type" else None

    async def fill(self, value: str) -> None:
        self._raise_action_error()
        if self.timeout:
            await asyncio.Event().wait()
        self.filled.append(value)

    async def click(self) -> None:
        self._raise_action_error()
        self.clicked += 1

    async def select_option(self, **kwargs: str) -> None:
        self._raise_action_error()
        self.selected.append(kwargs)


class FakePage:
    def __init__(self, locator: FakeLocator) -> None:
        self.locator_value = locator
        self.goto = AsyncMock()

    def get_by_role(self, *_args: object, **_kwargs: object) -> FakeLocator:
        return self.locator_value

    def get_by_label(self, *_args: object, **_kwargs: object) -> FakeLocator:
        return self.locator_value

    def get_by_placeholder(self, *_args: object, **_kwargs: object) -> FakeLocator:
        return self.locator_value

    def get_by_label(self, *_args: object, **_kwargs: object) -> FakeLocator:
        return self.locator_value

    def get_by_role(self, *_args: object, **_kwargs: object) -> FakeLocator:
        return self.locator_value


def snapshot() -> PageSnapshot:
    return PageSnapshot(
        url="https://example.test/",
        title="Form",
        visible_text="Email Login Country",
        elements=[
            {"kind": "input", "role": "textbox", "accessible_name": "Email", "label": "Email"},
            {"kind": "button", "role": "button", "accessible_name": "Login", "text": "Login"},
            {"kind": "select", "role": "combobox", "accessible_name": "Country", "label": "Country"},
        ],
    )


class SafeActionExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_fill_resolves_test_data_without_exposing_it_in_result(self) -> None:
        secret = "private-user@example.test"
        locator = FakeLocator()
        executor = SafeActionExecutor(FakePage(locator))
        result = await executor.execute(
            {"type": "fill", "target": {"kind": "label", "value": "Email"}, "value_ref": "email"},
            snapshot(), {"email": secret}, index=0,
        )
        self.assertEqual(locator.filled, [secret])
        self.assertNotIn(secret, str(result.model_dump()))
        self.assertEqual(result.status, "PASS")

    async def test_fill_uses_snapshot_placeholder_and_bounded_truncated_live_lookup(self) -> None:
        locator = FakeLocator()
        page = FakePage(locator)
        truncated = PageSnapshot(
            url="https://example.test/",
            title="Search",
            visible_text="",
            elements=[],
            elements_truncated=True,
            truncated=True,
        )
        resolver = DynamicLocatorResolver(page, inspector=AsyncMock(return_value=truncated))
        result = await SafeActionExecutor(page, locator_resolver=resolver).execute(
            {
                "type": "fill",
                "target": {"kind": "placeholder", "value": "Destination"},
                "value": "London",
            },
            truncated,
            {},
            index=0,
        )
        self.assertEqual(page.locator_value.filled, ["London"])
        self.assertEqual(result.status, "PASS")

    async def test_placeholder_fill_target_matches_snapshot_metadata(self) -> None:
        locator = FakeLocator()
        page = FakePage(locator)
        visible_snapshot = PageSnapshot(
            url="https://example.test/",
            title="Search",
            visible_text="",
            elements=[{
                "kind": "input",
                "role": "textbox",
                "placeholder": "Destination",
                "accessible_name": None,
                "input_type": "text",
            }],
        )
        result = await SafeActionExecutor(page).execute(
            {
                "type": "fill",
                "target": {"kind": "placeholder", "value": "Destination"},
                "value": "London",
            },
            visible_snapshot,
            {},
            index=0,
        )
        self.assertEqual(locator.filled, ["London"])
        self.assertEqual(result.status, "PASS")

    async def test_truncated_fill_lookup_keeps_missing_and_ambiguous_targets_safe(self) -> None:
        truncated = PageSnapshot(
            url="https://example.test/", title="Search", visible_text="",
            elements=[], elements_truncated=True, truncated=True,
        )
        target = {"kind": "placeholder", "value": "Destination"}
        for matches, category in ((0, "missing"), (2, "ambiguous")):
            class CountLocator(FakeLocator):
                async def count(self) -> int:
                    return matches

                def nth(self, _index: int) -> "CountLocator":
                    return self

            locator = CountLocator()
            page = FakePage(locator)
            resolver = DynamicLocatorResolver(
                page, inspector=AsyncMock(return_value=truncated)
            )
            with self.subTest(category=category), self.assertRaises(ActionExecutionError) as raised:
                await SafeActionExecutor(page, locator_resolver=resolver).execute(
                    {"type": "fill", "target": target, "value": "London"},
                    truncated,
                    {},
                    index=0,
                )
            self.assertEqual(raised.exception.category, category)
            self.assertEqual(locator.filled, [])
            diagnostic = raised.exception.fill_diagnostic
            self.assertIsNotNone(diagnostic)
            self.assertEqual(diagnostic.reason_code, "live_no_match" if matches == 0 else "live_ambiguous")
            self.assertEqual(diagnostic.target_kind, "placeholder")
            self.assertEqual(diagnostic.locator_strategy, "get_by_placeholder")
            self.assertEqual(diagnostic.snapshot_match, "missing")
            self.assertTrue(diagnostic.snapshot_truncated)
            self.assertEqual(diagnostic.candidate_count, matches)

    async def test_truncated_fill_reports_hidden_role_mismatch_and_locator_error_safely(self) -> None:
        truncated = PageSnapshot(
            url="https://example.test/", title="Search", visible_text="",
            elements=[], elements_truncated=True, truncated=True,
        )
        class NoMatchLocator(FakeLocator):
            async def count(self) -> int:
                return 0

        cases = (
            (FakeLocator(visible=False), {"kind": "placeholder", "value": "Secret field text"}, "live_not_visible", "missing"),
            (NoMatchLocator(), {"kind": "role", "role": "searchbox", "value": "Destination"}, "live_no_match", "missing"),
        )
        for locator, target, reason, category in cases:
            page = FakePage(locator)
            resolver = DynamicLocatorResolver(page, inspector=AsyncMock(return_value=truncated))
            with self.subTest(reason=reason), self.assertRaises(ActionExecutionError) as raised:
                await SafeActionExecutor(page, locator_resolver=resolver).execute(
                    {"type": "fill", "target": target, "value": "private-fill-value"},
                    truncated, {}, index=0,
                )
            self.assertEqual(raised.exception.category, category)
            diag = raised.exception.fill_diagnostic
            self.assertEqual(diag.reason_code, reason)
            self.assertEqual(diag.target_role, "searchbox" if target["kind"] == "role" else None)
            self.assertEqual(diag.candidate_count, 1 if reason == "live_not_visible" else 0)
            self.assertEqual(diag.visible_count, 0)
            self.assertNotIn("Secret field text", diag.model_dump_json())
            self.assertNotIn("private-fill-value", diag.model_dump_json())

        role_mismatch_snapshot = PageSnapshot(
            url="https://example.test/", title="Search", visible_text="",
            elements=[{
                "kind": "input", "role": "textbox", "accessible_name": "Destination",
            }],
            elements_truncated=True,
            truncated=True,
        )
        mismatch_page = FakePage(NoMatchLocator())
        mismatch_resolver = DynamicLocatorResolver(
            mismatch_page, inspector=AsyncMock(return_value=role_mismatch_snapshot)
        )
        with self.assertRaises(ActionExecutionError) as raised:
            await SafeActionExecutor(mismatch_page, locator_resolver=mismatch_resolver).execute(
                {
                    "type": "fill",
                    "target": {"kind": "role", "role": "searchbox", "value": "Destination"},
                    "value": "private-fill-value",
                },
                role_mismatch_snapshot, {}, index=0,
            )
        self.assertEqual(raised.exception.fill_diagnostic.reason_code, "role_or_name_mismatch")

        class BrokenPage(FakePage):
            def get_by_placeholder(self, *_args: object, **_kwargs: object) -> FakeLocator:
                raise RuntimeError("raw selector and private field content")

        broken_page = BrokenPage(FakeLocator())
        resolver = DynamicLocatorResolver(broken_page, inspector=AsyncMock(return_value=truncated))
        with self.assertRaises(ActionExecutionError) as raised:
            await SafeActionExecutor(broken_page, locator_resolver=resolver).execute(
                {"type": "fill", "target": {"kind": "placeholder", "value": "Private field"}, "value": "private-fill-value"},
                truncated, {}, index=0,
            )
        self.assertEqual(raised.exception.fill_diagnostic.reason_code, "locator_error")
        self.assertNotIn("raw selector", raised.exception.fill_diagnostic.model_dump_json())

    async def test_truncated_fill_lookup_does_not_fill_disabled_or_noneditable_fields(self) -> None:
        truncated = PageSnapshot(
            url="https://example.test/", title="Search", visible_text="",
            elements=[], elements_truncated=True, truncated=True,
        )
        for locator in (FakeLocator(enabled=False), FakeLocator(editable=False)):
            page = FakePage(locator)
            resolver = DynamicLocatorResolver(
                page, inspector=AsyncMock(return_value=truncated)
            )
            with self.subTest(enabled=locator.enabled, editable=locator.editable):
                with self.assertRaises(ActionExecutionError) as raised:
                    await SafeActionExecutor(page, locator_resolver=resolver).execute(
                        {
                            "type": "fill",
                            "target": {"kind": "placeholder", "value": "Destination"},
                            "value": "London",
                        },
                        truncated,
                        {},
                        index=0,
                    )
                self.assertEqual(raised.exception.category, "not_interactable")
                self.assertEqual(locator.filled, [])

    async def test_invalid_action_and_assertion_are_rejected(self) -> None:
        executor = SafeActionExecutor(FakePage(FakeLocator()))
        for action in (
            {"type": "execute", "code": "malicious"},
            {"type": "assert_visible", "target": {"kind": "label", "value": "Login"}},
        ):
            with self.subTest(action=action["type"]), self.assertRaises(ActionExecutionError):
                await executor.execute(action, snapshot(), {}, index=0)

    async def test_missing_value_reference_is_safe(self) -> None:
        executor = SafeActionExecutor(FakePage(FakeLocator()))
        with self.assertRaises(ActionExecutionError) as raised:
            await executor.execute(
                {"type": "fill", "target": {"kind": "label", "value": "Email"}, "value_ref": "missing"},
                snapshot(), {}, index=0,
            )
        self.assertEqual(raised.exception.category, "missing_test_data")
        self.assertNotIn("missing", str(raised.exception))

    async def test_action_timeout_is_bounded(self) -> None:
        executor = SafeActionExecutor(
            FakePage(FakeLocator(timeout=True)), action_timeout_seconds=0.01
        )
        with self.assertRaises(ActionExecutionError) as raised:
            await executor.execute(
                {"type": "fill", "target": {"kind": "label", "value": "Email"}, "value": "x"},
                snapshot(), {}, index=0,
            )
        self.assertEqual(raised.exception.category, "timeout")

    async def test_action_time_playwright_errors_are_classified_without_raw_messages(self) -> None:
        secret = "synthetic-action-error-secret"
        cases = (
            (
                {"type": "fill", "target": {"kind": "label", "value": "Email"}, "value": "x"},
                PlaywrightTimeoutError(f"private timeout {secret}"),
                "timeout",
            ),
            (
                {"type": "click", "target": {"kind": "role", "role": "button", "value": "Login"}},
                PlaywrightError(f"Element is not attached to the DOM: {secret}"),
                "detached_element",
            ),
            (
                {"type": "select", "target": {"kind": "label", "value": "Country"}, "option": "Canada"},
                PlaywrightError(f"Target page, context or browser has been closed: {secret}"),
                "closed_page",
            ),
            (
                {"type": "click", "target": {"kind": "role", "role": "button", "value": "Login"}},
                PlaywrightError(f"Protocol error in action: {secret}"),
                "infrastructure",
            ),
            (
                {"type": "click", "target": {"kind": "role", "role": "button", "value": "Login"}},
                PlaywrightError(f"The interaction is not supported: {secret}"),
                "browser_action",
            ),
        )
        for action, error, expected_category in cases:
            with self.subTest(expected_category=expected_category):
                executor = SafeActionExecutor(
                    FakePage(FakeLocator(action_error=error))
                )
                with self.assertRaises(ActionExecutionError) as raised:
                    await executor.execute(action, snapshot(), {}, index=0)
                self.assertEqual(raised.exception.category, expected_category)
                self.assertNotIn(secret, str(raised.exception))

    async def test_action_count_limit_is_enforced(self) -> None:
        executor = SafeActionExecutor(FakePage(FakeLocator()), max_actions=1)
        action = {"type": "click", "target": {"kind": "role", "role": "button", "value": "Login"}}
        await executor.execute(action, snapshot(), {}, index=0)
        with self.assertRaises(ActionExecutionError) as raised:
            await executor.execute(action, snapshot(), {}, index=1)
        self.assertEqual(raised.exception.category, "action_limit")

    async def test_navigation_uses_validated_helper(self) -> None:
        page = FakePage(FakeLocator())
        executor = SafeActionExecutor(page)
        with patch("app.browser.manager.navigate_page", new=AsyncMock()) as navigate:
            await executor.execute(
                {"type": "navigate", "url": "https://example.test/next"},
                snapshot(), {}, index=0,
            )
        navigate.assert_awaited_once_with(page, "https://example.test/next")
        page.goto.assert_not_awaited()

    async def test_select_option_uses_canonical_option_value(self) -> None:
        locator = FakeLocator()
        executor = SafeActionExecutor(FakePage(locator))
        result = await executor.execute(
            {"type": "select", "target": {"kind": "label", "value": "Country"}, "option": "Canada"},
            snapshot(), {}, index=0,
        )
        self.assertEqual(locator.selected, [{"label": "Canada"}])
        self.assertEqual(result.status, "PASS")

    async def test_password_fill_requires_value_reference_and_keeps_value_out_of_report(self) -> None:
        password_snapshot = PageSnapshot(
            url="https://example.test/", title="Login", visible_text="Password",
            elements=[{"kind": "input", "role": "textbox", "label": "Password", "accessible_name": "Password", "input_type": "password"}],
        )
        literal_locator = FakeLocator(input_type="password")
        with self.assertRaises(ActionExecutionError) as raised:
            await SafeActionExecutor(FakePage(literal_locator)).execute(
                {"type": "fill", "target": {"kind": "label", "value": "Password"}, "value": "should-not-run"},
                password_snapshot, {}, index=0,
            )
        self.assertEqual(raised.exception.category, "sensitive_value_ref_required")
        self.assertEqual(literal_locator.filled, [])

        secret = "synthetic-password-123"
        referenced_locator = FakeLocator(input_type="password")
        result = await SafeActionExecutor(FakePage(referenced_locator)).execute(
            {"type": "fill", "target": {"kind": "label", "value": "Password"}, "value_ref": "password"},
            password_snapshot, {"password": secret}, index=0,
        )
        self.assertEqual(referenced_locator.filled, [secret])
        self.assertNotIn(secret, result.model_dump_json())


if __name__ == "__main__":
    unittest.main()
