"""Check authenticated Gemini SDK metadata access without generating content."""

from __future__ import annotations

import asyncio
import importlib.metadata
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


MODEL_NAME = "gemini-3.7-flash"


@dataclass(frozen=True, slots=True)
class DiagnosticResult:
    sdk_version: str
    request_succeeded: bool
    elapsed_seconds: float
    request_diagnostic: tuple[str, int | None, str | None] | None
    cleanup_succeeded: bool | None
    cleanup_diagnostic: tuple[str, int | None, str | None] | None


def _sdk_version() -> str:
    try:
        return importlib.metadata.version("google-genai")
    except Exception:
        return "unavailable"


def _create_client(api_key: str) -> Any:
    from google import genai

    return genai.Client(api_key=api_key)


async def run_diagnostic(
    *,
    settings_loader: Callable[[], Settings] = get_settings,
    client_factory: Callable[[str], Any] = _create_client,
    sdk_version_reader: Callable[[], str] = _sdk_version,
) -> DiagnosticResult:
    """Run one model metadata GET and close the SDK client without exposing details."""

    sdk_version = sdk_version_reader()
    try:
        settings = settings_loader()
    except Exception as exc:
        diagnostic = _classify_provider_exception(exc, "")
        return DiagnosticResult(
            sdk_version, False, 0.0, diagnostic, None, None
        )

    if not settings.gemini_api_key.strip():
        return DiagnosticResult(
            sdk_version,
            False,
            0.0,
            ("unknown_provider_error", None, None),
            None,
            None,
        )

    try:
        client = client_factory(settings.gemini_api_key)
    except Exception as exc:
        diagnostic = _classify_provider_exception(exc, settings.gemini_api_key)
        return DiagnosticResult(
            sdk_version, False, 0.0, diagnostic, None, None
        )

    started = time.perf_counter()
    request_succeeded = False
    request_diagnostic: tuple[str, int | None, str | None] | None = None
    cleanup_succeeded = True
    cleanup_diagnostic: tuple[str, int | None, str | None] | None = None
    try:
        try:
            # Metadata GET only. Deliberately do not call generate_content().
            await client.aio.models.get(model=MODEL_NAME)
            request_succeeded = True
        except Exception as exc:
            request_diagnostic = _classify_provider_exception(
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

    return DiagnosticResult(
        sdk_version=sdk_version,
        request_succeeded=request_succeeded,
        elapsed_seconds=elapsed_seconds,
        request_diagnostic=request_diagnostic,
        cleanup_succeeded=cleanup_succeeded,
        cleanup_diagnostic=cleanup_diagnostic,
    )


def _format_diagnostic(
    diagnostic: tuple[str, int | None, str | None] | None,
) -> list[str]:
    if diagnostic is None:
        return []
    category, status, reason = diagnostic
    lines: list[str] = []
    if category in SAFE_PROVIDER_DIAGNOSTIC_CATEGORIES:
        lines.append(f"  Diagnostic category: {category}")
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        lines.append(f"  HTTP status: {status}")
    if reason in SAFE_PROVIDER_REASON_CODES:
        lines.append(f"  Provider reason: {reason}")
    return lines


def format_result(result: DiagnosticResult) -> str:
    """Render only allow-listed diagnostics and non-sensitive outcome metadata."""

    lines = [
        f"Google Gen AI SDK version: {result.sdk_version}",
        "Model metadata request: "
        + ("SUCCESS" if result.request_succeeded else "FAILURE"),
        f"Elapsed time: {result.elapsed_seconds:.3f} seconds",
    ]
    lines.extend(_format_diagnostic(result.request_diagnostic))
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
    return 0 if result.request_succeeded and result.cleanup_succeeded else 1


def main() -> None:
    try:
        exit_code = asyncio.run(_run())
    except KeyboardInterrupt:
        # Keep interruption output free of SDK exception details.
        print("Gemini SDK connectivity diagnostic interrupted.")
        exit_code = 130
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
