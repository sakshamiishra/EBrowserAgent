"""Offline contract tests for the HTTP run route and bounded schemas."""

from __future__ import annotations

import unittest
import asyncio
import importlib
from uuid import uuid4
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.errors import safe_request_validation_error
from app.api.routes import create_test_run, get_app_settings, health_check, router
from app.api.schemas import (
    ExecutionStepResult,
    MAX_EXECUTION_STEPS,
    ExecutionStatus,
    TestExecutionRequest,
    TestExecutionResponse,
    TestOutcome,
)
from app.config import Settings


class FakeOrchestrator:
    async def run(self, request: TestExecutionRequest) -> TestExecutionResponse:
        return TestExecutionResponse(
            run_id=uuid4(),
            status=ExecutionStatus.COMPLETED,
            outcome=TestOutcome.INCONCLUSIVE,
            summary="No required assertion was available to verify.",
            steps=[],
            verified_assertions=[],
        )


class ApiRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_route_returns_service_status(self) -> None:
        self.assertEqual(health_check(), {"status": "ok"})

    async def test_run_route_returns_typed_final_report(self) -> None:
        request = TestExecutionRequest(
            target_url="https://example.test/",
            instruction="Verify the heading.",
        )
        response = await create_test_run(request, FakeOrchestrator())
        validated = TestExecutionResponse.model_validate(response.model_dump())
        self.assertEqual(validated.status, ExecutionStatus.COMPLETED)
        self.assertEqual(validated.outcome, TestOutcome.INCONCLUSIVE)

    async def test_request_limits_are_enforced_before_orchestration(self) -> None:
        with self.assertRaises(ValidationError):
            TestExecutionRequest(
                target_url="https://example.test/",
                instruction="x" * 4_001,
            )
        with self.assertRaises(ValidationError):
            TestExecutionRequest(
                target_url="https://example.test/",
                instruction="Run a test.",
                test_data={str(index): "x" for index in range(21)},
            )
        with self.assertRaises(ValidationError):
            TestExecutionRequest(
                target_url="https://example.test/",
                instruction="Run a test.",
                test_data={"email": "x" * 1_001},
            )

    async def test_bounded_step_report_can_retain_all_occurrence_attempts(self) -> None:
        steps = [
            ExecutionStepResult(
                index=index, action_type="click", status="SKIPPED",
                explanation="Not attempted.", occurrence_id=f"action-{index:03d}",
            )
            for index in range(MAX_EXECUTION_STEPS)
        ]
        response = TestExecutionResponse(
            run_id=uuid4(), status=ExecutionStatus.COMPLETED,
            outcome=TestOutcome.INCONCLUSIVE, summary="Incomplete actions.", steps=steps,
        )
        self.assertEqual(len(response.steps), MAX_EXECUTION_STEPS)
        with self.assertRaises(ValidationError):
            TestExecutionResponse(
                run_id=uuid4(), status=ExecutionStatus.COMPLETED,
                outcome=TestOutcome.INCONCLUSIVE, summary="Incomplete actions.",
                steps=steps + [steps[-1]],
            )

    async def test_validation_error_response_does_not_echo_submitted_secret(self) -> None:
        secret = "private-test-value"
        try:
            TestExecutionRequest(
                target_url="https://example.test/",
                instruction="Run a test.",
                test_data={"email": secret * 100},
            )
        except ValidationError as exc:
            request_error = RequestValidationError(exc.errors())
        else:
            self.fail("the oversized test-data value should fail validation")
        response: JSONResponse = await safe_request_validation_error(SimpleNamespace(), request_error)
        body = response.body.decode("utf-8")
        self.assertNotIn(secret, body)
        self.assertIn("Request validation failed", body)

    async def test_application_asgi_handler_safely_rejects_invalid_request(self) -> None:
        secret = "synthetic-invalid-request-secret"
        settings = Settings(gemini_api_key="synthetic-key-not-used")
        # Import the configured application with synthetic settings so this test
        # exercises its registered exception handler without reading .env.
        with patch("app.config.get_settings", return_value=settings):
            application_module = importlib.import_module("app.main")
        application = application_module.app
        application.dependency_overrides[get_app_settings] = lambda: settings

        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://testserver"
        ) as client:
            response = await client.post("/api/runs", json={
                "target_url": "https://example.test/",
                "instruction": "Run a test.",
                "test_data": {"email": secret * 100},
            })

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json(), {
            "detail": "Request validation failed. Check the URL, instruction, and test-data limits."
        })
        self.assertNotIn(secret, response.text)

    async def test_asgi_run_endpoint_uses_dependency_injection_and_serializes_reports(self) -> None:
        app = FastAPI()
        app.include_router(router, prefix="/api")
        settings = Settings(
            gemini_api_key="synthetic-key-not-used",
            run_timeout_seconds=12.5,
            cleanup_grace_seconds=0.2,
        )
        app.dependency_overrides[get_app_settings] = lambda: settings
        issued_outcomes = [TestOutcome.PASS, TestOutcome.INCONCLUSIVE]
        constructed: list[dict[str, object]] = []
        providers: list[object] = []

        class RequestProvider:
            def __init__(self, *, settings: Settings) -> None:
                self.settings = settings
                self.aclose = AsyncMock()
                providers.append(self)

        class CapturingOrchestrator:
            def __init__(self, _planner: object, *, run_timeout_seconds: float,
                         cleanup_grace_seconds: float) -> None:
                constructed.append({
                    "run_timeout_seconds": run_timeout_seconds,
                    "cleanup_grace_seconds": cleanup_grace_seconds,
                })

            async def run(self, _request: TestExecutionRequest) -> TestExecutionResponse:
                outcome = issued_outcomes.pop(0)
                steps = [] if outcome is TestOutcome.INCONCLUSIVE else [
                    ExecutionStepResult(
                        index=0, action_type="click", status="PASS",
                        explanation="Action completed.", occurrence_id="action-001",
                        attempt_number=1,
                    )
                ]
                return TestExecutionResponse(
                    run_id=uuid4(), status="COMPLETED", outcome=outcome,
                    summary="Synthetic API test result.", steps=steps,
                    verified_assertions=[],
                )

        request_body = {
            "target_url": "https://example.test/",
            "instruction": "Use the supplied value only if required.",
            "test_data": {
                "email": "synthetic-sensitive-value",
                "password": "synthetic-password-value",
            },
        }
        with patch("app.planner.factory.GeminiProvider", RequestProvider), patch(
            "app.api.routes.TestOrchestrator", CapturingOrchestrator
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                passed = await client.post("/api/runs", json=request_body)
                inconclusive = await client.post("/api/runs", json=request_body)

        self.assertEqual(passed.status_code, 200)
        self.assertEqual(passed.json()["outcome"], "PASS")
        self.assertEqual(passed.json()["steps"][0]["occurrence_id"], "action-001")
        self.assertEqual(inconclusive.status_code, 200)
        self.assertEqual(inconclusive.json()["outcome"], "INCONCLUSIVE")
        self.assertEqual(
            constructed,
            [
                {"run_timeout_seconds": 12.5, "cleanup_grace_seconds": 0.2},
                {"run_timeout_seconds": 12.5, "cleanup_grace_seconds": 0.2},
            ],
        )
        self.assertEqual(len(providers), 2)
        for provider in providers:
            provider.aclose.assert_awaited_once()
        response_text = passed.text + inconclusive.text
        self.assertNotIn("synthetic-sensitive-value", response_text)
        self.assertNotIn("synthetic-password-value", response_text)

    async def test_asgi_provider_cleanup_hang_is_bounded_and_does_not_mask_response(self) -> None:
        app = FastAPI()
        app.include_router(router, prefix="/api")
        app.dependency_overrides[get_app_settings] = lambda: Settings(
            gemini_api_key="synthetic-key-not-used",
            cleanup_grace_seconds=0.1,
        )
        cleanup_started = asyncio.Event()
        cleanup_cancelled = asyncio.Event()

        class HangingProvider:
            def __init__(self, *, settings: Settings) -> None:
                async def close() -> None:
                    cleanup_started.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cleanup_cancelled.set()
                self.aclose = close

        class PassingOrchestrator:
            def __init__(self, _planner: object, **_kwargs: object) -> None:
                pass

            async def run(self, _request: TestExecutionRequest) -> TestExecutionResponse:
                return TestExecutionResponse(
                    run_id=uuid4(), status="COMPLETED", outcome=TestOutcome.PASS,
                    summary="Synthetic successful assertion.", steps=[], verified_assertions=[],
                )

        with patch("app.planner.factory.GeminiProvider", HangingProvider), patch(
            "app.api.routes.TestOrchestrator", PassingOrchestrator
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                response = await asyncio.wait_for(
                    client.post("/api/runs", json={
                        "target_url": "https://example.test/",
                        "instruction": "Verify the synthetic confirmation.",
                    }),
                    timeout=0.5,
                )

        self.assertTrue(cleanup_started.is_set())
        self.assertTrue(cleanup_cancelled.is_set())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["outcome"], "PASS")


if __name__ == "__main__":
    unittest.main()
