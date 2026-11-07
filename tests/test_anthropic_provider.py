"""Offline tests for the optional Anthropic planner adapter and selection."""

from __future__ import annotations

import json
import os
import re
import sys
import types
import unittest
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.api.schemas import StructuredActionPlan
from app.browser.snapshot import PageSnapshot
from app.config import Settings, get_settings
from app.planner.anthropic import (
    ANTHROPIC_MAX_OUTPUT_TOKENS,
    ANTHROPIC_PLAN_SCHEMA,
    SAFE_SUPPRESSED_PROVIDER_MESSAGE,
    AnthropicProvider,
)
from app.planner.factory import create_planner_provider
from app.planner.gemini import GeminiProvider
from app.planner.prompts import SYSTEM_INSTRUCTIONS, build_planning_input, build_provider_request
from app.planner.provider import (
    InternalPlanningInput,
    PlannerProviderError,
    ProviderPlanningRequest,
)
from app.planner.service import (
    PlannerService,
    PlanningProviderError,
    PlanningValidationError,
)
from scripts.anthropic_planner_smoke_test import _safe_failure
from scripts.anthropic_candidate_schema_diagnostic import CANDIDATE_PLAN_SCHEMA


def internal_request(
    *, test_data: dict[str, str] | None = None,
    snapshot: PageSnapshot | None = None,
) -> InternalPlanningInput:
    return InternalPlanningInput(
        instruction="Click Login and verify the login form.",
        snapshot=snapshot or PageSnapshot(
            url="https://example.test/",
            title="Example sign-in",
            visible_text="Welcome. Login",
            elements=[{
                "kind": "button", "role": "button",
                "accessible_name": "Login", "text": "Login",
            }],
        ),
        test_data=test_data or {},
    )


def valid_plan_json() -> str:
    return json.dumps({
        "version": 1,
        "goal": "Open Login and verify the form",
        "actions": [
            {
                "type": "click",
                "target": {"kind": "role", "value": "Login", "role": "button"},
            },
            {"type": "assert_text", "expected_text": "Login form", "target": None, "required": True},
        ],
    })


def fake_response(text: str | None, *, stop_reason: str = "end_turn") -> SimpleNamespace:
    content = None if text is None else [SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(stop_reason=stop_reason, content=content)


def fake_client(response: object | None = None, error: Exception | None = None) -> SimpleNamespace:
    messages = SimpleNamespace(create=AsyncMock(return_value=response))
    if error is not None:
        messages.create.side_effect = error
    return SimpleNamespace(messages=messages, aclose=AsyncMock())


def _matches_provider_schema(
    schema: dict[str, object],
    value: object,
    root: dict[str, object] | None = None,
) -> bool:
    """Evaluate the JSON Schema subset used here for focused offline assertions."""

    root = schema if root is None else root
    reference = schema.get("$ref")
    if isinstance(reference, str):
        prefix = "#/definitions/"
        if not reference.startswith(prefix):
            return False
        definitions = root.get("definitions")
        target_schema = definitions.get(reference[len(prefix):]) if isinstance(definitions, dict) else None
        return (
            isinstance(target_schema, dict)
            and _matches_provider_schema(target_schema, value, root)
        )

    variants = schema.get("anyOf")
    if isinstance(variants, list):
        return any(
            isinstance(variant, dict) and _matches_provider_schema(variant, value, root)
            for variant in variants
        )

    expected_type = schema.get("type")
    type_matches = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
        "null": lambda item: item is None,
    }
    if expected_type not in type_matches or not type_matches[expected_type](value):
        return False

    allowed = schema.get("enum")
    if isinstance(allowed, list) and value not in allowed:
        return False

    pattern = schema.get("pattern")
    if isinstance(pattern, str) and isinstance(value, str) and re.search(pattern, value) is None:
        return False

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, dict) or not isinstance(required, list):
            return False
        if any(key not in value for key in required):
            return False
        if schema.get("additionalProperties") is False and any(
            key not in properties for key in value
        ):
            return False
        for key, item in value.items():
            child_schema = properties.get(key)
            if (
                not isinstance(child_schema, dict)
                or not _matches_provider_schema(child_schema, item, root)
            ):
                return False

    if isinstance(value, list):
        minimum = schema.get("minItems", 0)
        item_schema = schema.get("items")
        if isinstance(minimum, int) and len(value) < minimum:
            return False
        if isinstance(item_schema, dict) and any(
            not _matches_provider_schema(item_schema, item, root) for item in value
        ):
            return False
    return True


def _plan_with_action(action: dict[str, object]) -> dict[str, object]:
    return {
        "version": 1,
        "goal": "Fill the synthetic field",
        "actions": [action],
    }


class AnthropicProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_structured_response_is_validated_by_planner_service(self) -> None:
        client = fake_client(fake_response(valid_plan_json()))
        settings = Settings(
            ai_provider="anthropic", anthropic_api_key="synthetic-api-key",
        )
        provider = AnthropicProvider(settings=settings, client=client)
        self.assertTrue(callable(provider.generate_plan))
        self.assertTrue(inspect.iscoroutinefunction(provider.generate_plan))
        plan = await PlannerService(provider).plan(internal_request())

        self.assertIsInstance(plan, StructuredActionPlan)
        self.assertEqual([action.type for action in plan.actions], ["click", "assert_text"])
        client.messages.create.assert_awaited_once()
        call = client.messages.create.await_args.kwargs
        self.assertEqual(call["model"], "claude-haiku-5-5")
        self.assertEqual(call["max_tokens"], ANTHROPIC_MAX_OUTPUT_TOKENS)
        self.assertEqual(call["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(call["output_config"]["format"]["schema"], ANTHROPIC_PLAN_SCHEMA)
        self.assertIn("Never generate Python, JavaScript, shell commands", call["system"])
        self.assertIn("untrusted_website_snapshot", call["messages"][0]["content"])
        self.assertEqual(call["messages"][0]["role"], "user")

    def test_provider_schema_accepts_only_one_fill_value_source(self) -> None:
        target = {"kind": "label", "value": "Email"}
        valid_literal = _plan_with_action({
            "type": "fill", "target": target,
            "value": "person@example.test", "value_ref": None,
        })
        valid_reference = _plan_with_action({
            "type": "fill", "target": target,
            "value": None, "value_ref": "email",
        })
        self.assertTrue(_matches_provider_schema(ANTHROPIC_PLAN_SCHEMA, valid_literal))
        self.assertTrue(_matches_provider_schema(ANTHROPIC_PLAN_SCHEMA, valid_reference))

        invalid_actions = (
            {"type": "fill", "target": target, "value": None, "value_ref": None},
            {"type": "fill", "target": target, "value": "literal", "value_ref": "email"},
        )
        for action in invalid_actions:
            with self.subTest(action=action):
                self.assertFalse(
                    _matches_provider_schema(ANTHROPIC_PLAN_SCHEMA, _plan_with_action(action))
                )

    def test_provider_schema_requires_role_only_for_role_targets(self) -> None:
        role_target_action = {
            "type": "click",
            "target": {"kind": "role", "value": "Login", "role": "button"},
        }
        self.assertTrue(
            _matches_provider_schema(
                ANTHROPIC_PLAN_SCHEMA, _plan_with_action(role_target_action)
            )
        )
        self.assertTrue(
            _matches_provider_schema(
                ANTHROPIC_PLAN_SCHEMA,
                _plan_with_action({
                    "type": "click",
                    "target": {"kind": "label", "value": "Email"},
                }),
            )
        )

        # These structurally valid candidate shapes are rejected later by the
        # canonical Pydantic validator when role/kind semantics disagree.
        for target in (
            {"kind": "role", "value": "Login"},
            {"kind": "label", "value": "Email", "role": "textbox"},
        ):
            with self.subTest(target=target):
                self.assertTrue(
                    _matches_provider_schema(
                        ANTHROPIC_PLAN_SCHEMA,
                        _plan_with_action({"type": "click", "target": target}),
                    )
                )

    def test_candidate_schema_covers_all_actions_refs_and_strict_objects(self) -> None:
        self.assertIs(CANDIDATE_PLAN_SCHEMA, ANTHROPIC_PLAN_SCHEMA)
        variants = ANTHROPIC_PLAN_SCHEMA["properties"]["actions"]["items"]["anyOf"]
        action_types = [variant["properties"]["type"]["enum"][0] for variant in variants]
        self.assertEqual(
            set(action_types),
            {
                "navigate", "click", "fill", "select", "wait",
                "assert_visible", "assert_text", "assert_url",
            },
        )
        self.assertEqual(action_types.count("fill"), 2)
        required_fields = {
            "navigate": {"type", "url"},
            "click": {"type", "target"},
            "fill": {"type", "target", "value", "value_ref"},
            "select": {"type", "target", "option"},
            "wait": {"type", "milliseconds"},
            "assert_visible": {"type", "target", "required"},
            "assert_text": {"type", "expected_text", "target", "required"},
            "assert_url": {"type", "expected_url", "exact", "required"},
        }
        for variant, action_type in zip(variants, action_types, strict=True):
            self.assertEqual(set(variant["required"]), required_fields[action_type])
        target_schema = ANTHROPIC_PLAN_SCHEMA["definitions"]["target"]
        self.assertEqual(target_schema["additionalProperties"], False)
        self.assertEqual(target_schema["required"], ["kind", "value"])
        for variant in variants:
            self.assertFalse(variant["additionalProperties"])
            for target in (
                variant["properties"].get("target"),
            ):
                if isinstance(target, dict) and "$ref" in target:
                    self.assertEqual(target["$ref"], "#/definitions/target")
                elif isinstance(target, dict) and "anyOf" in target:
                    self.assertEqual(target["anyOf"][0]["$ref"], "#/definitions/target")
        self.assertFalse(ANTHROPIC_PLAN_SCHEMA["additionalProperties"])

        role_target = {"kind": "role", "value": "Login", "role": "button"}
        non_role_target = {"kind": "label", "value": "Email"}
        valid_actions = (
            {"type": "navigate", "url": "https://example.test/"},
            {"type": "click", "target": role_target},
            {"type": "fill", "target": non_role_target, "value": "x", "value_ref": None},
            {"type": "fill", "target": non_role_target, "value": None, "value_ref": "email"},
            {"type": "select", "target": non_role_target, "option": "monthly"},
            {"type": "wait", "milliseconds": 500},
            {"type": "assert_visible", "target": role_target, "required": True},
            {"type": "assert_text", "expected_text": "Ready", "target": None, "required": True},
            {"type": "assert_url", "expected_url": "https://example.test/", "exact": True, "required": True},
        )
        for action in valid_actions:
            with self.subTest(action_type=action["type"]):
                self.assertTrue(
                    _matches_provider_schema(ANTHROPIC_PLAN_SCHEMA, _plan_with_action(action))
                )

        valid = _plan_with_action({"type": "click", "target": role_target})
        for invalid in (
            {**valid, "unexpected": "extra"},
            _plan_with_action({
                "type": "click",
                "target": {"kind": "role", "value": "Login", "role": "button", "extra": 1},
            }),
            _plan_with_action({
                "type": "click", "target": {"kind": "label", "value": "Email"},
                "extra": "unexpected",
            }),
        ):
            self.assertFalse(_matches_provider_schema(ANTHROPIC_PLAN_SCHEMA, invalid))

    async def test_canonical_validation_rejects_candidate_permitted_invalid_plans(self) -> None:
        invalid_actions = (
            {"type": "click", "target": {"kind": "role", "value": "Login"}},
            {"type": "click", "target": {"kind": "label", "value": "Email", "role": "textbox"}},
            {"type": "fill", "target": {"kind": "label", "value": "Email"},
             "value": None, "value_ref": None},
            {"type": "fill", "target": {"kind": "label", "value": "Email"},
             "value": "literal", "value_ref": "email"},
            {"type": "fill", "target": {"kind": "label", "value": "Email"},
             "value": None, "value_ref": ""},
            {"type": "fill", "target": {"kind": "label", "value": "Email"},
             "value": None, "value_ref": "   "},
            {"type": "wait", "milliseconds": 30_001},
            {"type": "navigate", "url": "not-a-url"},
        )
        for action in invalid_actions:
            client = fake_client(fake_response(json.dumps({
                "version": 1, "goal": "Run synthetic plan", "actions": [action],
            })))
            provider = AnthropicProvider(
                settings=Settings(anthropic_api_key="synthetic-api-key"),
                client=client,
            )
            with self.subTest(action=action), self.assertRaises(PlanningValidationError):
                await PlannerService(provider).plan(internal_request())

    async def test_both_valid_fill_sources_pass_canonical_service_validation(self) -> None:
        cases = (
            ({"type": "fill", "target": {"kind": "label", "value": "Email", "role": None},
              "value": "person@example.test", "value_ref": None}, {}),
            ({"type": "fill", "target": {"kind": "label", "value": "Email", "role": None},
              "value": None, "value_ref": "email"}, {"email": "synthetic@example.test"}),
        )
        for action, test_data in cases:
            client = fake_client(fake_response(json.dumps({
                "version": 1, "goal": "Fill the email field", "actions": [action],
            })))
            provider = AnthropicProvider(
                settings=Settings(anthropic_api_key="synthetic-api-key"),
                client=client,
            )
            with self.subTest(action=action):
                plan = await PlannerService(provider).plan(
                    internal_request(
                        test_data=test_data,
                        snapshot=PageSnapshot(
                            url="https://example.test/", title="Email form", visible_text="Email",
                            elements=[{
                                "kind": "input", "role": "textbox", "label": "Email",
                                "accessible_name": "Email",
                            }],
                        ),
                    )
                )
                self.assertEqual(plan.actions[0].type, "fill")

    async def test_actual_test_data_values_never_reach_anthropic(self) -> None:
        secret = "synthetic-no-send-password"
        client = fake_client(fake_response(valid_plan_json()))
        provider = AnthropicProvider(
            settings=Settings(
                ai_provider="anthropic", anthropic_api_key="synthetic-api-key"
            ),
            client=client,
        )
        await provider.generate_plan(build_provider_request(
            internal_request(test_data={"password": secret})
        ))
        serialized_call = repr(client.messages.create.await_args.kwargs)
        self.assertNotIn(secret, serialized_call)
        self.assertIn("password", serialized_call)

    async def test_request_integrity_is_required_before_sdk_call(self) -> None:
        safe = build_provider_request(internal_request())
        unverified = ProviderPlanningRequest(
            instruction=safe.instruction,
            snapshot=safe.snapshot,
            available_test_data_keys=safe.available_test_data_keys,
        )
        modified = safe.model_copy(update={"instruction": "modified"})
        for request in (unverified, modified):
            client = fake_client(fake_response(valid_plan_json()))
            provider = AnthropicProvider(
                settings=Settings(anthropic_api_key="synthetic-key"), client=client
            )
            with self.subTest(verified=request.is_sanitization_verified):
                with self.assertRaises(PlannerProviderError):
                    await provider.generate_plan(request)
            client.messages.create.assert_not_awaited()

    async def test_missing_or_unsupported_content_is_rejected_safely(self) -> None:
        for response in (
            fake_response(None),
            SimpleNamespace(stop_reason="end_turn", content=None),
            SimpleNamespace(
                stop_reason="end_turn",
                content=[SimpleNamespace(type="tool_use", input={"secret": "not shown"})],
            ),
        ):
            provider = AnthropicProvider(
                settings=Settings(anthropic_api_key="synthetic-key"),
                client=fake_client(response),
            )
            with self.subTest(response=response), self.assertRaises(PlannerProviderError):
                await provider.generate_plan(build_provider_request(internal_request()))

    async def test_truncated_and_refused_finish_reasons_are_rejected(self) -> None:
        for stop_reason in ("max_tokens", "refusal", "model_context_window_exceeded", None):
            provider = AnthropicProvider(
                settings=Settings(anthropic_api_key="synthetic-key"),
                client=fake_client(fake_response(valid_plan_json(), stop_reason=stop_reason)),
            )
            with self.subTest(stop_reason=stop_reason), self.assertRaises(PlannerProviderError):
                await provider.generate_plan(build_provider_request(internal_request()))

    async def test_malformed_json_fails_canonical_service_validation(self) -> None:
        provider = AnthropicProvider(
            settings=Settings(anthropic_api_key="synthetic-key"),
            client=fake_client(fake_response('{"version":')),
        )
        with self.assertRaises(PlanningValidationError):
            await PlannerService(provider).plan(internal_request())

    async def test_http_errors_use_only_shared_safe_diagnostics(self) -> None:
        expected = {
            400: ("invalid_request_or_model", "INVALID_ARGUMENT"),
            401: ("authentication_authorization", "UNAUTHENTICATED"),
            403: ("authentication_authorization", "PERMISSION_DENIED"),
            404: ("invalid_request_or_model", "NOT_FOUND"),
            408: ("timeout", "DEADLINE_EXCEEDED"),
            429: ("rate_limit_quota", "RESOURCE_EXHAUSTED"),
            503: ("server_error", "UNAVAILABLE"),
        }
        for status, (category, reason) in expected.items():
            error = RuntimeError(f"raw provider text and synthetic-secret for {status}")
            error.status_code = status
            client = fake_client(error=error)
            provider = AnthropicProvider(
                settings=Settings(anthropic_api_key="synthetic-secret"), client=client
            )
            with self.subTest(status=status), self.assertRaises(PlannerProviderError) as raised:
                await provider.generate_plan(build_provider_request(internal_request()))
            self.assertEqual(raised.exception.diagnostic_category, category)
            self.assertEqual(raised.exception.status_code, status)
            self.assertEqual(raised.exception.reason_code, reason)
            self.assertNotIn("raw provider text", str(raised.exception))
            self.assertNotIn("synthetic-secret", str(raised.exception))
            self.assertEqual(client.messages.create.await_count, 1)

    async def test_http_400_diagnostics_are_structured_redacted_and_bounded(self) -> None:
        api_key = "sk-ant-api03-syntheticCredential0123456789"
        echoed_key = "sk-ant-api03-anotherSyntheticCredential987654321"
        aq_key = "AQABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"
        request = internal_request()
        provider_request = build_provider_request(request)
        planning_input = build_planning_input(provider_request)
        raw_message = (
            "Invalid structured schema. System instructions: "
            f"{SYSTEM_INSTRUCTIONS}; serialized planner input: {planning_input}; "
            f"Authorization: Bearer sensitive-header-token-123456; "
            f'"x-api-key": "{echoed_key}"; key={aq_key}; configured={api_key}'
        )
        error = RuntimeError("RAW EXCEPTION MUST NOT ESCAPE " + api_key)
        error.status_code = 400
        error.body = {
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "code": "invalid_schema",
                "message": raw_message,
            },
            "request_id": "req_test-400_123",
        }
        client = fake_client(error=error)
        provider = AnthropicProvider(
            settings=Settings(
                ai_provider="anthropic", anthropic_api_key=api_key,
                anthropic_model="claude-haiku-5-5",
            ),
            client=client,
        )

        with self.assertRaises(PlanningProviderError) as raised:
            await PlannerService(provider).plan(request)

        diagnostic = raised.exception
        self.assertEqual(diagnostic.status_code, 400)
        self.assertEqual(diagnostic.diagnostic_category, "invalid_request_or_model")
        self.assertEqual(diagnostic.reason_code, "INVALID_ARGUMENT")
        self.assertEqual(diagnostic.provider_error_type, "invalid_request_error")
        self.assertEqual(diagnostic.provider_error_code, "unknown")
        self.assertEqual(diagnostic.request_id, "req_test-400_123")
        self.assertEqual(diagnostic.model_id, "claude-haiku-5-5")
        self.assertEqual(diagnostic.provider_message, SAFE_SUPPRESSED_PROVIDER_MESSAGE)
        report = _safe_failure(diagnostic)
        self.assertIn("invalid_request_error/unknown", report)
        self.assertIn("req_test-400_123", report)
        self.assertIn("claude-haiku-5-5", report)
        for protected in (
            api_key, echoed_key, "sensitive-header-token-123456",
            aq_key, request.instruction, SYSTEM_INSTRUCTIONS, planning_input,
            "RAW EXCEPTION MUST NOT ESCAPE",
        ):
            self.assertNotIn(protected, report)
        self.assertIn(SAFE_SUPPRESSED_PROVIDER_MESSAGE, report)

    async def test_partial_prompt_excerpts_are_suppressed_with_safe_metadata(self) -> None:
        request = internal_request()
        planning_input = build_planning_input(build_provider_request(request))
        cases = (
            ("type", "the", "system prompt"),
            ("code", "Login", "user instruction"),
            ("type", "sign", "snapshot title"),
            ("code", "Welcome", "snapshot text"),
        )
        self.assertIn("the", SYSTEM_INSTRUCTIONS)
        self.assertIn("Login", request.instruction)
        self.assertIn("sign", request.snapshot.title)
        self.assertIn("Welcome", planning_input)

        for field, excerpt, source in cases:
            error = RuntimeError("raw SDK exception must not be displayed")
            error.status_code = 400
            error_metadata = {
                "type": "invalid_request_error",
                "details": {"error_code": "enforced_spend_limit_reached"},
                "message": f"Provider rejected request near: {excerpt}",
            }
            if field == "type":
                error_metadata["type"] = excerpt
            else:
                error_metadata["details"]["error_code"] = excerpt
            error.body = {
                "error": error_metadata,
                "request_id": "req_partial_excerpt_123",
            }
            provider = AnthropicProvider(
                settings=Settings(
                    anthropic_api_key="synthetic-diagnostic-key",
                    anthropic_model="claude-haiku-5-5",
                ),
                client=fake_client(error=error),
            )
            with self.subTest(source=source, excerpt=excerpt), self.assertRaises(PlanningProviderError) as raised:
                await PlannerService(provider).plan(request)

            diagnostic = raised.exception
            report = _safe_failure(diagnostic)
            self.assertEqual(diagnostic.provider_message, SAFE_SUPPRESSED_PROVIDER_MESSAGE)
            self.assertNotIn(excerpt, report)
            self.assertEqual(diagnostic.status_code, 400)
            self.assertEqual(diagnostic.diagnostic_category, "invalid_request_or_model")
            self.assertEqual(
                diagnostic.provider_error_type,
                "unknown" if field == "type" else "invalid_request_error",
            )
            self.assertEqual(
                diagnostic.provider_error_code,
                "unknown" if field == "code" else "enforced_spend_limit_reached",
            )
            self.assertEqual(diagnostic.request_id, "req_partial_excerpt_123")
            self.assertEqual(diagnostic.model_id, "claude-haiku-5-5")

    async def test_documented_anthropic_error_metadata_is_allowlisted(self) -> None:
        error = RuntimeError("private raw provider error")
        error.status_code = 429
        error.body = {
            "error": {
                "type": "rate_limit_error",
                "details": {"error_code": "enforced_spend_limit_reached"},
                "message": "private provider message",
            },
            "request_id": "req_allowlisted_123",
        }
        provider = AnthropicProvider(
            settings=Settings(
                anthropic_api_key="synthetic-key", anthropic_model="claude-haiku-5-5"
            ),
            client=fake_client(error=error),
        )

        with self.assertRaises(PlanningProviderError) as raised:
            await PlannerService(provider).plan(internal_request())

        diagnostic = raised.exception
        self.assertEqual(diagnostic.provider_error_type, "rate_limit_error")
        self.assertEqual(diagnostic.provider_error_code, "enforced_spend_limit_reached")
        self.assertEqual(diagnostic.status_code, 429)
        self.assertEqual(diagnostic.diagnostic_category, "rate_limit_quota")
        self.assertEqual(diagnostic.request_id, "req_allowlisted_123")
        self.assertEqual(diagnostic.model_id, "claude-haiku-5-5")
        self.assertEqual(diagnostic.provider_message, SAFE_SUPPRESSED_PROVIDER_MESSAGE)

    async def test_unknown_and_malformed_error_metadata_use_fixed_sentinel(self) -> None:
        for error_type, error_code in (
            ("future_error_type", "future_code"),
            ("invalid request excerpt", {"nested": "untrusted"}),
        ):
            error = RuntimeError("must not be displayed")
            error.status_code = 400
            error.body = {
                "error": {
                    "type": error_type,
                    "details": {"error_code": error_code},
                    "message": "private provider message",
                },
                "request_id": "req_unknown_metadata_123",
            }
            provider = AnthropicProvider(
                settings=Settings(anthropic_api_key="synthetic-key"),
                client=fake_client(error=error),
            )
            with self.subTest(error_type=error_type), self.assertRaises(PlanningProviderError) as raised:
                await PlannerService(provider).plan(internal_request())
            self.assertEqual(raised.exception.provider_error_type, "unknown")
            self.assertEqual(raised.exception.provider_error_code, "unknown")
            report = _safe_failure(raised.exception)
            self.assertNotIn(str(error_type), report)
            self.assertNotIn(str(error_code), report)

    async def test_undocumented_top_level_error_code_is_not_accepted(self) -> None:
        error = RuntimeError("private provider failure")
        error.status_code = 429
        error.body = {
            "error": {
                "type": "rate_limit_error",
                "code": "enforced_spend_limit_reached",
                "message": "private provider message",
            },
            "request_id": "req_wrong_code_location_123",
        }
        provider = AnthropicProvider(
            settings=Settings(
                anthropic_api_key="synthetic-key", anthropic_model="claude-haiku-5-5"
            ),
            client=fake_client(error=error),
        )

        with self.assertRaises(PlanningProviderError) as raised:
            await PlannerService(provider).plan(internal_request())

        diagnostic = raised.exception
        self.assertEqual(diagnostic.provider_error_type, "rate_limit_error")
        self.assertEqual(diagnostic.provider_error_code, "unknown")
        self.assertEqual(diagnostic.status_code, 429)
        self.assertEqual(diagnostic.diagnostic_category, "rate_limit_quota")
        self.assertEqual(diagnostic.request_id, "req_wrong_code_location_123")
        self.assertEqual(diagnostic.model_id, "claude-haiku-5-5")

    async def test_http_401_diagnostic_redacts_credentials_without_request_id(self) -> None:
        api_key = "sk-ant-api03-synthetic401Credential123456"
        error = RuntimeError("raw text including " + api_key)
        error.status_code = 401
        error.body = {
            "error": {
                "type": "authentication_error",
                "message": f"API key rejected: {api_key}; authorization: Bearer other-token-123456",
            }
        }
        error.response = SimpleNamespace(headers={})
        provider = AnthropicProvider(
            settings=Settings(anthropic_api_key=api_key),
            client=fake_client(error=error),
        )

        with self.assertRaises(PlanningProviderError) as raised:
            await PlannerService(provider).plan(internal_request())

        diagnostic = raised.exception
        self.assertEqual(diagnostic.status_code, 401)
        self.assertEqual(diagnostic.diagnostic_category, "authentication_authorization")
        self.assertEqual(diagnostic.provider_error_type, "authentication_error")
        self.assertEqual(diagnostic.provider_error_code, "unknown")
        self.assertIsNone(diagnostic.request_id)
        self.assertEqual(diagnostic.model_id, "claude-haiku-5-5")
        self.assertEqual(diagnostic.provider_message, SAFE_SUPPRESSED_PROVIDER_MESSAGE)
        report = _safe_failure(diagnostic)
        self.assertNotIn(api_key, report)
        self.assertNotIn("other-token-123456", report)
        self.assertIn(SAFE_SUPPRESSED_PROVIDER_MESSAGE, report)

    async def test_malformed_error_body_exposes_no_raw_message_or_request_id(self) -> None:
        error = RuntimeError("raw response body contains sk-ant-api03-privateSecret0123456789")
        error.status_code = 400
        error.body = "not a structured error envelope"
        error.request_id = "malformed request id with spaces"
        provider = AnthropicProvider(
            settings=Settings(anthropic_api_key="synthetic-key"),
            client=fake_client(error=error),
        )

        with self.assertRaises(PlanningProviderError) as raised:
            await PlannerService(provider).plan(internal_request())

        diagnostic = raised.exception
        self.assertEqual(diagnostic.status_code, 400)
        self.assertIsNone(diagnostic.provider_error_type)
        self.assertEqual(diagnostic.provider_error_code, "unknown")
        self.assertIsNone(diagnostic.provider_message)
        self.assertIsNone(diagnostic.request_id)
        self.assertNotIn("raw response body", _safe_failure(diagnostic))
        self.assertNotIn("privateSecret0123456789", _safe_failure(diagnostic))

    async def test_timeout_and_network_errors_are_safe_and_not_retried(self) -> None:
        for error, category in (
            (TimeoutError("synthetic secret"), "timeout"),
            (ConnectionError("authorization: synthetic secret"), "network_connectivity"),
        ):
            client = fake_client(error=error)
            provider = AnthropicProvider(
                settings=Settings(anthropic_api_key="synthetic secret"), client=client
            )
            with self.subTest(category=category), self.assertRaises(PlannerProviderError) as raised:
                await provider.generate_plan(build_provider_request(internal_request()))
            self.assertEqual(raised.exception.diagnostic_category, category)
            self.assertNotIn("synthetic secret", str(raised.exception))
            self.assertEqual(client.messages.create.await_count, 1)

    async def test_async_client_is_configured_without_sdk_retries_and_closed(self) -> None:
        client = fake_client(fake_response(valid_plan_json()))
        module = types.ModuleType("anthropic")
        constructed: list[dict[str, object]] = []

        def make_client(**kwargs: object) -> SimpleNamespace:
            constructed.append(kwargs)
            return client

        module.AsyncAnthropic = make_client
        provider = AnthropicProvider(
            settings=Settings(
                anthropic_api_key="synthetic-api-key",
                anthropic_request_timeout_seconds=17,
            )
        )
        with patch.dict(sys.modules, {"anthropic": module}):
            await provider.generate_plan(build_provider_request(internal_request()))
        self.assertEqual(constructed, [{
            "api_key": "synthetic-api-key", "timeout": 17, "max_retries": 0,
        }])
        await provider.aclose()
        client.aclose.assert_awaited_once()

    async def test_client_cleanup_error_is_normalized(self) -> None:
        client = fake_client()
        client.aclose.side_effect = RuntimeError("authorization synthetic secret")
        provider = AnthropicProvider(settings=Settings(anthropic_api_key="key"), client=client)
        with self.assertRaisesRegex(PlannerProviderError, "Anthropic client cleanup failed") as raised:
            await provider.aclose()
        self.assertNotIn("synthetic secret", str(raised.exception))

    async def test_missing_key_fails_before_client_construction(self) -> None:
        provider = AnthropicProvider(settings=Settings(ai_provider="anthropic"))
        with self.assertRaisesRegex(PlannerProviderError, "API key is not configured"):
            await provider.generate_plan(build_provider_request(internal_request()))


class ProviderSelectionTests(unittest.TestCase):
    def test_gemini_remains_the_default(self) -> None:
        settings = Settings()
        self.assertEqual(settings.ai_provider, "gemini")
        self.assertIsInstance(create_planner_provider(settings), GeminiProvider)

    def test_provider_name_and_timeout_are_loaded_from_environment(self) -> None:
        with patch(
            "app.config._load_dotenv_file",
            side_effect=lambda _path: os.environ.setdefault("AI_PROVIDER", "gemini"),
        ):
            with patch.dict(os.environ, {
                "AI_PROVIDER": "anthropic",
                "ANTHROPIC_API_KEY": "synthetic-env-key",
                "ANTHROPIC_MODEL": "claude-haiku-5-5",
                "ANTHROPIC_REQUEST_TIMEOUT_SECONDS": "22",
            }, clear=False):
                settings = get_settings()
        self.assertEqual(settings.ai_provider, "anthropic")
        self.assertEqual(settings.anthropic_api_key, "synthetic-env-key")
        self.assertEqual(settings.anthropic_model, "claude-haiku-5-5")
        self.assertEqual(settings.anthropic_request_timeout_seconds, 22)
        self.assertEqual(Settings(ai_provider=" Gemini ").ai_provider, "gemini")

    def test_anthropic_selection_is_explicit_and_never_falls_back(self) -> None:
        with self.assertRaisesRegex(PlannerProviderError, "Anthropic API key is not configured"):
            create_planner_provider(Settings(ai_provider="anthropic"))

    def test_invalid_provider_is_rejected_with_safe_message(self) -> None:
        with self.assertRaisesRegex(ValueError, "AI_PROVIDER must be gemini or anthropic"):
            Settings(ai_provider="unsupported-provider-name")

    def test_provider_factory_fails_closed_for_custom_invalid_settings(self) -> None:
        settings = SimpleNamespace(ai_provider="other", anthropic_api_key="", anthropic_model="")
        with self.assertRaisesRegex(PlannerProviderError, "provider is unsupported"):
            create_planner_provider(settings)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
