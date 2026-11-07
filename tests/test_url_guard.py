from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from app.security.url_policy import URLPolicyError, install_context_url_guard


class FakeContext:
    def __init__(self) -> None:
        self.route_handler = None
        self.websocket_handler = None

    async def route(self, _pattern: str, handler) -> None:
        self.route_handler = handler

    async def route_web_socket(self, _pattern: str, handler) -> None:
        self.websocket_handler = handler


class FakeRoute:
    def __init__(self, url: str) -> None:
        self.request = type("Request", (), {"url": url})()
        self.continue_ = AsyncMock()
        self.abort = AsyncMock()


class RequestGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_safe_http_and_https_requests_continue_to_canonical_url(self) -> None:
        context = FakeContext()
        await install_context_url_guard(context)
        for url, canonical in (
            ("http://EXAMPLE.com./page", "http://example.com/page"),
            ("https://example.com/page", "https://example.com/page"),
        ):
            route = FakeRoute(url)
            with self.subTest(url=url), patch(
                "app.security.url_policy.validate_target_url", new=AsyncMock(return_value=canonical)
            ):
                await context.route_handler(route)
            route.continue_.assert_awaited_once_with(url=canonical)
            route.abort.assert_not_awaited()

    async def test_private_destination_aborts(self) -> None:
        context = FakeContext()
        await install_context_url_guard(context)
        route = FakeRoute("http://127.0.0.1/")
        with patch(
            "app.security.url_policy.validate_target_url",
            new=AsyncMock(side_effect=URLPolicyError("private address")),
        ):
            await context.route_handler(route)
        route.abort.assert_awaited_once_with("blockedbyclient")
        route.continue_.assert_not_awaited()

    async def test_dns_failure_aborts(self) -> None:
        context = FakeContext()
        await install_context_url_guard(context)
        route = FakeRoute("https://unresolvable.example/")
        with patch(
            "app.security.url_policy.validate_target_url",
            new=AsyncMock(side_effect=URLPolicyError("DNS failure")),
        ):
            await context.route_handler(route)
        route.abort.assert_awaited_once_with("blockedbyclient")

    async def test_unsupported_scheme_aborts_without_dns_validation(self) -> None:
        context = FakeContext()
        await install_context_url_guard(context)
        route = FakeRoute("file:///etc/passwd")
        with patch("app.security.url_policy.validate_target_url", new=AsyncMock()) as validate:
            await context.route_handler(route)
        validate.assert_not_awaited()
        route.abort.assert_awaited_once_with("blockedbyclient")
        route.continue_.assert_not_awaited()

    async def test_websocket_connections_are_closed_before_server_connect(self) -> None:
        context = FakeContext()
        await install_context_url_guard(context)
        web_socket = type("FakeWebSocketRoute", (), {"close": AsyncMock()})()
        await context.websocket_handler(web_socket)
        web_socket.close.assert_awaited_once_with(
            code=1008, reason="WebSocket connections are disabled"
        )


if __name__ == "__main__":
    unittest.main()
