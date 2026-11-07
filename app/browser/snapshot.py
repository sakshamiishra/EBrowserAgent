"""Provider-independent page snapshot models."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from app.api.schemas import StrictSchema


ElementKind = Literal["button", "link", "input", "select", "textarea", "custom"]
MAX_SNAPSHOT_URL_LENGTH = 2_048
ShortSnapshotText = Annotated[str, StringConstraints(max_length=160)]


class InteractiveElementSnapshot(StrictSchema):
    kind: ElementKind
    role: ShortSnapshotText | None = None
    accessible_name: ShortSnapshotText | None = None
    label: ShortSnapshotText | None = None
    aria_label: ShortSnapshotText | None = None
    placeholder: ShortSnapshotText | None = None
    name: ShortSnapshotText | None = None
    test_id: ShortSnapshotText | None = None
    text: ShortSnapshotText | None = None
    input_type: ShortSnapshotText | None = None


class PageSnapshot(StrictSchema):
    url: Annotated[str, StringConstraints(max_length=MAX_SNAPSHOT_URL_LENGTH)]
    title: ShortSnapshotText
    visible_text: Annotated[str, StringConstraints(max_length=5_000)]
    visible_text_truncated: bool = False
    elements: list[InteractiveElementSnapshot] = Field(default_factory=list, max_length=100)
    elements_truncated: bool = False
    truncated: bool = False
