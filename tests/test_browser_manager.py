from __future__ import annotations

import asyncio
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.browser.manager import browser_session, navigate_page
from app.security.url_policy import URLPolicyError


class BrowserManagerTests(unittest.IsolatedAsyncioTestCase):
    def make_playwright_modules(self, *, navigation_error: Exception | None = None,
                                context_error: Exception | None = None,
                                page_close_error: Exception | None = None):
        page = MagicMock()
        page.close = AsyncMock(side_effect=page_close_error)
        page.goto = AsyncMock(side_effect=navigation_error)
        page.set_default_navigation_timeout = MagicMock()
        page.set_default_timeout = MagicMock()

        context = MagicMock()
        context.new_page = AsyncMock(return_value=page)
        context.close = AsyncMock()

        browser = MagicMock()
        browser.new_context = AsyncMock(side_effect=context_error, return_value=context)
        browser.close = AsyncMock()

        playwright = MagicMock()
        playwright.chromium.launch = AsyncMock(return_value=browser)
        playwright.stop = AsyncMock()

        manager = MagicMock()
        manager.start = AsyncMock(return_value=playwright)
        api_module = types.ModuleType("playwright.async_api")
        api_module.async_playwright = MagicMock(return_value=manager)
        parent_module = types.ModuleType("playwright")
        parent_module.async_api = api_module
        return {
            "playwright": parent_module,
            "playwright.async_api": api_module,
        }, manager, playwright, browser, context, page

    def patch_dependencies(self, modules):
        stack = patch.dict(sys.modules, modules)
        stack.start()
        self.addCleanup(stack.stop)
        return (
            patch("app.browser.manager.validate_target_url", new=AsyncMock(side_effect=lambda url: url)),
            patch("app.browser.manager.install_context_url_guard", new=AsyncMock()),
        )

    async def test_cleanup_grace_validation_matches_settings_bounds(self) -> None:
        for grace in (0.1, 60.0):
            modules, _manager, _playwright, _browser, _context, _page = self.make_playwright_modules()
            validate_patch, guard_patch = self.patch_dependencies(modules)
            with self.subTest(grace=grace), validate_patch, guard_patch:
                async with browser_session(cleanup_grace_seconds=grace):
                    pass

        for grace in (-1, 0, 0.09, 60.01, float("nan"), float("inf"), -float("inf"), True):
            with self.subTest(grace=grace), self.assertRaises(ValueError):
                async with browser_session(cleanup_grace_seconds=grace):
                    self.fail("invalid cleanup grace must fail before browser startup")

    async def test_navigation_helper_uses_validated_canonical_url(self) -> None:
        page = MagicMock()
        page.goto = AsyncMock(return_value="navigation response")
        with patch(
            "app.browser.manager.validate_target_url",
            new=AsyncMock(return_value="https://example.com/canonical"),
        ) as validate:
            result = await navigate_page(page, "HTTPS://EXAMPLE.com./canonical")
        validate.assert_awaited_once_with("HTTPS://EXAMPLE.com./canonical")
        page.goto.assert_awaited_once_with(
            "https://example.com/canonical", wait_until="domcontentloaded"
        )
        self.assertEqual(result, "navigation response")

    async def test_navigation_helper_rejects_unsafe_url_before_goto(self) -> None:
        page = MagicMock()
        page.goto = AsyncMock()
        with patch(
            "app.browser.manager.validate_target_url",
            new=AsyncMock(side_effect=URLPolicyError("unsafe destination")),
        ):
            with self.assertRaisesRegex(URLPolicyError, "unsafe destination"):
                await navigate_page(page, "http://127.0.0.1/")
        page.goto.assert_not_awaited()

    async def test_navigation_helper_rejects_unsupported_scheme(self) -> None:
        page = MagicMock()
        page.goto = AsyncMock()
        with self.assertRaises(URLPolicyError):
            await navigate_page(page, "file:///etc/passwd")
        page.goto.assert_not_awaited()

    async def test_navigation_errors_are_propagated(self) -> None:
        page = MagicMock()
        page.goto = AsyncMock(side_effect=RuntimeError("navigation failed"))
        with patch(
            "app.browser.manager.validate_target_url",
            new=AsyncMock(return_value="https://example.com/"),
        ):
            with self.assertRaisesRegex(RuntimeError, "navigation failed"):
                await navigate_page(page, "https://example.com/")
        page.goto.assert_awaited_once_with(
            "https://example.com/", wait_until="domcontentloaded"
        )

    async def test_successful_session_closes_all_resources(self) -> None:
        modules, manager, playwright, browser, context, page = self.make_playwright_modules()
        validate_patch, guard_patch = self.patch_dependencies(modules)
        with validate_patch, guard_patch:
            async with browser_session("https://example.com") as session:
                self.assertIs(session.page, page)
        page.close.assert_awaited_once()
        page.goto.assert_awaited_once_with(
            "https://example.com", wait_until="domcontentloaded"
        )
        context.close.assert_awaited_once()
        browser.close.assert_awaited_once()
        playwright.stop.assert_awaited_once()

    async def test_partial_initialization_failure_cleans_initialized_resources(self) -> None:
        modules, _manager, playwright, browser, _context, _page = self.make_playwright_modules(
            context_error=RuntimeError("context creation failed")
        )
        validate_patch, guard_patch = self.patch_dependencies(modules)
        with validate_patch, guard_patch, self.assertRaisesRegex(RuntimeError, "context creation"):
            async with browser_session():
                self.fail("session should not be entered")
        browser.close.assert_awaited_once()
        playwright.stop.assert_awaited_once()

    async def test_initial_navigation_failure_preserves_original_error_and_cleans_up(self) -> None:
        modules, _manager, playwright, browser, context, page = self.make_playwright_modules(
            navigation_error=RuntimeError("navigation failed"),
            page_close_error=RuntimeError("secondary cleanup failure"),
        )
        validate_patch, guard_patch = self.patch_dependencies(modules)
        with validate_patch, guard_patch, self.assertLogs("app.browser.manager", level="WARNING"):
            with self.assertRaisesRegex(RuntimeError, "navigation failed"):
                async with browser_session("https://example.com"):
                    self.fail("session should not be entered")
        page.close.assert_awaited_once()
        context.close.assert_awaited_once()
        browser.close.assert_awaited_once()
        playwright.stop.assert_awaited_once()

    async def test_cancelled_cleanup_step_does_not_skip_later_resources(self) -> None:
        modules, _manager, playwright, browser, context, page = self.make_playwright_modules(
            page_close_error=asyncio.CancelledError()
        )
        validate_patch, guard_patch = self.patch_dependencies(modules)
        with validate_patch, guard_patch, self.assertLogs("app.browser.manager", level="WARNING"):
            with self.assertRaises(asyncio.CancelledError):
                async with browser_session():
                    pass
        context.close.assert_awaited_once()
        browser.close.assert_awaited_once()
        playwright.stop.assert_awaited_once()

    async def test_close_failure_is_logged_and_later_resources_still_close(self) -> None:
        modules, _manager, playwright, browser, context, page = self.make_playwright_modules(
            page_close_error=RuntimeError("page close failed")
        )
        validate_patch, guard_patch = self.patch_dependencies(modules)
        with validate_patch, guard_patch, self.assertLogs("app.browser.manager", level="WARNING"):
            with self.assertRaisesRegex(RuntimeError, "Failed to close 1"):
                async with browser_session():
                    pass
        context.close.assert_awaited_once()
        browser.close.assert_awaited_once()
        playwright.stop.assert_awaited_once()

    async def test_hanging_cleanup_is_bounded_and_remaining_resources_are_attempted(self) -> None:
        close_started = asyncio.Event()
        close_cancelled = asyncio.Event()

        async def hanging_close() -> None:
            close_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                close_cancelled.set()

        modules, _manager, playwright, browser, context, page = self.make_playwright_modules()
        page.close = AsyncMock(side_effect=hanging_close)
        validate_patch, guard_patch = self.patch_dependencies(modules)
        entered = asyncio.Event()

        async def open_session() -> None:
            async with browser_session(cleanup_grace_seconds=0.1):
                entered.set()
                await asyncio.Event().wait()

        with validate_patch, guard_patch, self.assertLogs(
            "app.browser.manager", level="WARNING"
        ):
            task = asyncio.create_task(open_session())
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=0.5)

        self.assertTrue(close_started.is_set())
        self.assertTrue(close_cancelled.is_set())
        context.close.assert_awaited_once()
        browser.close.assert_awaited_once()
        playwright.stop.assert_awaited_once()
        await asyncio.sleep(0)
        self.assertEqual(
            {task for task in asyncio.all_tasks() if task is not asyncio.current_task()},
            set(),
        )

    async def test_sessions_create_independent_contexts(self) -> None:
        modules, _manager, _playwright, browser, _context, _page = self.make_playwright_modules()
        context_a = MagicMock()
        context_a.new_page = AsyncMock(return_value=MagicMock(
            close=AsyncMock(), set_default_navigation_timeout=MagicMock(), set_default_timeout=MagicMock()
        ))
        context_a.close = AsyncMock()
        context_b = MagicMock()
        context_b.new_page = AsyncMock(return_value=MagicMock(
            close=AsyncMock(), set_default_navigation_timeout=MagicMock(), set_default_timeout=MagicMock()
        ))
        context_b.close = AsyncMock()
        browser.new_context = AsyncMock(side_effect=[context_a, context_b])
        validate_patch, guard_patch = self.patch_dependencies(modules)
        with validate_patch, guard_patch:
            async with browser_session():
                pass
            async with browser_session():
                pass
        self.assertEqual(browser.new_context.await_count, 2)
        self.assertIsNot(context_a, context_b)


if __name__ == "__main__":
    unittest.main()
