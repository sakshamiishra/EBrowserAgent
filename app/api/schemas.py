"""Validated API and provider-independent action plan schemas."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Union
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    field_validator,
    model_validator,
)


RequestInstruction = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4_000)
]
TestDataKey = Annotated[str, StringConstraints(min_length=1, max_length=100)]
TestDataValue = Annotated[str, StringConstraints(max_length=1_000)]
MAX_PLAN_GOAL_LENGTH = 500
MAX_TARGET_VALUE_LENGTH = 1_000
MAX_TARGET_ROLE_LENGTH = 100
MAX_FILL_VALUE_LENGTH = 4_096
MAX_VALUE_REFERENCE_LENGTH = 100
MAX_SELECT_OPTION_LENGTH = 500
MAX_ASSERT_TEXT_LENGTH = 2_000
MAX_EXECUTION_STEPS = 250

PlanGoal = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_PLAN_GOAL_LENGTH)
]
TargetDescription = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_TARGET_VALUE_LENGTH)
]
TargetRole = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_TARGET_ROLE_LENGTH)
]
FillValue = Annotated[str, StringConstraints(max_length=MAX_FILL_VALUE_LENGTH)]
ValueReference = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_VALUE_REFERENCE_LENGTH)
]
SelectOption = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_SELECT_OPTION_LENGTH)
]
ExpectedText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_ASSERT_TEXT_LENGTH)
]


class StrictSchema(BaseModel):
    """Base model that rejects fields not explicitly declared in the schema."""

    model_config = ConfigDict(extra="forbid")


class TestExecutionRequest(StrictSchema):
    """Input supplied by a client requesting a browser test run."""

    target_url: HttpUrl
    instruction: RequestInstruction
    test_data: dict[TestDataKey, TestDataValue] = Field(default_factory=dict, max_length=20)

    @field_validator("test_data")
    @classmethod
    def validate_test_data(cls, values: dict[str, str]) -> dict[str, str]:
        if any(not key.strip() for key in values):
            raise ValueError("test-data keys must not be blank")
        if sum(len(key) + len(value) for key, value in values.items()) > 10_000:
            raise ValueError("test data exceeds the supported combined size")
        return values


class ExecutionStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"


class TestOutcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


class ElementTarget(StrictSchema):
    """A semantic or narrowly scoped description of a page element."""

    kind: Literal["role", "label", "placeholder", "text", "attribute", "css"]
    value: TargetDescription
    role: TargetRole | None = None

    @model_validator(mode="after")
    def require_role_for_role_target(self) -> ElementTarget:
        if self.kind == "role" and self.role is None:
            raise ValueError("role targets must include the accessible role")
        if self.kind != "role" and self.role is not None:
            raise ValueError("role is only valid for role targets")
        return self


class NavigateAction(StrictSchema):
    type: Literal["navigate"]
    url: HttpUrl


class ClickAction(StrictSchema):
    type: Literal["click"]
    target: ElementTarget


class FillAction(StrictSchema):
    type: Literal["fill"]
    target: ElementTarget
    value: FillValue | None = None
    value_ref: ValueReference | None = None

    @model_validator(mode="after")
    def require_one_value_source(self) -> FillAction:
        if (self.value is None) == (self.value_ref is None):
            raise ValueError("provide exactly one of value or value_ref")
        return self


class SelectAction(StrictSchema):
    type: Literal["select"]
    target: ElementTarget
    option: SelectOption


class WaitAction(StrictSchema):
    type: Literal["wait"]
    milliseconds: int = Field(ge=0, le=30_000)


class AssertVisibleAction(StrictSchema):
    type: Literal["assert_visible"]
    target: ElementTarget
    required: bool = True


class AssertTextAction(StrictSchema):
    type: Literal["assert_text"]
    expected_text: ExpectedText
    target: ElementTarget | None = None
    required: bool = True


class AssertUrlAction(StrictSchema):
    type: Literal["assert_url"]
    expected_url: HttpUrl
    exact: bool = True
    required: bool = True


Action = Annotated[
    Union[
        NavigateAction,
        ClickAction,
        FillAction,
        SelectAction,
        WaitAction,
        AssertVisibleAction,
        AssertTextAction,
        AssertUrlAction,
    ],
    Field(discriminator="type"),
]


class StructuredActionPlan(StrictSchema):
    """Provider-independent plan; assertions are final postconditions."""

    version: Literal[1] = 1
    goal: PlanGoal
    actions: list[Action] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def assertions_are_final_postconditions(self) -> StructuredActionPlan:
        seen_assertion = False
        for action in self.actions:
            is_assertion = isinstance(
                action, (AssertVisibleAction, AssertTextAction, AssertUrlAction)
            )
            if seen_assertion and not is_assertion:
                raise ValueError("assertions must follow all planned browser actions")
            seen_assertion = seen_assertion or is_assertion
        return self


class FillResolutionDiagnostic(StrictSchema):
    """Allow-listed, value-free details about resolving one fill target."""

    target_kind: Literal["role", "label", "placeholder", "text", "attribute", "css"]
    target_role: Literal[
        "button", "link", "textbox", "searchbox", "combobox", "checkbox", "radio",
        "option", "menuitem", "tab", "switch", "slider", "spinbutton", "heading",
        "img", "listbox", "other",
    ] | None = None
    locator_strategy: Literal[
        "get_by_role", "get_by_label", "get_by_placeholder", "get_by_text",
        "allowlisted_attribute", "restricted_css",
    ]
    snapshot_match: Literal["matched", "missing"] | None = None
    snapshot_truncated: bool = False
    candidate_count: int | None = Field(default=None, ge=0, le=100)
    visible_count: int | None = Field(default=None, ge=0, le=100)
    reason_code: Literal[
        "resolved", "target_not_in_snapshot", "live_no_match", "live_not_visible",
        "live_ambiguous", "role_or_name_mismatch", "locator_mismatch",
        "locator_error", "target_not_interactable", "action_error",
    ]


class ExecutionStepResult(StrictSchema):
    """A concise result for one planned or executed step."""

    index: int = Field(ge=0)
    action_type: Annotated[str, StringConstraints(min_length=1, max_length=50)]
    status: Literal["PENDING", "PASS", "FAIL", "INCONCLUSIVE", "SKIPPED"]
    explanation: Annotated[str, StringConstraints(max_length=300)]
    occurrence_id: Annotated[str, StringConstraints(min_length=1, max_length=40)] | None = None
    attempt_number: int | None = Field(default=None, ge=1)
    fill_diagnostic: FillResolutionDiagnostic | None = None


class VerifiedAssertion(StrictSchema):
    """Safe assertion evidence; expected values are deliberately omitted."""

    index: int = Field(ge=0)
    action_type: Literal["assert_visible", "assert_text", "assert_url"]
    required: bool
    status: Literal["PASS", "FAIL", "INCONCLUSIVE"]
    reason_code: Literal[
        "verified",
        "not_found_in_complete_snapshot",
        "snapshot_truncated",
        "live_no_match",
        "live_not_visible",
        "live_ambiguous",
        "condition_not_met",
        "verification_error",
        "verification_deferred",
    ]
    target_kind: Literal["role", "label", "placeholder", "text", "attribute", "css"] | None = None
    snapshot_match: Literal["matched", "missing"] | None = None
    snapshot_truncated: bool = False
    candidate_count: int | None = Field(default=None, ge=0, le=100)
    visible_count: int | None = Field(default=None, ge=0, le=100)
    explanation: Annotated[str, StringConstraints(max_length=300)]


class TestExecutionResponse(StrictSchema):
    """Run status and eventual outcome returned by the API."""

    run_id: UUID
    status: ExecutionStatus
    outcome: TestOutcome | None = None
    summary: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    steps: list[ExecutionStepResult] = Field(
        default_factory=list, max_length=MAX_EXECUTION_STEPS
    )
    verified_assertions: list[VerifiedAssertion] = Field(default_factory=list, max_length=50)
