"""Offline tests for independent assertion verification."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from app.api.schemas import AssertTextAction, AssertUrlAction, AssertVisibleAction
from app.browser.snapshot import PageSnapshot
from app.browser.locator import DynamicLocatorResolver, LocatorResolutionError
from app.execution.verification import AssertionVerifier


class Locator:
    def __init__(
        self,
        *,
        visible: bool = True,
        text: str = "Login form displayed",
        input_value: str = "",
        is_value_control: bool = False,
    ) -> None:
        self.visible = visible
        self.text = text
        self.value = input_value
        self.is_value_control = is_value_control
        self.inner_text_calls = 0
        self.input_value_calls = 0

    async def count(self) -> int:
        return 1

    def nth(self, _index: int) -> "Locator":
        return self

    async def is_visible(self) -> bool:
        return self.visible

    async def inner_text(self) -> str:
        self.inner_text_calls += 1
        return self.text

    async def evaluate(self, _expression: str) -> bool:
        return self.is_value_control

    async def input_value(self) -> str:
        self.input_value_calls += 1
        return self.value


class Page:
    def __init__(self, locator: Locator | None = None, url: str = "https://example.test/login") -> None:
        self.locator = locator or Locator()
        self.url = url

    def get_by_role(self, *_args: object, **_kwargs: object) -> Locator:
        return self.locator

    def get_by_label(self, *_args: object, **_kwargs: object) -> Locator:
        return self.locator

    def get_by_placeholder(self, *_args: object, **_kwargs: object) -> Locator:
        return self.locator


def snapshot(
    *,
    text: str = "Login form displayed",
    truncated: bool = False,
    elements_truncated: bool = False,
    elements: list[dict[str, object]] | None = None,
) -> PageSnapshot:
    return PageSnapshot(
        url="https://example.test/login",
        title="Login",
        visible_text=text,
        visible_text_truncated=truncated,
        elements_truncated=elements_truncated,
        truncated=truncated or elements_truncated,
        elements=elements if elements is not None else [
            {"kind": "button", "role": "button", "accessible_name": "Login", "text": "Login"},
            {"kind": "input", "role": "textbox", "label": "Email", "accessible_name": "Email"},
        ],
    )


class AssertionVerifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_visible_assertion_is_checked_against_live_locator(self) -> None:
        verifier = AssertionVerifier(Page(Locator(visible=True)))
        result = await verifier.verify(
            AssertVisibleAction(type="assert_visible", target={"kind": "role", "role": "button", "value": "Login"}),
            snapshot(), index=0,
        )
        self.assertEqual(result.status, "PASS")

    async def test_browser_infrastructure_failure_is_inconclusive_not_missing(self) -> None:
        class ClosedLocator(Locator):
            async def count(self) -> int:
                raise RuntimeError("private closed-page details")

        result = await AssertionVerifier(Page(ClosedLocator())).verify(
            AssertVisibleAction(
                type="assert_visible",
                target={"kind": "role", "role": "button", "value": "Login"},
            ),
            snapshot(), index=0,
        )
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertNotIn("private", result.explanation)

    async def test_confirmed_zero_matches_are_a_failed_visibility_assertion(self) -> None:
        empty_snapshot = snapshot(text="", elements=[])
        resolver = DynamicLocatorResolver(
            Page(), inspector=AsyncMock(return_value=empty_snapshot)
        )
        result = await AssertionVerifier(Page(), resolver).verify(
            AssertVisibleAction(
                type="assert_visible", target={"kind": "role", "role": "button", "value": "PrivateWidgetName"},
            ),
            snapshot(elements=[]), index=0,
        )
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.reason_code, "not_found_in_complete_snapshot")
        self.assertEqual(result.target_kind, "role")
        self.assertEqual(result.snapshot_match, "missing")
        self.assertFalse(result.snapshot_truncated)
        self.assertNotIn("PrivateWidgetName", result.model_dump_json())

    async def test_missing_target_in_truncated_snapshot_is_inconclusive(self) -> None:
        class NoLiveMatch(Locator):
            async def count(self) -> int:
                return 0

        truncated_snapshot = snapshot(text="", elements_truncated=True, elements=[])
        page = Page(NoLiveMatch())
        resolver = DynamicLocatorResolver(
            page, inspector=AsyncMock(return_value=truncated_snapshot)
        )
        result = await AssertionVerifier(page, resolver).verify(
            AssertVisibleAction(
                type="assert_visible", target={"kind": "role", "role": "button", "value": "Search"}
            ),
            truncated_snapshot,
            index=0,
        )
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertEqual(result.reason_code, "live_no_match")
        self.assertEqual(result.target_kind, "role")
        self.assertEqual(result.snapshot_match, "missing")
        self.assertTrue(result.snapshot_truncated)
        self.assertEqual(result.candidate_count, 0)
        self.assertEqual(result.visible_count, 0)

    async def test_omitted_target_can_pass_unique_visible_live_lookup(self) -> None:
        truncated_snapshot = snapshot(text="", elements_truncated=True, elements=[])
        page = Page(Locator(visible=True))
        resolver = DynamicLocatorResolver(
            page, inspector=AsyncMock(return_value=truncated_snapshot)
        )
        result = await AssertionVerifier(page, resolver).verify(
            AssertVisibleAction(
                type="assert_visible", target={"kind": "role", "role": "button", "value": "Search"}
            ),
            truncated_snapshot,
            index=0,
        )
        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.reason_code, "verified")
        self.assertEqual(result.candidate_count, 1)
        self.assertEqual(result.visible_count, 1)
        self.assertNotIn("Search", result.model_dump_json())

    async def test_multiple_live_matches_for_omitted_target_remain_inconclusive(self) -> None:
        class MultipleLocator(Locator):
            async def count(self) -> int:
                return 2

            def nth(self, _index: int) -> "MultipleLocator":
                return self

        truncated_snapshot = snapshot(text="", elements_truncated=True, elements=[])
        page = Page(MultipleLocator())
        resolver = DynamicLocatorResolver(
            page, inspector=AsyncMock(return_value=truncated_snapshot)
        )
        result = await AssertionVerifier(page, resolver).verify(
            AssertVisibleAction(
                type="assert_visible", target={"kind": "role", "role": "button", "value": "Search"}
            ),
            truncated_snapshot,
            index=0,
        )
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertEqual(result.reason_code, "live_ambiguous")
        self.assertEqual(result.candidate_count, 2)
        self.assertEqual(result.visible_count, 2)

    async def test_hidden_live_candidates_report_counts_and_fail_visibility(self) -> None:
        truncated_snapshot = snapshot(text="", elements_truncated=True, elements=[])
        page = Page(Locator(visible=False))
        resolver = DynamicLocatorResolver(
            page, inspector=AsyncMock(return_value=truncated_snapshot)
        )
        result = await AssertionVerifier(page, resolver).verify(
            AssertVisibleAction(
                type="assert_visible", target={"kind": "role", "role": "button", "value": "Search"}
            ),
            truncated_snapshot,
            index=0,
        )
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.reason_code, "live_not_visible")
        self.assertEqual(result.candidate_count, 1)
        self.assertEqual(result.visible_count, 0)

    async def test_live_lookup_error_for_omitted_target_is_inconclusive(self) -> None:
        class BrokenLocator(Locator):
            async def count(self) -> int:
                raise RuntimeError("sensitive browser failure")

        truncated_snapshot = snapshot(text="", elements_truncated=True, elements=[])
        page = Page(BrokenLocator())
        resolver = DynamicLocatorResolver(
            page, inspector=AsyncMock(return_value=truncated_snapshot)
        )
        result = await AssertionVerifier(page, resolver).verify(
            AssertVisibleAction(
                type="assert_visible", target={"kind": "role", "role": "button", "value": "Search"}
            ),
            truncated_snapshot,
            index=0,
        )
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertEqual(result.reason_code, "verification_error")
        self.assertNotIn("sensitive browser failure", result.model_dump_json())

    async def test_found_but_invisible_target_fails_with_safe_reason(self) -> None:
        invisible_page = Page(Locator(visible=False))
        resolver = DynamicLocatorResolver(
            invisible_page,
            inspector=AsyncMock(return_value=snapshot()),
        )
        result = await AssertionVerifier(invisible_page, resolver).verify(
            AssertVisibleAction(
                type="assert_visible", target={"kind": "role", "role": "button", "value": "Login"}
            ),
            snapshot(),
            index=0,
        )
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.reason_code, "live_not_visible")

    async def test_ambiguous_target_remains_inconclusive_with_safe_reason(self) -> None:
        class AmbiguousResolver:
            async def find_visible(self, *_args: object, **_kwargs: object) -> list[object]:
                raise LocatorResolutionError("ambiguous", "private locator detail")

        result = await AssertionVerifier(Page(), AmbiguousResolver()).verify(
            AssertVisibleAction(
                type="assert_visible", target={"kind": "role", "role": "button", "value": "Secret search control"}
            ),
            snapshot(),
            index=0,
        )
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertEqual(result.reason_code, "live_ambiguous")
        self.assertNotIn("Secret search control", result.model_dump_json())
        self.assertNotIn("private locator detail", result.model_dump_json())

    async def test_multiple_visible_matches_are_inconclusive(self) -> None:
        class MultipleLocator(Locator):
            async def count(self) -> int:
                return 2

            def nth(self, _index: int) -> "MultipleLocator":
                return self

        page = Page(MultipleLocator())
        result = await AssertionVerifier(page).verify(
            AssertVisibleAction(
                type="assert_visible", target={"kind": "role", "role": "button", "value": "Login"}
            ),
            snapshot(),
            index=0,
        )
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertEqual(result.reason_code, "live_ambiguous")

    async def test_targeted_text_uses_visible_inner_text(self) -> None:
        locator = Locator(text="Login form displayed")
        verifier = AssertionVerifier(Page(locator))
        result = await verifier.verify(
            AssertTextAction(
                type="assert_text",
                expected_text="Login form",
                target={"kind": "role", "role": "button", "value": "Login"},
            ),
            snapshot(), index=0,
        )
        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.reason_code, "verified")
        self.assertEqual(locator.inner_text_calls, 1)
        self.assertEqual(locator.input_value_calls, 0)

    async def test_assert_text_checks_current_input_value_exactly(self) -> None:
        input_snapshot = snapshot(elements=[{
            "kind": "input", "role": "textbox", "label": "Destination",
            "accessible_name": "Destination", "input_type": "text",
        }])
        for actual, expected, status in (
            ("London", "London", "PASS"),
            ("Paris", "London", "FAIL"),
        ):
            locator = Locator(input_value=actual, is_value_control=True)
            result = await AssertionVerifier(Page(locator)).verify(
                AssertTextAction(
                    type="assert_text",
                    target={"kind": "label", "value": "Destination"},
                    expected_text=expected,
                ),
                input_snapshot,
                index=0,
            )
            with self.subTest(actual=actual):
                self.assertEqual(result.status, status)
                self.assertEqual(locator.input_value_calls, 1)
                self.assertEqual(locator.inner_text_calls, 0)
                self.assertNotIn(actual, result.model_dump_json())
                self.assertNotIn(expected, result.model_dump_json())

    async def test_password_value_assertion_never_exposes_the_value(self) -> None:
        secret = "synthetic-password-value"
        password_snapshot = snapshot(elements=[{
            "kind": "input", "role": "textbox", "label": "Password",
            "accessible_name": "Password", "input_type": "password",
        }])
        locator = Locator(input_value=secret, is_value_control=True)
        result = await AssertionVerifier(Page(locator)).verify(
            AssertTextAction(
                type="assert_text",
                target={"kind": "label", "value": "Password"},
                expected_text=secret,
            ),
            password_snapshot,
            index=0,
        )
        self.assertEqual(result.status, "PASS")
        self.assertNotIn(secret, result.model_dump_json())

    async def test_ambiguous_input_value_assertion_remains_inconclusive(self) -> None:
        class MultipleInputs(Locator):
            async def count(self) -> int:
                return 2

            def nth(self, _index: int) -> "MultipleInputs":
                return self

        input_snapshot = snapshot(elements=[{
            "kind": "input", "role": "textbox", "label": "Destination",
            "accessible_name": "Destination", "input_type": "text",
        }])
        locator = MultipleInputs(input_value="London", is_value_control=True)
        result = await AssertionVerifier(Page(locator)).verify(
            AssertTextAction(
                type="assert_text",
                target={"kind": "label", "value": "Destination"},
                expected_text="London",
            ),
            input_snapshot,
            index=0,
        )
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertEqual(result.reason_code, "live_ambiguous")
        self.assertEqual(locator.input_value_calls, 0)

    async def test_absent_text_in_truncated_snapshot_is_inconclusive(self) -> None:
        verifier = AssertionVerifier(Page())
        result = await verifier.verify(
            AssertTextAction(type="assert_text", expected_text="not in captured prefix"),
            snapshot(text="short prefix", truncated=True), index=0,
        )
        self.assertEqual(result.status, "INCONCLUSIVE")

    async def test_url_assertion_validates_expected_url_before_comparison(self) -> None:
        verifier = AssertionVerifier(Page())
        with patch(
            "app.execution.verification.validate_target_url",
            new=AsyncMock(return_value="https://example.test/login"),
        ) as validate:
            result = await verifier.verify(
                AssertUrlAction(type="assert_url", expected_url="https://example.test/login"),
                snapshot(), index=0,
            )
        validate.assert_awaited_once()
        self.assertEqual(result.status, "PASS")

    async def test_non_exact_url_comparison_does_not_match_another_hostname(self) -> None:
        verifier = AssertionVerifier(Page(url="https://example.test.evil/login"))
        with patch(
            "app.execution.verification.validate_target_url",
            new=AsyncMock(return_value="https://example.test/login"),
        ):
            result = await verifier.verify(
                AssertUrlAction(
                    type="assert_url", expected_url="https://example.test/login", exact=False
                ),
                snapshot(), index=0,
            )
        self.assertEqual(result.status, "FAIL")

    async def test_non_exact_url_allows_a_path_beneath_expected_path(self) -> None:
        verifier = AssertionVerifier(Page(url="https://example.test/login/step"))
        with patch(
            "app.execution.verification.validate_target_url",
            new=AsyncMock(return_value="https://example.test/login"),
        ):
            result = await verifier.verify(
                AssertUrlAction(
                    type="assert_url", expected_url="https://example.test/login", exact=False
                ),
                snapshot(), index=0,
            )
        self.assertEqual(result.status, "PASS")


if __name__ == "__main__":
    unittest.main()
