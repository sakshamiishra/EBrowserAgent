"""Diagnose Gemini content generation without involving the browser planner."""

from __future__ import annotations

import asyncio
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import Settings, get_settings
from app.planner.gemini import _classify_provider_exception
from app.planner.provider import (
    SAFE_PROVIDER_DIAGNOSTIC_CATEGORIES,
    SAFE_PROVIDER_REASON_CODES,
)


PROMPT = "Reply with exactly: READY"
MAX_OUTPUT_TOKENS = 16


@dataclass(frozen=True, slots=True)
class GenerationDiagnostic:
    model: str
    succeeded: bool
    elapsed_seconds: float
    diagnostic: tuple[str, int | None, str | None] | None
    cleanup_succeeded: bool | None
    cleanup_diagnostic: tuple[str, int | None, str | None] | None


def _create_client(api_key: str) -> Any:
    from google import genai

    return genai.Client(api_key=api_key)


async def run_diagnostic(
    *,
    settings_loader: Callable[[], Settings] = get_settings,
    client_factory: Callable[[str], Any] = _create_client,
) -> GenerationDiagnostic:
    """Perform one generation call and report only safe diagnostic metadata."""

    try:
        settings = settings_loader()
    except Exception as exc:
        return GenerationDiagnostic(
            "unavailable",
            False,
            0.0,
            _classify_provider_exception(exc, ""),
            None,
            None,
        )

    configured_model = settings.gemini_model
    model = _safe_model_name(configured_model, settings.gemini_api_key)
    if not settings.gemini_api_key.strip():
        return GenerationDiagnostic(
            model,
            False,
            0.0,
            ("unknown_provider_error", None, None),
            None,
            None,
        )

    try:
        client = client_factory(settings.gemini_api_key)
    except Exception as exc:
        return GenerationDiagnostic(
            model,
            False,
            0.0,
            _classify_provider_exception(exc, settings.gemini_api_key),
            None,
            None,
        )

    succeeded = False
    diagnostic: tuple[str, int | None, str | None] | None = None
    cleanup_succeeded = True
    cleanup_diagnostic: tuple[str, int | None, str | None] | None = None
    started = time.perf_counter()
    try:
        try:
            # One direct SDK call, bounded by the configured application timeout.
            await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=configured_model,
                    contents=PROMPT,
                    config={"max_output_tokens": MAX_OUTPUT_TOKENS},
                ),
                timeout=settings.gemini_request_timeout_seconds,
            )
            succeeded = True
        except Exception as exc:
            diagnostic = _classify_provider_exception(
                exc, settings.gemini_api_key
            )
    finally:
        elapsed_seconds = time.perf_counter() - started
        try:
            await client.aio.aclose()
        except Exception as exc:
            cleanup_succeeded = False
            cleanup_diagnostic = _classify_provider_exception(
                exc, settings.gemini_api_key
            )

    return GenerationDiagnostic(
        model=model,
        succeeded=succeeded,
        elapsed_seconds=elapsed_seconds,
        diagnostic=diagnostic,
        cleanup_succeeded=cleanup_succeeded,
        cleanup_diagnostic=cleanup_diagnostic,
    )


def _safe_model_name(model: str, api_key: str) -> str:
    """Keep the explicitly reported model free of credentials and controls."""

    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9._:/-]{1,200}", model):
        return "unavailable"
    if api_key and api_key.casefold() in model.casefold():
        return "[redacted]"
    return model


def _format_diagnostic(
    diagnostic: tuple[str, int | None, str | None] | None,
) -> list[str]:
    if diagnostic is None:
        return []
    category, status, reason = diagnostic
    lines: list[str] = []
    if category in SAFE_PROVIDER_DIAGNOSTIC_CATEGORIES:
        lines.append(f"Diagnostic category: {category}")
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        lines.append(f"HTTP status: {status}")
    if reason in SAFE_PROVIDER_REASON_CODES:
        lines.append(f"Provider reason: {reason}")
    return lines


def format_result(result: GenerationDiagnostic) -> str:
    """Format only the configured model and allow-listed diagnostic fields."""

    lines = [
        f"Configured model: {result.model}",
        "Generation request: " + ("SUCCESS" if result.succeeded else "FAILURE"),
        f"Elapsed time: {result.elapsed_seconds:.3f} seconds",
    ]
    lines.extend(_format_diagnostic(result.diagnostic))
    if result.cleanup_succeeded is None:
        lines.append("Client cleanup: NOT NEEDED")
    else:
        lines.append(
            "Client cleanup: "
            + ("SUCCESS" if result.cleanup_succeeded else "FAILURE")
        )
        lines.extend(_format_diagnostic(result.cleanup_diagnostic))
    return "\n".join(lines)


async def _run() -> int:
    result = await run_diagnostic()
    print(format_result(result))
    return 0 if result.succeeded and result.cleanup_succeeded else 1


def main() -> None:
    try:
        exit_code = asyncio.run(_run())
    except KeyboardInterrupt:
        print("Gemini generation connectivity diagnostic interrupted.")
        exit_code = 130
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
