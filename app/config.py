"""Environment-backed application settings without an extra dependency."""

from __future__ import annotations

import os
from dataclasses import dataclass
import math
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
MIN_CLEANUP_GRACE_SECONDS = 0.1
MAX_CLEANUP_GRACE_SECONDS = 60.0
DEFAULT_CLEANUP_GRACE_SECONDS = 5.0
DEFAULT_AI_PROVIDER = "gemini"
SUPPORTED_AI_PROVIDERS = frozenset({"gemini", "anthropic"})


def validate_cleanup_grace_seconds(value: object) -> float:
    """Validate and normalize the shared cleanup grace setting."""

    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not MIN_CLEANUP_GRACE_SECONDS <= value <= MAX_CLEANUP_GRACE_SECONDS
        or not math.isfinite(value)
    ):
        raise ValueError("cleanup grace period must be between 0.1 and 60 seconds")
    return float(value)


def _load_dotenv_file(path: Path) -> None:
    """Load simple KEY=VALUE entries; existing process environment wins."""

    if not path.is_file():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            os.environ.setdefault(key, value)


@dataclass(frozen=True, slots=True)
class Settings:
    app_name: str = "EBrowserAgent"
    environment: str = "development"
    ai_provider: str = DEFAULT_AI_PROVIDER
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.8-flash"
    gemini_request_timeout_seconds: float = 45.0
    gemini_max_output_tokens: int = 4_096
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-5-5"
    anthropic_request_timeout_seconds: float = 30.0
    run_timeout_seconds: float = 300.0
    cleanup_grace_seconds: float = DEFAULT_CLEANUP_GRACE_SECONDS

    def __post_init__(self) -> None:
        if not isinstance(self.ai_provider, str):
            raise ValueError("AI_PROVIDER must be gemini or anthropic")
        provider = self.ai_provider.strip().lower()
        if provider not in SUPPORTED_AI_PROVIDERS:
            raise ValueError("AI_PROVIDER must be gemini or anthropic")
        object.__setattr__(self, "ai_provider", provider)
        if not isinstance(self.anthropic_model, str) or not self.anthropic_model.strip():
            raise ValueError("ANTHROPIC_MODEL must not be empty")
        if (
            isinstance(self.run_timeout_seconds, bool)
            or not isinstance(self.run_timeout_seconds, (int, float))
            or not math.isfinite(self.run_timeout_seconds)
            or not 1 <= self.run_timeout_seconds <= 3_600
        ):
            raise ValueError("run timeout must be between 1 and 3600 seconds")
        if (
            isinstance(self.anthropic_request_timeout_seconds, bool)
            or not isinstance(self.anthropic_request_timeout_seconds, (int, float))
            or not math.isfinite(self.anthropic_request_timeout_seconds)
            or not 1 <= self.anthropic_request_timeout_seconds <= 600
        ):
            raise ValueError("Anthropic request timeout must be between 1 and 600 seconds")
        validate_cleanup_grace_seconds(self.cleanup_grace_seconds)


def get_settings() -> Settings:
    """Return settings, loading a local .env file when present."""

    _load_dotenv_file(PROJECT_ROOT / ".env")
    return Settings(
        app_name=os.getenv("EBROWSER_APP_NAME", "EBrowserAgent"),
        environment=os.getenv("EBROWSER_ENV", "development"),
        ai_provider=os.getenv("AI_PROVIDER", DEFAULT_AI_PROVIDER),
        gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash"),
        gemini_request_timeout_seconds=float(
            os.getenv("GEMINI_REQUEST_TIMEOUT_SECONDS", "45")
        ),
        gemini_max_output_tokens=int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "4096")),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-haiku-5-5"),
        anthropic_request_timeout_seconds=float(
            os.getenv("ANTHROPIC_REQUEST_TIMEOUT_SECONDS", "30")
        ),
        run_timeout_seconds=float(os.getenv("EBROWSER_RUN_TIMEOUT_SECONDS", "300")),
        cleanup_grace_seconds=float(os.getenv("EBROWSER_CLEANUP_GRACE_SECONDS", "5")),
    )
