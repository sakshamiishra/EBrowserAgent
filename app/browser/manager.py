"""Isolated asynchronous Playwright browser sessions."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator

from app.config import DEFAULT_CLEANUP_GRACE_SECONDS, validate_cleanup_grace_seconds
from app.security.url_policy import install_context_url_guard, validate_target_url

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class BrowserSession:
    """Resources owned by one isolated test session."""

    playwright: Any
    browser: Any
    context: Any
    page: Any


async def navigate_page(
    page: Any,
    destination_url: str,
    *,
    wait_until: str = "domcontentloaded",
) -> Any:
    """Validate and canonicalize an application-initiated page navigation.

    Future application code should use this helper instead of calling
    ``page.goto()`` directly. The raw Playwright page remains available for
    ordinary interactions. This helper and the context request guard do not
    cover every browser-owned navigation or network path.
    """

    safe_url = await validate_target_url(destination_url)
    return await page.goto(safe_url, wait_until=wait_until)


@asynccontextmanager
async def browser_session(
    initial_url: str | None = None,
    *,
    headless: bool = True,
    navigation_timeout_ms: int = 30_000,
    operation_timeout_ms: int = 10_000,
    cleanup_grace_seconds: float = DEFAULT_CLEANUP_GRACE_SECONDS,
) -> AsyncIterator[BrowserSession]:
    """Start an isolated Chromium session and reliably release its resources.

    Playwright is imported lazily so the rest of the application and unit tests
    can be used before the package and browser binary are installed.
    """

    if navigation_timeout_ms <= 0 or operation_timeout_ms <= 0:
        raise ValueError("Browser timeouts must be positive")
    cleanup_grace_seconds = validate_cleanup_grace_seconds(cleanup_grace_seconds)
    from playwright.async_api import async_playwright

    playwright = browser = context = page = None
    body_failed = False
    try:
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(headless=headless)
        context = await browser.new_context(service_workers="block")
        await install_context_url_guard(context)
        page = await context.new_page()
        page.set_default_navigation_timeout(navigation_timeout_ms)
        page.set_default_timeout(operation_timeout_ms)
        session = BrowserSession(playwright, browser, context, page)
        if initial_url is not None:
            await navigate_page(page, initial_url, wait_until="domcontentloaded")
        yield session
    except BaseException:
        body_failed = True
        raise
    finally:
        cleanup_failure_count = 0
        cleanup_cancellation: asyncio.CancelledError | None = None
        resources = [
            (page, "close"),
            (context, "close"),
            (browser, "close"),
            (playwright, "stop"),
        ]
        resources = [(resource, method) for resource, method in resources if resource is not None]
        cleanup_deadline = asyncio.get_running_loop().time() + cleanup_grace_seconds
        for index, (resource, close_method) in enumerate(resources):
            remaining_resources = len(resources) - index
            remaining_time = max(0.0, cleanup_deadline - asyncio.get_running_loop().time())
            resource_budget = remaining_time / remaining_resources
            try:
                # One shared grace window is divided among remaining resources,
                # so a hung page close cannot consume the browser/context attempts.
                await asyncio.wait_for(
                    getattr(resource, close_method)(), timeout=resource_budget
                )
            except asyncio.TimeoutError:
                cleanup_failure_count += 1
                logger.warning("Timed out closing browser resource %s", type(resource).__name__)
            except asyncio.CancelledError as exc:
                cleanup_cancellation = cleanup_cancellation or exc
                logger.warning(
                    "Cleanup of browser resource %s was cancelled; continuing cleanup",
                    type(resource).__name__,
                )
            except Exception:
                cleanup_failure_count += 1
                logger.warning("Failed to close browser resource %s", type(resource).__name__)

        # Do not replace an exception from setup or the session body with a
        # cleanup error. If the session body succeeded, make cleanup failure
        # visible to its caller after attempting every resource.
        if not body_failed:
            if cleanup_cancellation is not None:
                raise cleanup_cancellation
            if cleanup_failure_count:
                raise RuntimeError(
                    f"Failed to close {cleanup_failure_count} browser session resource(s)"
                ) from None
