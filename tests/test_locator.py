"""Offline tests for dynamic, ambiguity-safe locator resolution."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.api.schemas import ElementTarget
from app.browser.locator import DynamicLocatorResolver, LocatorResolutionError
from app.browser.snapshot import PageSnapshot


class FakeLocator:
    def __init__(self, visible: list[bool]) -> None:
        self.visible = visible
        self.clicked = 0

    async def count(self) -> int:
        return len(self.visible)

    async def is_visible(self) -> bool:
        return len(self.visible) == 1 and self.visible[0]

    async def is_enabled(self) -> bool:
        return True

    async def is_editable(self) -> bool:
        return True

    async def element_handle(self) -> "FakeLocator | None":
        return self if len(self.visible) == 1 else None

    async def evaluate(self, _expression: str, expected: object) -> bool:
        return expected is self

    async def click(self) -> None:
        if len(self.visible) != 1:
            raise RuntimeError("strict mode violation")
        self.clicked += 1

    def nth(self, index: int) -> "FakeCandidate":
        return FakeCandidate(self.visible[index])


class FakeCandidate:
    def __init__(self, visible: bool) -> None:
        self.visible = visible

    async def is_visible(self) -> bool:
        return self.visible


class FakePage:
    def __init__(self, matches: list[bool]) -> None:
        self.locator_value = FakeLocator(matches)
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def get_by_role(self, *args: object, **kwargs: object) -> FakeLocator:
        self.calls.append(("role", args, kwargs))
        return self.locator_value

    def get_by_label(self, *args: object, **kwargs: object) -> FakeLocator:
        self.calls.append(("label", args, kwargs))
        return self.locator_value

    def get_by_placeholder(self, *args: object, **kwargs: object) -> FakeLocator:
        self.calls.append(("placeholder", args, kwargs))
        return self.locator_value

    def get_by_text(self, *args: object, **kwargs: object) -> FakeLocator:
        self.calls.append(("text", args, kwargs))
        return self.locator_value

    def locator(self, selector: str) -> FakeLocator:
        self.calls.append(("css", (selector,), {}))
        return self.locator_value


def button_snapshot(*names: str) -> PageSnapshot:
    return PageSnapshot(
        url="https://example.test/",
        title="Example",
        visible_text=" ".join(names),
        elements=[
            {"kind": "button", "role": "button", "accessible_name": name, "text": name}
            for name in names
        ],
    )


class DynamicLocatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolves_unique_semantic_target(self) -> None:
        page = FakePage([True])
        resolver = DynamicLocatorResolver(page)
        result = await resolver.resolve(
            ElementTarget(kind="role", role="button", value="Login"),
            button_snapshot("Login"),
        )
        self.assertIs(result, page.locator_value)
        self.assertEqual(page.calls[0][0], "role")
        self.assertEqual(page.calls[0][2], {"name": "Login", "exact": True})

    async def test_native_search_input_role_resolves_as_searchbox(self) -> None:
        page = FakePage([True])
        snapshot = PageSnapshot(
            url="https://example.test/",
            title="Search",
            visible_text="",
            elements=[{
                "kind": "input", "role": "searchbox",
                "accessible_name": "Destination", "input_type": "search",
            }],
        )
        await DynamicLocatorResolver(page).resolve(
            ElementTarget(kind="role", role="searchbox", value="Destination"), snapshot
        )
        self.assertEqual(page.calls[0], ("role", ("searchbox",), {"name": "Destination", "exact": True}))

    async def test_refreshes_stale_snapshot_once(self) -> None:
        page = FakePage([True])
        inspector = AsyncMock(return_value=button_snapshot("Continue"))
        resolver = DynamicLocatorResolver(page, inspector=inspector)
        result = await resolver.resolve(
            ElementTarget(kind="role", role="button", value="Continue"),
            button_snapshot("Old button"),
        )
        self.assertIs(result, page.locator_value)
        inspector.assert_awaited_once_with(page)

    async def test_ambiguous_matches_are_rejected_without_choosing_first(self) -> None:
        page = FakePage([True, True])
        current = button_snapshot("Login", "Login")
        inspector = AsyncMock(return_value=current)
        resolver = DynamicLocatorResolver(page, inspector=inspector)
        with self.assertRaises(LocatorResolutionError) as raised:
            await resolver.resolve(
                ElementTarget(kind="role", role="button", value="Login"), current
            )
        self.assertEqual(raised.exception.category, "ambiguous")
        inspector.assert_not_awaited()

    async def test_hidden_duplicate_does_not_make_target_ambiguous(self) -> None:
        page = FakePage([False, True])
        resolver = DynamicLocatorResolver(page)
        result = await resolver.resolve(
            ElementTarget(kind="role", role="button", value="Login"),
            button_snapshot("Login"),
        )
        self.assertEqual(await result.count(), 2)

    async def test_unsupported_css_and_attributes_fail_before_locator_query(self) -> None:
        page = FakePage([True])
        resolver = DynamicLocatorResolver(page)
        for target in (
            ElementTarget(kind="css", value="button:nth-child(1)"),
            ElementTarget(kind="attribute", value="onclick=run()"),
        ):
            with self.subTest(target=target.kind), self.assertRaises(LocatorResolutionError) as raised:
                await resolver.resolve(target, button_snapshot("Login"))
            self.assertEqual(raised.exception.category, "unsupported")
        self.assertEqual(page.calls, [])

    async def test_simple_css_target_is_limited_to_current_snapshot(self) -> None:
        page = FakePage([True])
        resolver = DynamicLocatorResolver(page)
        await resolver.resolve(ElementTarget(kind="css", value="button"), button_snapshot("Login"))
        self.assertEqual(page.calls[0][0], "css")

    async def test_zero_live_matches_remain_a_missing_target(self) -> None:
        page = FakePage([])
        resolver = DynamicLocatorResolver(
            page, inspector=AsyncMock(return_value=button_snapshot("Login"))
        )
        with self.assertRaises(LocatorResolutionError) as raised:
            await resolver.resolve(
                ElementTarget(kind="role", role="button", value="Login"), button_snapshot("Login")
            )
        self.assertEqual(raised.exception.category, "missing")

    async def test_action_resolution_does_not_live_lookup_a_truncated_snapshot_miss(self) -> None:
        page = FakePage([True])
        truncated = PageSnapshot(
            url="https://example.test/",
            title="Example",
            visible_text="",
            elements=[],
            elements_truncated=True,
            truncated=True,
        )
        resolver = DynamicLocatorResolver(
            page, inspector=AsyncMock(return_value=truncated)
        )
        with self.assertRaises(LocatorResolutionError) as raised:
            await resolver.resolve(
                ElementTarget(kind="role", role="button", value="Search"), truncated
            )
        self.assertEqual(raised.exception.category, "missing")
        self.assertEqual(page.calls, [])

    async def test_playwright_timeout_and_closed_page_are_infrastructure_errors(self) -> None:
        class BrokenLocator(FakeLocator):
            def __init__(self, error: Exception) -> None:
                super().__init__([True])
                self.error = error

            async def count(self) -> int:
                raise self.error

        for error in (
            PlaywrightTimeoutError("private timeout details"),
            RuntimeError("private closed-page details"),
        ):
            page = FakePage([True])
            page.locator_value = BrokenLocator(error)
            resolver = DynamicLocatorResolver(page)
            with self.subTest(error=type(error).__name__), self.assertRaises(LocatorResolutionError) as raised:
                await resolver.resolve(
                    ElementTarget(kind="role", role="button", value="Login"), button_snapshot("Login")
                )
            self.assertEqual(raised.exception.category, "infrastructure")
            self.assertNotIn("private", str(raised.exception))

    async def test_dom_insertion_before_action_does_not_click_a_retargeted_index(self) -> None:
        class MutatingLocator(FakeLocator):
            def __init__(self) -> None:
                super().__init__([True])
                self.count_calls = 0

            async def count(self) -> int:
                self.count_calls += 1
                return 1 if self.count_calls == 1 else 2

        page = FakePage([True])
        page.locator_value = MutatingLocator()
        resolver = DynamicLocatorResolver(page)
        with self.assertRaises(LocatorResolutionError) as raised:
            await resolver.perform(
                ElementTarget(kind="role", role="button", value="Login"),
                button_snapshot("Login"),
                lambda locator: locator.click(),
            )
        self.assertEqual(raised.exception.category, "ambiguous")
        self.assertEqual(page.locator_value.clicked, 0)

    async def test_replacement_after_final_checks_never_receives_the_action(self) -> None:
        class DomState:
            current = "original"
            original_attached = True
            replacement_clicks = 0

        state = DomState()

        class BoundHandle:
            async def is_visible(self) -> bool:
                # The element is replaced immediately after the final visibility
                # check, reproducing a race at the action boundary.
                state.current = "replacement"
                state.original_attached = False
                return True

            async def click(self) -> None:
                if not state.original_attached:
                    raise RuntimeError("captured element detached")

            async def is_enabled(self) -> bool:
                return True

            async def is_editable(self) -> bool:
                return True

        handle = BoundHandle()

        class ReplacingLocator(FakeLocator):
            def __init__(self) -> None:
                super().__init__([True])

            async def element_handle(self) -> BoundHandle:
                return handle

            async def evaluate(self, _expression: str, expected: object) -> bool:
                return expected is handle and state.current == "original"

        page = FakePage([True])
        page.locator_value = ReplacingLocator()
        resolver = DynamicLocatorResolver(page)
        with self.assertRaisesRegex(RuntimeError, "detached"):
            await resolver.perform(
                ElementTarget(kind="role", role="button", value="Login"),
                button_snapshot("Login"),
                lambda exact_handle: exact_handle.click(),
            )
        self.assertEqual(state.current, "replacement")
        self.assertEqual(state.replacement_clicks, 0)

    async def test_disabled_or_noneditable_targets_fail_before_operation(self) -> None:
        class UnusableLocator(FakeLocator):
            def __init__(self, *, enabled: bool, editable: bool) -> None:
                super().__init__([True])
                self.enabled = enabled
                self.editable = editable

            async def is_enabled(self) -> bool:
                return self.enabled

            async def is_editable(self) -> bool:
                return self.editable

        for enabled, editable in ((False, True), (True, False)):
            page = FakePage([True])
            page.locator_value = UnusableLocator(enabled=enabled, editable=editable)
            resolver = DynamicLocatorResolver(page)
            acted = False

            async def operation(_handle: object) -> None:
                nonlocal acted
                acted = True

            with self.assertRaises(LocatorResolutionError) as raised:
                await resolver.perform(
                    ElementTarget(kind="role", role="button", value="Login"),
                    button_snapshot("Login"), operation, require_editable=True,
                )
            self.assertEqual(raised.exception.category, "not_interactable")
            self.assertFalse(acted)


if __name__ == "__main__":
    unittest.main()
