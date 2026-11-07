"""Health and browser-test execution API routes."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException

from app.api.schemas import TestExecutionRequest, TestExecutionResponse
from app.config import Settings, get_settings
from app.orchestration.runner import TestOrchestrator
from app.planner.factory import create_planner_provider
from app.planner.provider import PlannerProviderError
from app.planner.service import PlannerService

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health", tags=["health"])
def health_check() -> dict[str, str]:
    """Report that the API process is responsive."""

    return {"status": "ok"}


def get_app_settings() -> Settings:
    """Expose validated settings as an overridable FastAPI dependency."""

    try:
        return get_settings()
    except Exception:
        raise HTTPException(
            status_code=503, detail="Application configuration is unavailable"
        ) from None


async def get_test_orchestrator(
    settings: Settings = Depends(get_app_settings),
) -> AsyncIterator[TestOrchestrator]:
    """Create one request-scoped provider and close its client after the run."""

    try:
        provider = create_planner_provider(settings)
    except PlannerProviderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    try:
        yield TestOrchestrator(
            PlannerService(provider),
            run_timeout_seconds=settings.run_timeout_seconds,
            cleanup_grace_seconds=settings.cleanup_grace_seconds,
        )
    finally:
        try:
            await asyncio.wait_for(
                provider.aclose(), timeout=settings.cleanup_grace_seconds
            )
        except asyncio.CancelledError:
            logger.warning("Planner provider cleanup was cancelled")
            raise
        except asyncio.TimeoutError:
            logger.warning("Planner provider cleanup exceeded its grace period")
        except Exception:
            # Provider errors are never logged verbatim.
            logger.warning("Planner provider cleanup failed after the test run")


@router.post("/runs", tags=["runs"], response_model=TestExecutionResponse)
async def create_test_run(
    request: TestExecutionRequest,
    orchestrator: TestOrchestrator = Depends(get_test_orchestrator),
) -> TestExecutionResponse:
    """Execute one request and return only the bounded final report."""

    return await orchestrator.run(request)
