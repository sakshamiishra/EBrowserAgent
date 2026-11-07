"""Offline tests for the direct Gemini generation endpoint diagnostic."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from google.genai.errors import ClientError

from app.config import Settings
from scripts.gemini_generation_connectivity_test import (
    MAX_OUTPUT_TOKENS,
    PROMPT,
    format_result,
    run_diagnostic,
)


class GeminiGenerationConnectivityTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_makes_one_minimal_generation_call_and_closes_client(self) -> None:
        generate = AsyncMock(return_value=SimpleNamespace(text="private response"))
        close = AsyncMock()
        client = SimpleNamespace(
            aio=SimpleNamespace(
                models=SimpleNamespace(generate_content=generate),
                aclose=close,
            )
        )
        result = await run_diagnostic(
            settings_loader=lambda: Settings(
                gemini_api_key="AQ-test-secret",
                gemini_model="gemini-test-model",
                gemini_request_timeout_seconds=12.5,
            ),
            client_factory=lambda _key: client,
        )

        self.assertTrue(result.succeeded)
        self.assertTrue(result.cleanup_succeeded)
        generate.assert_awaited_once_with(
            model="gemini-test-model",
            contents=PROMPT,
            config={"max_output_tokens": MAX_OUTPUT_TOKENS},
        )
        close.assert_awaited_once_with()
        self.assertEqual(result.model, "gemini-test-model")
        output = format_result(result)
        self.assertIn("Generation request: SUCCESS", output)
        self.assertNotIn("private response", output)
        self.assertNotIn("AQ-test-secret", output)

    async def test_request_failure_reports_safe_diagnostics_and_closes_client(self) -> None:
        secret = "AQ-test-secret"
        sdk_error = ClientError(
            503,
            {"status": "UNAVAILABLE", "message": f"Authorization {secret} private body"},
        )
        generate = AsyncMock(side_effect=sdk_error)
        close = AsyncMock()
        client = SimpleNamespace(
            aio=SimpleNamespace(
                models=SimpleNamespace(generate_content=generate),
                aclose=close,
            )
        )
        result = await run_diagnostic(
            settings_loader=lambda: Settings(gemini_api_key=secret),
            client_factory=lambda _key: client,
        )

        self.assertFalse(result.succeeded)
        self.assertEqual(result.diagnostic, ("server_error", 503, "UNAVAILABLE"))
        self.assertTrue(result.cleanup_succeeded)
        close.assert_awaited_once_with()
        output = format_result(result)
        for sensitive_text in (secret, "Authorization", "private body"):
            self.assertNotIn(sensitive_text, output)
        self.assertIn("HTTP status: 503", output)
        self.assertIn("Provider reason: UNAVAILABLE", output)

    async def test_cleanup_failure_is_separate_and_does_not_mask_request_failure(self) -> None:
        secret = "AQ-test-secret"
        sdk_error = ClientError(
            403,
            {"status": "PERMISSION_DENIED", "message": f"private {secret} body"},
        )
        generate = AsyncMock(side_effect=sdk_error)
        close = AsyncMock(side_effect=RuntimeError(f"private cleanup {secret}"))
        client = SimpleNamespace(
            aio=SimpleNamespace(
                models=SimpleNamespace(generate_content=generate),
                aclose=close,
            )
        )
        result = await run_diagnostic(
            settings_loader=lambda: Settings(gemini_api_key=secret),
            client_factory=lambda _key: client,
        )

        self.assertFalse(result.succeeded)
        self.assertEqual(
            result.diagnostic,
            ("authentication_authorization", 403, "PERMISSION_DENIED"),
        )
        self.assertFalse(result.cleanup_succeeded)
        output = format_result(result)
        self.assertIn("Generation request: FAILURE", output)
        self.assertIn("Client cleanup: FAILURE", output)
        self.assertIn("HTTP status: 403", output)
        self.assertIn("Provider reason: PERMISSION_DENIED", output)
        for sensitive_text in (secret, "private body", "private cleanup"):
            self.assertNotIn(sensitive_text, output)

    async def test_configured_timeout_is_enforced_without_increasing_it(self) -> None:
        started = asyncio.Event()

        async def wait_for_timeout(**_: object) -> None:
            started.set()
            await asyncio.Event().wait()

        generate = AsyncMock(side_effect=wait_for_timeout)
        close = AsyncMock()
        client = SimpleNamespace(
            aio=SimpleNamespace(
                models=SimpleNamespace(generate_content=generate),
                aclose=close,
            )
        )
        result = await run_diagnostic(
            settings_loader=lambda: Settings(
                gemini_api_key="AQ-test-secret",
                gemini_request_timeout_seconds=0.01,
            ),
            client_factory=lambda _key: client,
        )

        self.assertTrue(started.is_set())
        self.assertFalse(result.succeeded)
        self.assertEqual(result.diagnostic, ("timeout", None, None))
        generate.assert_awaited_once()
        close.assert_awaited_once_with()

    async def test_model_output_is_redacted_if_it_contains_the_api_key(self) -> None:
        secret = "AQ-test-secret"
        generate = AsyncMock(return_value=SimpleNamespace())
        close = AsyncMock()
        client = SimpleNamespace(
            aio=SimpleNamespace(
                models=SimpleNamespace(generate_content=generate),
                aclose=close,
            )
        )
        result = await run_diagnostic(
            settings_loader=lambda: Settings(
                gemini_api_key=secret,
                gemini_model=f"gemini-{secret}",
            ),
            client_factory=lambda _key: client,
        )

        self.assertEqual(result.model, "[redacted]")
        self.assertNotIn(secret, format_result(result))


if __name__ == "__main__":
    unittest.main()
