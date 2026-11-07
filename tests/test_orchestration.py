"""Mocked end-to-end tests for planning, execution, and independent verification."""

from __future__ import annotations

import asyncio
import json
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

from app.api.schemas import TestExecutionRequest, TestOutcome
from app.browser.snapshot import PageSnapshot
from app.browser.locator import DynamicLocatorResolver
from app.execution.executor import SafeActionExecutor
from app.orchestration.runner import TestOrchestrator
from app.planner.provider import PlannerProviderError, PlannerProviderResponse
from app.planner.service import PlannerService
from app.browser.manager import BrowserSession
from app.security.url_policy import URLPolicyError


def plan(*actions: dict[str, object]) -> str:
    return json.dumps({"version": 1, "goal": "Complete the sign-in check", "actions": list(actions)})


def text_assertion(text: str) -> dict[str, object]:
    return {"type": "assert_text", "expected_text": text}


class QueueProvider:
    def __init__(self, *responses: str | Exception) -> None:
        self.responses = list(responses)
        self.received: list[object] = []

    async def generate_plan(self, request: object) -> PlannerProviderResponse:
        self.received.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return PlannerProviderResponse(content=response)


class FakeLocator:
    def __init__(self, page: "FakePage", *, count: int = 1, name: str = "") -> None:
        self.page = page
        self.match_count = count
        self.name = name

    async def count(self) -> int:
        return self.match_count

    def nth(self, index: int) -> "FakeLocator":
        return FakeLocator(self.page, count=1 if index < self.match_count else 0, name=self.name)

    async def is_visible(self) -> bool:
        return self.match_count > 0

    async def is_enabled(self) -> bool:
        return True

    async def is_editable(self) -> bool:
        return True

    async def element_handle(self) -> "FakeLocator | None":
        return self if self.match_count == 1 else None

    async def evaluate(self, _expression: str, expected: object) -> bool:
        return expected is self

    async def click(self) -> None:
        if self.page.timeout_action:
            await asyncio.Event().wait()
        if self.name == "Continue" and self.page.fail_continue_attempts:
            self.page.fail_continue_attempts -= 1
            raise RuntimeError("simulated temporary failure")
        if self.name in {"Login", "Continue"}:
            self.page.logged_in = True

    async def get_attribute(self, name: str) -> str | None:
        if name == "type":
            return "password" if self.name == "Password" else "text"
        return None

    async def fill(self, value: str) -> None:
        self.page.filled_value = value

    async def inner_text(self) -> str:
        return "Login form displayed" if self.page.logged_in else "Login"


class FakePage:
    def __init__(
        self, *, ambiguous: bool = False, timeout_action: bool = False,
        continue_available: bool = False, fail_continue_attempts: int = 0,
        password_available: bool = False,
    ) -> None:
        self.url = "https://example.test/"
        self.logged_in = False
        self.ambiguous = ambiguous
        self.timeout_action = timeout_action
        self.continue_available = continue_available
        self.fail_continue_attempts = fail_continue_attempts
        self.password_available = password_available
        self.filled_value = ""

    def get_by_role(self, *_args: object, **kwargs: object) -> FakeLocator:
        name = str(kwargs.get("name", ""))
        if name == "Login":
            count = (2 if self.ambiguous else 1) if not self.logged_in else 0
        elif name == "Continue":
            count = 1 if self.continue_available and not self.logged_in else 0
        else:
            count = 0
        return FakeLocator(self, count=count, name=name)

    def get_by_label(self, label: str, **_kwargs: object) -> FakeLocator:
        return FakeLocator(
            self, count=1 if label == "Password" and self.password_available else 0,
            name=label,
        )

    def get_by_text(self, *_args: object, **_kwargs: object) -> FakeLocator:
        return FakeLocator(self)


def snapshot_for(page: FakePage) -> PageSnapshot:
    if page.logged_in:
        return PageSnapshot(
            url=page.url,
            title="Login form",
            visible_text="Login form displayed",
            elements=[{"kind": "input", "role": "textbox", "label": "Email"}],
        )
    buttons = [
        {"kind": "button", "role": "button", "accessible_name": "Login", "text": "Login"}
    ]
    if page.continue_available:
        buttons.append(
            {"kind": "button", "role": "button", "accessible_name": "Continue", "text": "Continue"}
        )
    if page.password_available:
        buttons.append({
            "kind": "input", "role": "textbox", "accessible_name": "Password",
            "label": "Password", "input_type": "password",
        })
    if page.ambiguous:
        buttons.append(buttons[0].copy())
    return PageSnapshot(
        url=page.url,
        title="Example",
        visible_text="Login",
        elements=buttons,
    )


class FakeSessionContext:
    def __init__(self, page: FakePage, *, cleanup_error: bool = False) -> None:
        self.session = BrowserSession(None, None, None, page)
        self.cleanup_error = cleanup_error
        self.exited = False

    async def __aenter__(self) -> BrowserSession:
        return self.session

    async def __aexit__(self, *_args: object) -> bool:
        self.exited = True
        if self.cleanup_error:
            raise RuntimeError("private cleanup details")
        return False


def make_runner(
    responses: tuple[str | Exception, ...],
    *,
    page: FakePage | None = None,
    cleanup_error: bool = False,
    max_replans: int = 3,
    action_timeout: float = 1,
    run_timeout: float = 300,
    max_total_actions: int = 50,
) -> tuple[TestOrchestrator, QueueProvider, FakeSessionContext]:
    page = page or FakePage()
    provider = QueueProvider(*responses)
    context = FakeSessionContext(page, cleanup_error=cleanup_error)

    def session_factory(_url: str) -> FakeSessionContext:
        return context

    return (
        TestOrchestrator(
            PlannerService(provider),
            session_factory=session_factory,
            inspector=lambda current: _inspect(current),
            executor_factory=lambda current, max_actions: SafeActionExecutor(
                current,
                max_actions=max_actions,
                action_timeout_seconds=action_timeout,
                locator_resolver=DynamicLocatorResolver(current, inspector=_inspect),
            ),
            max_replans=max_replans,
            run_timeout_seconds=run_timeout,
            max_total_actions=max_total_actions,
        ),
        provider,
        context,
    )


async def _inspect(page: FakePage) -> PageSnapshot:
    return snapshot_for(page)


class OrchestrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.url_patch = patch(
            "app.orchestration.runner.validate_target_url",
            new=AsyncMock(return_value="https://example.test/"),
        )
        self.url_patch.start()
        self.addCleanup(self.url_patch.stop)

    async def test_orchestrator_cleanup_grace_uses_shared_settings_bounds(self) -> None:
        for grace in (0.1, 60.0):
            runner = TestOrchestrator(
                PlannerService(QueueProvider()), cleanup_grace_seconds=grace
            )
            self.assertEqual(runner.cleanup_grace_seconds, grace)
        for grace in (-1, 0, 0.09, 60.01, float("nan"), float("inf"), -float("inf"), True):
            with self.subTest(grace=grace), self.assertRaises(ValueError):
                TestOrchestrator(PlannerService(QueueProvider()), cleanup_grace_seconds=grace)

    async def test_integrated_mock_plan_execute_replan_and_verify_pass(self) -> None:
        runner, provider, _context = make_runner((
            plan({"type": "click", "target": {"kind": "role", "role": "button", "value": "Login"}}),
            plan(text_assertion("Login form displayed")),
        ))
        result = await runner.run(TestExecutionRequest(
            target_url="https://example.test/",
            instruction="Click Login and verify the login form is displayed.",
        ))
        self.assertEqual(result.outcome, TestOutcome.PASS)
        self.assertEqual(len(result.steps), 1)
        self.assertEqual(result.verified_assertions[0].status, "PASS")
        self.assertEqual(len(provider.received), 2)

    async def test_required_failed_assertion_returns_fail(self) -> None:
        runner, _provider, _context = make_runner((plan(text_assertion("Missing confirmation")),))
        result = await runner.run(TestExecutionRequest(
            target_url="https://example.test/", instruction="Verify confirmation."
        ))
        self.assertEqual(result.outcome, TestOutcome.FAIL)
        self.assertEqual(result.verified_assertions[0].status, "FAIL")

    async def test_exact_successful_retry_explicitly_recovers_the_failed_action(self) -> None:
        runner, provider, _context = make_runner((
            plan({"type": "click", "target": {"kind": "role", "role": "button", "value": "Continue"}}),
            plan({"type": "click", "target": {"kind": "role", "role": "button", "value": "Continue"}}),
            plan(text_assertion("Login form displayed")),
        ), page=FakePage(continue_available=True, fail_continue_attempts=1), max_replans=2)
        result = await runner.run(TestExecutionRequest(
            target_url="https://example.test/", instruction="Continue and verify Login."
        ))
        self.assertEqual(result.outcome, TestOutcome.PASS)
        self.assertEqual(result.steps[0].status, "INCONCLUSIVE")
        self.assertEqual(result.steps[1].status, "PASS")
        self.assertEqual(len(provider.received), 3)

    async def test_identical_unresolved_fills_are_not_retried_by_replanning(self) -> None:
        fill_location = {
            "type": "fill",
            "target": {"kind": "label", "value": "Email"},
            "value_ref": "destination",
        }
        page = FakePage()
        page.logged_in = True
        runner, provider, _context = make_runner((
            plan(fill_location, fill_location, text_assertion("Search results")),
            plan(fill_location, fill_location, text_assertion("Search results")),
        ), page=page, max_replans=1)
        result = await runner.run(TestExecutionRequest(
            target_url="https://example.test/",
            instruction="Fill the location field with London and verify results.",
            test_data={"destination": "London"},
        ))
        fill_steps = [step for step in result.steps if step.action_type == "fill"]
        self.assertEqual(result.outcome, TestOutcome.INCONCLUSIVE)
        self.assertEqual([step.status for step in fill_steps], ["INCONCLUSIVE"])
        self.assertEqual([step.attempt_number for step in fill_steps], [1])
        self.assertEqual(len(provider.received), 1)

    async def test_unresolved_fill_does_not_replan_navigation_or_create_another_fill(self) -> None:
        navigation = {"type": "navigate", "url": "https://example.test/search"}
        fill_action = {
            "type": "fill",
            "target": {"kind": "label", "value": "Email"},
            "value_ref": "destination",
        }
        repeated_plan = plan(navigation, fill_action, text_assertion("Search results"))
        page = FakePage()
        page.logged_in = True
        runner, provider, _context = make_runner(
            (repeated_plan, repeated_plan), page=page, max_replans=2
        )
        with patch("app.browser.manager.navigate_page", new=AsyncMock()) as navigate:
            result = await runner.run(TestExecutionRequest(
                target_url="https://example.test/",
                instruction="Fill the destination field and verify results.",
                test_data={"destination": "London"},
            ))
        self.assertEqual(result.outcome, TestOutcome.INCONCLUSIVE)
        self.assertEqual(len(provider.received), 1)
        self.assertEqual(navigate.await_count, 1)
        self.assertEqual([step.action_type for step in result.steps], ["navigate", "fill"])

    async def test_fill_lookup_diagnostic_reaches_report_without_target_or_value(self) -> None:
        target_text = "synthetic private target"
        fill_value = "synthetic sensitive fill value"
        page = FakePage()
        snapshots = [
            PageSnapshot(
                url=page.url, title="Search", visible_text="",
                elements=[{"kind": "input", "role": "textbox", "label": target_text}],
            ),
            PageSnapshot(url=page.url, title="Search", visible_text="", elements=[]),
            PageSnapshot(url=page.url, title="Search", visible_text="", elements=[]),
        ]

        async def inspect_in_sequence(_page: FakePage) -> PageSnapshot:
            return snapshots.pop(0)

        provider = QueueProvider(
            plan({
                "type": "fill",
                "target": {"kind": "label", "value": target_text},
                "value": fill_value,
            }),
            plan(text_assertion("Search completed")),
        )
        context = FakeSessionContext(page)
        runner = TestOrchestrator(
            PlannerService(provider),
            session_factory=lambda _url: context,
            inspector=inspect_in_sequence,
            executor_factory=lambda current, max_actions: SafeActionExecutor(
                current,
                max_actions=max_actions,
                locator_resolver=DynamicLocatorResolver(current, inspector=inspect_in_sequence),
            ),
            max_replans=1,
        )
        result = await runner.run(TestExecutionRequest(
            target_url="https://example.test/", instruction="Fill the missing field safely."
        ))
        fill_step = next(step for step in result.steps if step.action_type == "fill")
        self.assertEqual(fill_step.status, "INCONCLUSIVE")
        self.assertEqual(fill_step.fill_diagnostic.reason_code, "locator_mismatch")
        self.assertEqual(fill_step.fill_diagnostic.target_kind, "label")
        self.assertEqual(fill_step.fill_diagnostic.locator_strategy, "get_by_label")
        self.assertEqual(fill_step.fill_diagnostic.snapshot_match, "missing")
        self.assertFalse(fill_step.fill_diagnostic.snapshot_truncated)
        serialized = result.model_dump_json()
        self.assertNotIn(target_text, serialized)
        self.assertNotIn(fill_value, serialized)

    async def test_duplicate_identical_actions_keep_independent_occurrence_obligations(self) -> None:
        click_continue = {
            "type": "click",
            "target": {"kind": "role", "role": "button", "value": "Continue"},
        }
        runner, _provider, _context = make_runner((
            plan(click_continue, click_continue, text_assertion("Login form displayed")),
            plan(click_continue, text_assertion("Login form displayed")),
        ), page=FakePage(continue_available=True, fail_continue_attempts=1), max_replans=1)
        result = await runner.run(TestExecutionRequest(
            target_url="https://example.test/", instruction="Continue twice and verify Login."
        ))
        action_steps = [step for step in result.steps if step.action_type == "click"]
        self.assertEqual(result.outcome, TestOutcome.INCONCLUSIVE)
        self.assertEqual([step.status for step in action_steps], ["INCONCLUSIVE", "SKIPPED", "PASS"])
        self.assertEqual(action_steps[0].occurrence_id, action_steps[2].occurrence_id)
        self.assertNotEqual(action_steps[1].occurrence_id, action_steps[2].occurrence_id)
        self.assertEqual(action_steps[0].attempt_number, 1)
        self.assertEqual(action_steps[2].attempt_number, 2)
        self.assertIn("1 planned action occurrence remains unresolved", result.summary)
        self.assertEqual(result.verified_assertions[0].status, "INCONCLUSIVE")

    async def test_two_identical_action_occurrences_can_both_complete(self) -> None:
        runner, _provider, _context = make_runner((plan(
            {"type": "wait", "milliseconds": 0},
            {"type": "wait", "milliseconds": 0},
            text_assertion("Login"),
        ),), max_replans=0)
        result = await runner.run(TestExecutionRequest(
            target_url="https://example.test/", instruction="Wait twice and verify Login."
        ))
        action_steps = [step for step in result.steps if step.action_type == "wait"]
        self.assertEqual(result.outcome, TestOutcome.PASS)
        self.assertEqual([step.status for step in action_steps], ["PASS", "PASS"])
        self.assertNotEqual(action_steps[0].occurrence_id, action_steps[1].occurrence_id)

    async def test_reordered_replan_matches_occurrences_one_to_one(self) -> None:
        click_continue = {
            "type": "click",
            "target": {"kind": "role", "role": "button", "value": "Continue"},
        }
        runner, _provider, _context = make_runner((
            plan(click_continue,
                 {"type": "wait", "milliseconds": 0},
                 {"type": "wait", "milliseconds": 1},
                 text_assertion("Login form displayed")),
            plan({"type": "wait", "milliseconds": 1},
                 {"type": "wait", "milliseconds": 0},
                 click_continue,
                 text_assertion("Login form displayed")),
        ), page=FakePage(continue_available=True, fail_continue_attempts=1), max_replans=1)
        result = await runner.run(TestExecutionRequest(
            target_url="https://example.test/", instruction="Continue, wait twice, verify Login."
        ))
        skipped_waits = [step for step in result.steps if step.action_type == "wait" and step.status == "SKIPPED"]
        completed_waits = [step for step in result.steps if step.action_type == "wait" and step.status == "PASS"]
        self.assertEqual(result.outcome, TestOutcome.PASS)
        self.assertEqual(len(skipped_waits), 2)
        self.assertEqual(len(completed_waits), 2)
        self.assertEqual(
            {step.occurrence_id for step in skipped_waits},
            {step.occurrence_id for step in completed_waits},
        )

    async def test_ambiguous_duplicate_replan_preserves_unmatched_occurrence(self) -> None:
        click_continue = {
            "type": "click",
            "target": {"kind": "role", "role": "button", "value": "Continue"},
        }
        runner, _provider, _context = make_runner((
            plan(click_continue,
                 {"type": "wait", "milliseconds": 0},
                 {"type": "wait", "milliseconds": 0},
                 text_assertion("Login")),
            plan(click_continue, {"type": "wait", "milliseconds": 0}, text_assertion("Login")),
        ), page=FakePage(continue_available=True, fail_continue_attempts=1), max_replans=1)
        result = await runner.run(TestExecutionRequest(
            target_url="https://example.test/", instruction="Continue and wait twice."
        ))
        self.assertEqual(result.outcome, TestOutcome.INCONCLUSIVE)
        self.assertIn("1 planned action occurrence remains unresolved", result.summary)
        skipped = [step for step in result.steps if step.action_type == "wait" and step.status == "SKIPPED"]
        passed = [step for step in result.steps if step.action_type == "wait" and step.status == "PASS"]
        self.assertEqual(len(skipped), 2)
        self.assertEqual(len(passed), 1)
        self.assertIn(passed[0].occurrence_id, {step.occurrence_id for step in skipped})

    async def test_failed_click_wait_and_preexisting_text_cannot_pass(self) -> None:
        runner, _provider, _context = make_runner((
            plan(
                {"type": "click", "target": {"kind": "role", "role": "button", "value": "Continue"}},
                {"type": "wait", "milliseconds": 0},
                text_assertion("Login"),
            ),
            plan({"type": "wait", "milliseconds": 0}, text_assertion("Login")),
        ), max_replans=1)
        result = await runner.run(TestExecutionRequest(
            target_url="https://example.test/", instruction="Click Continue and verify Login."
        ))
        self.assertTrue(any(step.action_type == "click" and step.status == "INCONCLUSIVE" for step in result.steps))
        self.assertTrue(any(step.action_type == "wait" and step.status == "SKIPPED" for step in result.steps))
        self.assertEqual(result.outcome, TestOutcome.INCONCLUSIVE)

    async def test_successful_retry_does_not_erase_an_unrelated_skipped_action(self) -> None:
        runner, _provider, _context = make_runner((
            plan(
                {"type": "click", "target": {"kind": "role", "role": "button", "value": "Continue"}},
                {"type": "click", "target": {"kind": "role", "role": "button", "value": "Login"}},
                text_assertion("Login form displayed"),
            ),
            plan(
                {"type": "click", "target": {"kind": "role", "role": "button", "value": "Continue"}},
                text_assertion("Login form displayed"),
            ),
        ), page=FakePage(continue_available=True, fail_continue_attempts=1), max_replans=1)
        result = await runner.run(TestExecutionRequest(
            target_url="https://example.test/", instruction="Continue, then Login."
        ))
        self.assertTrue(any(step.action_type == "click" and step.status == "SKIPPED" for step in result.steps))
        self.assertEqual(result.outcome, TestOutcome.INCONCLUSIVE)

    async def test_state_change_does_not_abandon_remaining_plan_actions(self) -> None:
        runner, _provider, _context = make_runner((plan(
            {"type": "click", "target": {"kind": "role", "role": "button", "value": "Login"}},
            {"type": "click", "target": {"kind": "role", "role": "button", "value": "Continue"}},
            text_assertion("Login form displayed"),
        ),), page=FakePage(continue_available=False), max_replans=0)
        result = await runner.run(TestExecutionRequest(
            target_url="https://example.test/", instruction="Open Login then Continue."
        ))
        self.assertEqual(result.outcome, TestOutcome.INCONCLUSIVE)
        self.assertEqual([step.action_type for step in result.steps[:2]], ["click", "click"])
        self.assertEqual(result.steps[0].status, "PASS")
        self.assertEqual(result.steps[1].status, "INCONCLUSIVE")

    async def test_total_action_limit_reports_unattempted_plan_actions_as_skipped(self) -> None:
        runner, _provider, _context = make_runner((plan(
            {"type": "click", "target": {"kind": "role", "role": "button", "value": "Login"}},
            {"type": "click", "target": {"kind": "role", "role": "button", "value": "Continue"}},
            text_assertion("Login form displayed"),
        ),), max_replans=0, max_total_actions=1)
        result = await runner.run(TestExecutionRequest(
            target_url="https://example.test/", instruction="Open Login then Continue."
        ))
        self.assertEqual(result.outcome, TestOutcome.INCONCLUSIVE)
        self.assertTrue(any(step.action_type == "click" and step.status == "SKIPPED" for step in result.steps))

    async def test_ambiguous_target_is_not_clicked(self) -> None:
        page = FakePage(ambiguous=True)
        runner, _provider, _context = make_runner((
            plan({"type": "click", "target": {"kind": "role", "role": "button", "value": "Login"}}),
        ), page=page, max_replans=0)
        result = await runner.run(TestExecutionRequest(
            target_url="https://example.test/", instruction="Click Login."
        ))
        self.assertFalse(page.logged_in)
        self.assertEqual(result.outcome, TestOutcome.INCONCLUSIVE)
        self.assertIn("more than one", result.steps[0].explanation)

    async def test_unsupported_provider_action_fails_canonical_validation(self) -> None:
        runner, _provider, _context = make_runner((plan({"type": "execute", "code": "bad"}),))
        result = await runner.run(TestExecutionRequest(
            target_url="https://example.test/", instruction="Run a test."
        ))
        self.assertEqual(result.outcome, TestOutcome.INCONCLUSIVE)
        self.assertNotIn("bad", result.summary)

    async def test_planner_error_is_safe_and_inconclusive(self) -> None:
        runner, _provider, _context = make_runner((PlannerProviderError("private provider text"),))
        result = await runner.run(TestExecutionRequest(
            target_url="https://example.test/", instruction="Verify something."
        ))
        self.assertEqual(result.outcome, TestOutcome.INCONCLUSIVE)
        self.assertNotIn("private", result.summary)

    async def test_action_timeout_is_reported_without_exception_details(self) -> None:
        page = FakePage(timeout_action=True)
        runner, _provider, _context = make_runner((
            plan({"type": "click", "target": {"kind": "role", "role": "button", "value": "Login"}}),
        ), page=page, max_replans=0, action_timeout=0.01)
        result = await runner.run(TestExecutionRequest(
            target_url="https://example.test/", instruction="Click Login."
        ))
        self.assertEqual(result.outcome, TestOutcome.INCONCLUSIVE)
        self.assertIn("time limit", result.steps[0].explanation)

    async def test_replanning_limit_does_not_claim_success(self) -> None:
        runner, provider, _context = make_runner((
            plan(
                {"type": "click", "target": {"kind": "role", "role": "button", "value": "Login"}},
                text_assertion("never present"),
            ),
        ), max_replans=0)
        result = await runner.run(TestExecutionRequest(
            target_url="https://example.test/", instruction="Open Login then verify text."
        ))
        self.assertEqual(len(provider.received), 1)
        self.assertEqual(result.outcome, TestOutcome.FAIL)

    async def test_browser_cleanup_failure_downgrades_result(self) -> None:
        runner, _provider, context = make_runner((plan(text_assertion("Login")),), cleanup_error=True)
        result = await runner.run(TestExecutionRequest(
            target_url="https://example.test/", instruction="Verify Login."
        ))
        self.assertTrue(context.exited)
        self.assertEqual(result.outcome, TestOutcome.INCONCLUSIVE)
        self.assertNotIn("private cleanup", result.summary)

    async def test_cancellation_propagates_and_session_cleanup_runs(self) -> None:
        started = asyncio.Event()

        class BlockingProvider:
            async def generate_plan(self, _request: object) -> PlannerProviderResponse:
                started.set()
                await asyncio.Event().wait()
                return PlannerProviderResponse(content="{}")

        page = FakePage()
        context = FakeSessionContext(page)
        runner = TestOrchestrator(
            PlannerService(BlockingProvider()),
            session_factory=lambda _url: context,
            inspector=_inspect,
        )
        task = asyncio.create_task(runner.run(TestExecutionRequest(
            target_url="https://example.test/", instruction="Check something."
        )))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(context.exited)

    async def test_run_wide_deadline_returns_inconclusive_and_closes_session(self) -> None:
        started = asyncio.Event()

        class SlowProvider:
            async def generate_plan(self, _request: object) -> PlannerProviderResponse:
                started.set()
                await asyncio.sleep(10)
                return PlannerProviderResponse(content="{}")

        context = FakeSessionContext(FakePage(), cleanup_error=True)
        runner = TestOrchestrator(
            PlannerService(SlowProvider()), session_factory=lambda _url: context,
            inspector=_inspect, run_timeout_seconds=0.01,
        )
        result = await runner.run(TestExecutionRequest(
            target_url="https://example.test/", instruction="Check something."
        ))
        self.assertTrue(started.is_set())
        self.assertTrue(context.exited)
        self.assertEqual(result.outcome, TestOutcome.INCONCLUSIVE)
        self.assertIn("overall time limit", result.summary)

    async def test_run_deadline_returns_after_bounded_session_cleanup(self) -> None:
        class BoundedCleanupContext(FakeSessionContext):
            def __init__(self, page: FakePage) -> None:
                super().__init__(page)
                self.cleanup_timed_out = False
                self.remaining_resource_closed = False

            async def __aexit__(self, *_args: object) -> bool:
                self.exited = True
                try:
                    await asyncio.wait_for(asyncio.Event().wait(), timeout=0.02)
                except asyncio.TimeoutError:
                    self.cleanup_timed_out = True
                self.remaining_resource_closed = True
                return False

        class SlowProvider:
            async def generate_plan(self, _request: object) -> PlannerProviderResponse:
                await asyncio.Event().wait()
                return PlannerProviderResponse(content="{}")

        context = BoundedCleanupContext(FakePage())
        runner = TestOrchestrator(
            PlannerService(SlowProvider()), session_factory=lambda _url: context,
            inspector=_inspect, run_timeout_seconds=0.01, cleanup_grace_seconds=0.1,
        )
        started = asyncio.get_running_loop().time()
        result = await asyncio.wait_for(runner.run(TestExecutionRequest(
            target_url="https://example.test/", instruction="Check something."
        )), timeout=0.3)
        elapsed = asyncio.get_running_loop().time() - started
        self.assertEqual(result.outcome, TestOutcome.INCONCLUSIVE)
        self.assertIn("overall time limit", result.summary)
        self.assertTrue(context.cleanup_timed_out)
        self.assertTrue(context.remaining_resource_closed)
        self.assertLess(elapsed, 0.2)

    async def test_sensitive_value_stays_out_of_provider_request_and_report(self) -> None:
        secret = "private-user@example.test"
        runner, provider, _context = make_runner((plan(text_assertion("Login")),))
        result = await runner.run(TestExecutionRequest(
            target_url="https://example.test/",
            instruction=f"Use {secret} only if needed and verify Login.",
            test_data={"email": secret},
        ))
        provider_request = provider.received[0]
        self.assertNotIn(secret, provider_request.model_dump_json())
        self.assertNotIn(secret, result.model_dump_json())

    async def test_password_value_ref_stays_out_of_provider_and_api_report(self) -> None:
        secret = "synthetic-password-only"
        runner, provider, _ = make_runner((plan(
            {"type": "fill", "target": {"kind": "label", "value": "Password"}, "value_ref": "password"},
            text_assertion("Login"),
        ),), page=FakePage(password_available=True))
        result = await runner.run(TestExecutionRequest(
            target_url="https://example.test/", instruction="Fill the password and verify Login.",
            test_data={"password": secret},
        ))
        self.assertEqual(result.outcome, TestOutcome.PASS)
        self.assertEqual(provider.received[0].available_test_data_keys, ("password",))
        self.assertNotIn(secret, provider.received[0].model_dump_json())
        self.assertNotIn(secret, result.model_dump_json())

    async def test_unsafe_url_is_rejected_before_opening_session(self) -> None:
        session_factory = AsyncMock()
        with patch(
            "app.orchestration.runner.validate_target_url",
            new=AsyncMock(side_effect=URLPolicyError("private target")),
        ):
            runner = TestOrchestrator(PlannerService(QueueProvider()), session_factory=session_factory)
            result = await runner.run(TestExecutionRequest(
                target_url="http://127.0.0.1/", instruction="Do a test."
            ))
        self.assertEqual(result.outcome, TestOutcome.FAIL)
        session_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
