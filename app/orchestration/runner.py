"""Coordinate browser sessions, planning, bounded replanning, and verification."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from app.api.schemas import (
    Action,
    AssertTextAction,
    AssertUrlAction,
    AssertVisibleAction,
    ExecutionStepResult,
    FillAction,
    MAX_EXECUTION_STEPS,
    TestExecutionRequest,
    TestExecutionResponse,
    TestOutcome,
    VerifiedAssertion,
)
from app.browser.inspector import inspect_page
from app.browser.manager import (
    DEFAULT_CLEANUP_GRACE_SECONDS,
    BrowserSession,
    browser_session,
)
from app.browser.snapshot import PageSnapshot
from app.config import validate_cleanup_grace_seconds
from app.execution.executor import ActionExecutionError, SafeActionExecutor
from app.execution.verification import AssertionVerifier
from app.planner.provider import InternalPlanningInput
from app.planner.service import (
    PlannerService,
    PlanningError,
)
from app.security.url_policy import URLPolicyError, validate_target_url


MAX_REPLANS = 3
MAX_TOTAL_ACTIONS = 50
MAX_TOTAL_ASSERTIONS = 50
DEFAULT_RUN_TIMEOUT_SECONDS = 300.0
_ASSERTION_TYPES = (AssertTextAction, AssertUrlAction, AssertVisibleAction)
_TERMINAL_FILL_LOOKUP_FAILURES = frozenset({
    "target_not_in_snapshot",
    "live_no_match",
    "live_not_visible",
    "live_ambiguous",
    "role_or_name_mismatch",
    "locator_mismatch",
    "locator_error",
    "target_not_interactable",
})


@dataclass(slots=True)
class _ActionOccurrence:
    """One planned action occurrence, independent of identical action content."""

    occurrence_id: str
    action_token: str
    state: str = "planned"
    attempts: int = 0


def _is_assertion(action: Action) -> bool:
    return isinstance(action, _ASSERTION_TYPES)


def _dedupe_redundant_fill_actions(actions: list[Action]) -> list[Action]:
    """Collapse identical fills in one plan while preserving distinct values and targets."""

    result: list[Action] = []
    seen_fills: set[bytes] = set()
    for action in actions:
        if isinstance(action, FillAction):
            token = hashlib.sha256(action.model_dump_json().encode("utf-8")).digest()
            if token in seen_fills:
                continue
            seen_fills.add(token)
        result.append(action)
    return result


def _safe_failure_summary(exc: ActionExecutionError) -> str:
    messages = {
        "missing": "A planned page element was not found.",
        "ambiguous": "A planned target matched more than one visible element.",
        "not_interactable": "The planned target is disabled or cannot be edited.",
        "timeout": "A browser action exceeded its time limit.",
        "detached_element": "The selected page element detached before the action completed.",
        "closed_page": "The browser page or context closed during the action.",
        "unsafe_navigation": "A requested navigation was rejected by URL policy.",
        "action_limit": "The run reached its action limit.",
        "unsupported_action": "The plan contained an action that cannot be executed.",
        "missing_test_data": "A referenced test-data value was unavailable.",
        "sensitive_value_ref_required": "Password fields require a referenced test-data value.",
        "infrastructure": "The browser could not inspect or safely use the target.",
        "browser_action": "The browser action could not be completed.",
    }
    return messages.get(exc.category, "A planned browser action could not be completed.")


class TestOrchestrator:
    """Run one test case. Planner, session, inspector, and executor are injectable."""

    def __init__(
        self,
        planner: PlannerService,
        *,
        session_factory: Callable[..., AbstractAsyncContextManager[BrowserSession]] = browser_session,
        inspector: Callable[[Any], Any] = inspect_page,
        executor_factory: Callable[..., SafeActionExecutor] = SafeActionExecutor,
        max_replans: int = MAX_REPLANS,
        max_total_actions: int = MAX_TOTAL_ACTIONS,
        run_timeout_seconds: float = DEFAULT_RUN_TIMEOUT_SECONDS,
        cleanup_grace_seconds: float = DEFAULT_CLEANUP_GRACE_SECONDS,
    ) -> None:
        cleanup_grace_seconds = validate_cleanup_grace_seconds(cleanup_grace_seconds)
        if (
            max_replans < 0
            or max_total_actions <= 0
            or isinstance(run_timeout_seconds, bool)
            or run_timeout_seconds <= 0
            or not float(run_timeout_seconds) < float("inf")
        ):
            raise ValueError("Orchestration limits are invalid")
        self.planner = planner
        self.session_factory = session_factory
        self.inspector = inspector
        self.executor_factory = executor_factory
        self.max_replans = max_replans
        self.max_total_actions = max_total_actions
        self.run_timeout_seconds = float(run_timeout_seconds)
        self.cleanup_grace_seconds = float(cleanup_grace_seconds)

    async def run(self, request: TestExecutionRequest) -> TestExecutionResponse:
        run_id = uuid4()
        steps: list[ExecutionStepResult] = []
        verified: list[VerifiedAssertion] = []
        deadline = asyncio.timeout(self.run_timeout_seconds)
        try:
            async with deadline:
                canonical_url = await validate_target_url(str(request.target_url))
                session_context = (
                    self.session_factory(
                        canonical_url,
                        cleanup_grace_seconds=self.cleanup_grace_seconds,
                    )
                    if self.session_factory is browser_session
                    else self.session_factory(canonical_url)
                )
                async with session_context as session:
                    return await self._run_session(request, session, run_id, steps, verified)
        except URLPolicyError:
            return self._response(
                run_id, TestOutcome.FAIL, "The target URL was rejected by URL policy.", steps, verified
            )
        except TimeoutError:
            self._append_step(
                steps,
                ExecutionStepResult(
                    index=len(steps), action_type="run", status="INCONCLUSIVE",
                    explanation="The run-wide time limit expired.",
                ),
            )
            return self._response(
                run_id, TestOutcome.INCONCLUSIVE,
                "The run exceeded its overall time limit before completion.", steps, verified,
            )
        except Exception:
            # A cleanup exception can replace the timeout exception while the
            # context manager unwinds. Preserve the deadline outcome in that case.
            if deadline.expired():
                return self._response(
                    run_id, TestOutcome.INCONCLUSIVE,
                    "The run exceeded its overall time limit before completion.", steps, verified,
                )
            # Session startup and cleanup errors may contain internal details; never forward them.
            return self._response(
                run_id,
                TestOutcome.INCONCLUSIVE,
                "The browser session could not be completed safely.",
                steps,
                verified,
            )
        return self._response(
            run_id, TestOutcome.INCONCLUSIVE, "The run did not produce a result.", steps, verified
        )

    async def _run_session(
        self,
        request: TestExecutionRequest,
        session: BrowserSession,
        run_id: Any,
        steps: list[ExecutionStepResult],
        verified: list[VerifiedAssertion],
    ) -> TestExecutionResponse:
        page = session.page
        try:
            snapshot = await self.inspector(page)
        except Exception:
            return self._response(
                run_id, TestOutcome.INCONCLUSIVE, "The initial page could not be inspected.", steps, verified
            )

        executor = self.executor_factory(
            page, max_actions=self.max_total_actions
        )
        assertions: list[Action] = []
        seen_assertions: set[str] = set()
        assertions_truncated = False
        replan_count = 0
        checkpoint = ""
        action_occurrences: list[_ActionOccurrence] = []
        inspection_unresolved = False
        planned_action_slots = 0
        planning_failed = False
        plan_interrupted = False
        stop_replanning_after_fill_failure = False

        while True:
            try:
                instruction = request.instruction
                if checkpoint:
                    instruction = f"{instruction}\n\nExecution checkpoint: {checkpoint}"
                plan = await self.planner.plan(
                    InternalPlanningInput(
                        instruction=instruction,
                        snapshot=snapshot,
                        test_data=request.test_data,
                    )
                )
            except (PlanningError, ValueError, TypeError):
                planning_failed = True
                break
            except Exception:
                planning_failed = True
                break

            plan_interrupted = False
            all_planned_actions = _dedupe_redundant_fill_actions(
                [action for action in plan.actions if not _is_assertion(action)]
            )
            plan_occurrences = self._reconcile_action_occurrences(
                all_planned_actions, action_occurrences
            )
            remaining_slots = max(0, self.max_total_actions - planned_action_slots)
            actions_to_execute = all_planned_actions[:remaining_slots]
            overflow_actions = all_planned_actions[remaining_slots:]
            planned_action_slots += len(actions_to_execute)
            budget_overflow = False
            plan_has_postconditions = any(_is_assertion(action) for action in plan.actions)
            page_changed = False
            for action in plan.actions:
                if _is_assertion(action):
                    token = action.model_dump_json()
                    if token not in seen_assertions:
                        seen_assertions.add(token)
                        if len(assertions) < MAX_TOTAL_ASSERTIONS:
                            assertions.append(action)
                        else:
                            assertions_truncated = True

            for action_index, action in enumerate(actions_to_execute):
                occurrence = plan_occurrences[action_index]
                occurrence.attempts += 1
                attempt_number = occurrence.attempts
                try:
                    step = await executor.execute(
                        action,
                        snapshot,
                        request.test_data,
                        index=len(steps),
                    )
                    occurrence.state = "completed"
                    step = step.model_copy(update={
                        "occurrence_id": occurrence.occurrence_id,
                        "attempt_number": attempt_number,
                    })
                    self._append_step(steps, step)
                except ActionExecutionError as exc:
                    occurrence.state = "failed"
                    if (
                        isinstance(action, FillAction)
                        and exc.fill_diagnostic is not None
                        and exc.fill_diagnostic.reason_code in _TERMINAL_FILL_LOOKUP_FAILURES
                    ):
                        # Identical lookup evidence will not improve by replaying the
                        # action plan. Preserve this occurrence as unresolved and stop.
                        stop_replanning_after_fill_failure = True
                    explanation = _safe_failure_summary(exc)
                    self._append_step(steps,
                        ExecutionStepResult(
                            index=len(steps),
                            action_type=action.type,
                            status="INCONCLUSIVE",
                            explanation=explanation,
                            occurrence_id=occurrence.occurrence_id,
                            attempt_number=attempt_number,
                            fill_diagnostic=exc.fill_diagnostic,
                        )
                    )
                    for skipped_index, skipped in enumerate(
                        all_planned_actions[action_index + 1:], start=action_index + 1
                    ):
                        skipped_occurrence = plan_occurrences[skipped_index]
                        skipped_occurrence.state = "skipped"
                        self._append_step(steps, ExecutionStepResult(
                            index=len(steps), action_type=skipped.type,
                            status="SKIPPED",
                            explanation="This planned action was skipped after an earlier action failed.",
                            occurrence_id=skipped_occurrence.occurrence_id,
                            attempt_number=None,
                        ))
                    plan_interrupted = True
                    break

                try:
                    updated_snapshot = await self.inspector(page)
                except Exception:
                    self._append_step(steps,
                        ExecutionStepResult(
                            index=len(steps),
                            action_type="inspect",
                            status="INCONCLUSIVE",
                            explanation="The page could not be inspected after an action.",
                        )
                    )
                    inspection_unresolved = True
                    for skipped_index, skipped in enumerate(
                        all_planned_actions[action_index + 1:], start=action_index + 1
                    ):
                        skipped_occurrence = plan_occurrences[skipped_index]
                        skipped_occurrence.state = "skipped"
                        self._append_step(steps, ExecutionStepResult(
                            index=len(steps), action_type=skipped.type,
                            status="SKIPPED",
                            explanation="This planned action was skipped because page inspection failed.",
                            occurrence_id=skipped_occurrence.occurrence_id,
                            attempt_number=None,
                        ))
                    plan_interrupted = True
                    break
                if updated_snapshot.model_dump(mode="json") != snapshot.model_dump(mode="json"):
                    page_changed = True
                    snapshot = updated_snapshot
                    checkpoint = (
                        f"{executor.executed_actions} actions have completed. "
                        "The page was reinspected after the prior action. Continue this plan "
                        "only if each remaining target still resolves uniquely in the current state."
                    )
                else:
                    snapshot = updated_snapshot
            else:
                if overflow_actions:
                    for overflow_index, skipped in enumerate(overflow_actions, start=len(actions_to_execute)):
                        skipped_occurrence = plan_occurrences[overflow_index]
                        skipped_occurrence.state = "skipped"
                        self._append_step(steps, ExecutionStepResult(
                            index=len(steps), action_type=skipped.type,
                            status="SKIPPED",
                            explanation="The run reached its total planned-action limit.",
                            occurrence_id=skipped_occurrence.occurrence_id,
                            attempt_number=None,
                        ))
                    plan_interrupted = True
                    budget_overflow = True
                if budget_overflow:
                    break
                # Every executable action in this plan was completed. Replan
                # once after state changes so the planner can inspect the new page.
                if page_changed and not plan_has_postconditions and replan_count < self.max_replans:
                    replan_count += 1
                    continue
                break

            if budget_overflow:
                break
            if plan_interrupted:
                if stop_replanning_after_fill_failure:
                    break
                if replan_count >= self.max_replans:
                    break
                replan_count += 1
                checkpoint = (
                    f"{executor.executed_actions} actions have completed. The active plan was "
                    "interrupted. Retry failed actions explicitly and address every skipped planned "
                    "action; unrelated successful actions do not count as recovery."
                )
                continue
            break

        try:
            final_snapshot = await self.inspector(page)
        except Exception:
            final_snapshot = None

        if final_snapshot is None:
            for index, action in enumerate(assertions):
                verified.append(
                    VerifiedAssertion(
                        index=index,
                        action_type=action.type,
                        required=action.required,
                        status="INCONCLUSIVE",
                        reason_code="verification_error",
                        explanation="The final page state could not be inspected.",
                    )
                )
            return self._response(
                run_id, TestOutcome.INCONCLUSIVE, "The final page state could not be verified.", steps, verified
            )

        unresolved_occurrences = [
            occurrence for occurrence in action_occurrences
            if occurrence.state != "completed"
        ]
        obligations_resolved = not unresolved_occurrences and not inspection_unresolved
        if not obligations_resolved:
            for index, action in enumerate(assertions):
                self._append_assertion(verified, VerifiedAssertion(
                    index=index, action_type=action.type, required=action.required,
                    status="INCONCLUSIVE",
                    reason_code="verification_deferred",
                    explanation="Final postconditions were not checked because planned actions remain unresolved.",
                ))
        else:
            verifier = AssertionVerifier(page)
            for index, action in enumerate(assertions):
                self._append_assertion(
                    verified, await verifier.verify(action, final_snapshot, index=index)
                )

        required = [assertion for assertion in verified if assertion.required]
        if not required:
            outcome = TestOutcome.INCONCLUSIVE
            if unresolved_occurrences or inspection_unresolved:
                summary = self._unresolved_summary(
                    len(unresolved_occurrences), inspection_unresolved,
                    "No required assertion was available.",
                )
            elif planning_failed:
                summary = "The planner could not produce a valid action plan."
            else:
                summary = "The planner produced no required assertions to verify."
        elif any(assertion.status == "FAIL" for assertion in required):
            outcome = TestOutcome.FAIL
            summary = "At least one required assertion was not satisfied."
        elif unresolved_occurrences or inspection_unresolved:
            outcome = TestOutcome.INCONCLUSIVE
            summary = self._unresolved_summary(
                len(unresolved_occurrences), inspection_unresolved,
                "The test cannot be confirmed.",
            )
        elif assertions_truncated:
            outcome = TestOutcome.INCONCLUSIVE
            summary = "The run reached its assertion verification limit."
        elif all(assertion.status == "PASS" for assertion in required):
            outcome = TestOutcome.PASS
            summary = "All required assertions were independently verified."
        else:
            outcome = TestOutcome.INCONCLUSIVE
            summary = "One or more required assertions could not be verified reliably."
        return self._response(run_id, outcome, summary, steps, verified)

    @staticmethod
    def _reconcile_action_occurrences(
        actions: list[Action], occurrences: list[_ActionOccurrence]
    ) -> list[_ActionOccurrence]:
        """Match replanned actions one-to-one to the oldest identical unresolved occurrence.

        The ordered occurrence ledger, not an action-content counter, is the
        completion source of truth. Repeated actions consume distinct entries.
        """

        assigned: list[_ActionOccurrence] = []
        claimed: set[str] = set()
        for action in actions:
            token = action.model_dump_json()
            # An explicit replan of a failed action is preferred over a skipped
            # duplicate; remaining identical occurrences are matched oldest first.
            match = None
            for state in ("failed", "skipped"):
                match = next((
                    occurrence for occurrence in occurrences
                    if occurrence.occurrence_id not in claimed
                    and occurrence.action_token == token
                    and occurrence.state == state
                ), None)
                if match is not None:
                    break
            if match is None:
                match = _ActionOccurrence(
                    occurrence_id=f"action-{len(occurrences) + 1:03d}",
                    action_token=token,
                )
                occurrences.append(match)
            claimed.add(match.occurrence_id)
            assigned.append(match)
        return assigned

    @staticmethod
    def _unresolved_summary(
        count: int, inspection_unresolved: bool, suffix: str
    ) -> str:
        if count:
            noun = "occurrence" if count == 1 else "occurrences"
            verb = "remains" if count == 1 else "remain"
            inspection = " A page inspection also remains unresolved." if inspection_unresolved else ""
            return f"{count} planned action {noun} {verb} unresolved.{inspection} {suffix}"
        return f"A page inspection remains unresolved. {suffix}"

    @staticmethod
    def _append_step(steps: list[ExecutionStepResult], step: ExecutionStepResult) -> None:
        if len(steps) < MAX_EXECUTION_STEPS:
            steps.append(step)

    @staticmethod
    def _append_assertion(
        assertions: list[VerifiedAssertion], assertion: VerifiedAssertion
    ) -> None:
        if len(assertions) < MAX_TOTAL_ASSERTIONS:
            assertions.append(assertion)

    @staticmethod
    def _response(
        run_id: Any,
        outcome: TestOutcome,
        summary: str,
        steps: list[ExecutionStepResult],
        assertions: list[VerifiedAssertion],
    ) -> TestExecutionResponse:
        return TestExecutionResponse(
            run_id=run_id,
            status="COMPLETED",
            outcome=outcome,
            summary=summary,
            steps=list(steps),
            verified_assertions=list(assertions),
        )
