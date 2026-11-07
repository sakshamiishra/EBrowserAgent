"""Provider-neutral types and protocol for action-plan generation."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    StringConstraints,
    field_serializer,
    field_validator,
    model_validator,
)

from app.browser.snapshot import InteractiveElementSnapshot, PageSnapshot


SAFE_PROVIDER_DIAGNOSTIC_CATEGORIES = frozenset(
    {
        "authentication_authorization",
        "rate_limit_quota",
        "invalid_request_or_model",
        "server_error",
        "network_connectivity",
        "timeout",
        "unknown_provider_error",
    }
)
SAFE_PROVIDER_REASON_CODES = frozenset(
    {
        "UNAUTHENTICATED",
        "PERMISSION_DENIED",
        "RESOURCE_EXHAUSTED",
        "INVALID_ARGUMENT",
        "NOT_FOUND",
        "INTERNAL",
        "UNAVAILABLE",
        "DEADLINE_EXCEEDED",
        "FAILED_PRECONDITION",
        "SAFETY",
        "OTHER",
    }
)
_DIAGNOSTIC_CREDENTIAL_RE = re.compile(
    r"(?i)(?:sk-ant-[A-Za-z0-9_-]{8,}|sk-[A-Za-z0-9_-]{20,}|"
    r"AIza[A-Za-z0-9_-]{20,}|AQ[A-Za-z0-9_-]{30,}|Bearer\s+\S+)"
)
_DIAGNOSTIC_HEADER_RE = re.compile(
    r"(?i)([\"']?\b(?:authorization|proxy-authorization|x-api-key|api-key|"
    r"anthropic-api-key|x-goog-api-key)[\"']?\s*[:=]\s*)"
    r"(?:bearer\s+)?(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)"
)


class ProviderInteractiveElementSnapshot(InteractiveElementSnapshot):
    """Deeply immutable element snapshot used across the provider boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderPageSnapshot(PageSnapshot):
    """Immutable provider snapshot, detached from the internal browser snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    elements: tuple[ProviderInteractiveElementSnapshot, ...] = Field(
        default_factory=tuple, max_length=100
    )


class InternalPlanningInput(BaseModel):
    """Bounded local input; test-data values must stay inside PlannerService."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    instruction: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4_000)]
    snapshot: PageSnapshot
    test_data: Mapping[str, str] = Field(default_factory=dict)

    @field_validator("snapshot", mode="before")
    @classmethod
    def detach_snapshot(cls, value: PageSnapshot | dict[str, object]) -> PageSnapshot:
        if isinstance(value, PageSnapshot):
            return PageSnapshot.model_validate(value.model_dump(mode="python"))
        return value

    @model_validator(mode="after")
    def bound_test_data(self) -> InternalPlanningInput:
        if len(self.test_data) > 20:
            raise ValueError("test_data may contain at most 20 entries")
        total = 0
        for key, value in self.test_data.items():
            if not key.strip() or len(key) > 100:
                raise ValueError("test_data keys must contain 1 to 100 characters")
            if len(value) > 1_000:
                raise ValueError("test_data values may not exceed 1,000 characters")
            total += len(key) + len(value)
        if total > 10_000:
            raise ValueError("test_data may not exceed 10,000 combined characters")
        object.__setattr__(self, "test_data", MappingProxyType(dict(self.test_data)))
        return self

    @field_serializer("test_data")
    def serialize_test_data(self, value: Mapping[str, str]) -> dict[str, str]:
        return dict(value)


class ProviderPlanningRequest(BaseModel):
    """Sanitized provider input with no field for original test-data values."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    instruction: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4_000)]
    snapshot: ProviderPageSnapshot
    available_test_data_keys: tuple[Annotated[str, StringConstraints(min_length=1, max_length=100)], ...] = Field(max_length=20)
    _sanitization_digest: str | None = PrivateAttr(default=None)

    @classmethod
    def _from_sanitizer(
        cls,
        *,
        instruction: str,
        snapshot: ProviderPageSnapshot,
        available_test_data_keys: tuple[str, ...],
    ) -> ProviderPlanningRequest:
        """Construct the request through the local sanitizer path."""

        request = cls(
            instruction=instruction,
            snapshot=snapshot,
            available_test_data_keys=available_test_data_keys,
        )
        request._sanitization_digest = request._current_content_digest()
        return request

    @property
    def is_sanitization_verified(self) -> bool:
        expected_digest = self._sanitization_digest
        if expected_digest is None:
            return False
        try:
            actual_digest = self._current_content_digest()
        except Exception:
            return False
        return hmac.compare_digest(expected_digest, actual_digest)

    def _current_content_digest(self) -> str:
        """Hash a stable serialization of every field sent to the provider."""

        payload = {
            "instruction": self.instruction,
            "snapshot": self.snapshot.model_dump(mode="json"),
            "available_test_data_keys": sorted(self.available_test_data_keys),
        }
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class PlannerProviderResponse(BaseModel):
    """Raw JSON returned by a provider; it is not trusted until parsed locally."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    content: Annotated[str, StringConstraints(min_length=1, max_length=32_000)]


class PlannerProviderError(RuntimeError):
    """Raised for provider configuration, API, timeout, or response failures."""

    def __init__(
        self,
        message: str,
        *,
        diagnostic_category: str = "unknown_provider_error",
        status_code: int | None = None,
        reason_code: str | None = None,
        provider_error_type: str | None = None,
        provider_error_code: str | None = None,
        provider_message: str | None = None,
        request_id: str | None = None,
        model_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.diagnostic_category = (
            diagnostic_category
            if isinstance(diagnostic_category, str)
            and diagnostic_category in SAFE_PROVIDER_DIAGNOSTIC_CATEGORIES
            else "unknown_provider_error"
        )
        self.status_code = (
            status_code
            if isinstance(status_code, int)
            and not isinstance(status_code, bool)
            and 100 <= status_code <= 599
            else None
        )
        self.reason_code = (
            reason_code
            if isinstance(reason_code, str) and reason_code in SAFE_PROVIDER_REASON_CODES
            else None
        )
        self.provider_error_type = _safe_diagnostic_token(
            provider_error_type, r"[a-z][a-z0-9_]{0,63}"
        )
        self.provider_error_code = _safe_diagnostic_token(
            provider_error_code, r"[A-Za-z0-9_.-]{1,64}"
        )
        self.provider_message = _safe_diagnostic_message(provider_message)
        self.request_id = _safe_diagnostic_token(request_id, r"req_[A-Za-z0-9_-]{1,96}")
        self.model_id = _safe_diagnostic_token(model_id, r"claude-[A-Za-z0-9._:-]{1,90}")


def _safe_diagnostic_token(value: object, pattern: str) -> str | None:
    if not isinstance(value, str) or len(value) > 100:
        return None
    return (
        value
        if re.fullmatch(pattern, value) and not _DIAGNOSTIC_CREDENTIAL_RE.search(value)
        else None
    )


def _safe_diagnostic_message(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    safe = " ".join("".join(char if char.isprintable() else " " for char in value).split())
    safe = _DIAGNOSTIC_HEADER_RE.sub(r"\1[credential redacted]", safe)
    safe = _DIAGNOSTIC_CREDENTIAL_RE.sub("[credential redacted]", safe)
    return safe[:240] or None


class PlannerProvider(Protocol):
    """Async provider interface; application code should invoke it via PlannerService.

    Providers should reject requests whose content-bound sanitizer digest is not
    valid. The digest detects accidental modification, not deliberate in-process
    manipulation.
    """

    async def generate_plan(self, request: ProviderPlanningRequest) -> PlannerProviderResponse:
        """Return provider output for local canonical-schema validation."""
