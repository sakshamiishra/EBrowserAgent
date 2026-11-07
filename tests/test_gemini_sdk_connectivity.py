"""Offline tests for the authenticated Gemini metadata diagnostic."""

from __future__ import annotations

import unittest
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from google.genai.errors import ClientError

from app.config import Settings
from scripts.gemini_sdk_connectivity_test import format_result, run_diagnostic


class GeminiSDKConnectivityTests(unittest.IsolatedAsyncioTestCase):
    async def test_metadata_success_reports_only_safe_summary_and_closes_client(self) -> None:
        get_model = AsyncMock(return_value=SimpleNamespace(name="private-model-metadata"))
        close = AsyncMock()
        client = SimpleNamespace(
            aio=SimpleNamespace(
                models=SimpleNamespace(get=get_model),
                aclose=close,
            )
        )
        result = await run_diagnostic(
            settings_loader=lambda: Settings(gemini_api_key="AQ-test-secret"),
            client_factory=lambda _key: client,
            sdk_version_reader=lambda: "2.34.0",
        )

        self.assertTrue(result.request_succeeded)
        self.assertTrue(result.cleanup_succeeded)
        self.assertGreaterEqual(result.elapsed_seconds, 0)
        get_model.assert_awaited_once_with(model="gemini-3.7-flash")
        close.assert_awaited_once_with()
        output = format_result(result)
        self.assertIn("Google Gen AI SDK version: 2.34.0", output)
        self.assertIn("Model metadata request: SUCCESS", output)
        self.assertNotIn("private-model-metadata", output)
        self.assertNotIn("AQ-test-secret", output)

    async def test_metadata_request_failure_is_sanitized_and_client_closes(self) -> None:
        secret = "AQ-test-secret"
        sdk_error = ClientError(
            403,
            {
                "status": "PERMISSION_DENIED",
                "message": f"Authorization: Bearer {secret}; private response body",
            },
        )
        get_model = AsyncMock(side_effect=sdk_error)
        close = AsyncMock()
        client = SimpleNamespace(
            aio=SimpleNamespace(
                models=SimpleNamespace(get=get_model),
                aclose=close,
            )
        )
        result = await run_diagnostic(
            settings_loader=lambda: Settings(gemini_api_key=secret),
            client_factory=lambda _key: client,
            sdk_version_reader=lambda: "2.34.0",
        )

        self.assertFalse(result.request_succeeded)
        self.assertEqual(
            result.request_diagnostic,
            ("authentication_authorization", 403, "PERMISSION_DENIED"),
        )
        self.assertTrue(result.cleanup_succeeded)
        close.assert_awaited_once_with()
        output = format_result(result)
        self.assertIn("HTTP status: 403", output)
        self.assertIn("Provider reason: PERMISSION_DENIED", output)
        for sensitive_text in (secret, "Authorization", "private response body"):
            self.assertNotIn(sensitive_text, output)

    async def test_cleanup_failure_is_reported_separately_from_request_failure(self) -> None:
        secret = "AQ-test-secret"
        request_error = ClientError(
            503,
            {"status": "UNAVAILABLE", "message": f"private {secret} response"},
        )
        get_model = AsyncMock(side_effect=request_error)
        close = AsyncMock(side_effect=RuntimeError(f"cleanup detail {secret}"))
        client = SimpleNamespace(
            aio=SimpleNamespace(
                models=SimpleNamespace(get=get_model),
                aclose=close,
            )
        )
        result = await run_diagnostic(
            settings_loader=lambda: Settings(gemini_api_key=secret),
            client_factory=lambda _key: client,
            sdk_version_reader=lambda: "2.34.0",
        )

        self.assertFalse(result.request_succeeded)
        self.assertEqual(
            result.request_diagnostic,
            ("server_error", 503, "UNAVAILABLE"),
        )
        self.assertFalse(result.cleanup_succeeded)
        self.assertEqual(
            result.cleanup_diagnostic,
            ("unknown_provider_error", None, None),
        )
        output = format_result(result)
        self.assertIn("Model metadata request: FAILURE", output)
        self.assertIn("Client cleanup: FAILURE", output)
        self.assertIn("HTTP status: 503", output)
        self.assertIn("Provider reason: UNAVAILABLE", output)
        for sensitive_text in (secret, "private response", "cleanup detail"):
            self.assertNotIn(sensitive_text, output)

    async def test_cancellation_still_attempts_client_cleanup(self) -> None:
        request_started = asyncio.Event()

        async def wait_until_cancelled(**_: object) -> None:
            request_started.set()
            await asyncio.Event().wait()

        get_model = AsyncMock(side_effect=wait_until_cancelled)
        close = AsyncMock()
        client = SimpleNamespace(
            aio=SimpleNamespace(
                models=SimpleNamespace(get=get_model),
                aclose=close,
            )
        )
        task = asyncio.create_task(
            run_diagnostic(
                settings_loader=lambda: Settings(gemini_api_key="AQ-test-secret"),
                client_factory=lambda _key: client,
                sdk_version_reader=lambda: "2.34.0",
            )
        )
        await request_started.wait()
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task
        close.assert_awaited_once_with()


if __name__ == "__main__":
    unittest.main()
