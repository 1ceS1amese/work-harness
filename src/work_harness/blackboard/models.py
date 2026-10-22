"""Knowledge contracts. Execution records are deliberately outside this module."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    model_validator,
)

ObjectKind = Literal["fact", "intent", "hint"]
ClaimType = Literal[
    "user_statement",
    "file_content",
    "tool_observation",
    "derived",
    "assessment",
    "delivery",
]
FactLifecycle = Literal["valid", "contested", "stale", "superseded", "expired", "retracted"]
IntentStatus = Literal["open", "satisfied", "blocked", "abandoned"]
HintStatus = Literal["active", "consumed", "dismissed", "expired"]
NonEmpty = Annotated[str, Field(min_length=1, max_length=500)]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class BoardModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        allow_inf_nan=False,
        revalidate_instances="always",
    )


def digest(value: JsonValue | BaseModel) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


class ObjectRef(BoardModel):
    # A local:<name> reference addresses an object in the same proposed commit.
    object_id: NonEmpty
    revision: int = Field(ge=1, strict=True)


class Link(BoardModel):
    relation: Literal["contributes_to", "refines", "supports", "contradicts", "supersedes"]
    target: ObjectRef


class Evidence(BoardModel):
    source: Literal["user_input", "file", "tool", "rule", "assessment", "delivery"]
    locator: NonEmpty
    content_digest: Digest
    description: str = Field(min_length=1, max_length=4000)


class ContentRef(BoardModel):
    content_digest: Digest
    media_type: Literal["text/plain", "text/markdown", "application/json"] = "text/plain"
    size_bytes: int = Field(ge=0, le=2_000_000, strict=True)


class AcceptanceCriterion(BoardModel):
    id: NonEmpty
    description: str = Field(min_length=1, max_length=4000)
    claim_key: NonEmpty
    claim_type: ClaimType
    scope: dict[str, str] = Field(default_factory=dict)
    operator: Literal["exists", "equals", "at_least", "at_most"] = "exists"
    expected: JsonValue = None
    required_checks: tuple[NonEmpty, ...] = ()

    @model_validator(mode="after")
    def validate_comparison(self) -> AcceptanceCriterion:
        if self.operator in {"at_least", "at_most"} and (
            isinstance(self.expected, bool) or not isinstance(self.expected, (int, float))
        ):
            raise ValueError("numeric acceptance requires a numeric expected value")
        if self.operator == "exists" and self.expected is not None:
            raise ValueError("exists cannot have an expected value")
        return self


class KnowledgeContent(BoardModel):
    scope: dict[str, str] = Field(default_factory=dict)
    topic: NonEmpty
    tags: tuple[NonEmpty, ...] = ()
    derived_from: tuple[ObjectRef, ...] = ()
    links: tuple[Link, ...] = ()
    attachments: tuple[ContentRef, ...] = ()


class FactContent(KnowledgeContent):
    kind: Literal["fact"] = "fact"
    statement: str = Field(min_length=1, max_length=20000)
    claim_type: ClaimType
    claim_key: NonEmpty | None = None
    value: JsonValue = None
    evidence: tuple[Evidence, ...] = Field(min_length=1, max_length=100)
    premise_refs: tuple[ObjectRef, ...] = ()
    valid_from: AwareDatetime | None = None
    valid_until: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_interval(self) -> FactContent:
        if self.valid_from and self.valid_until and self.valid_until <= self.valid_from:
            raise ValueError("fact validity interval must be nonempty")
        return self


class IntentContent(KnowledgeContent):
    kind: Literal["intent"] = "intent"
    objective: str = Field(min_length=1, max_length=20000)
    root_intent_ref: ObjectRef | None = None
    parent_intent_ref: ObjectRef | None = None
    acceptance: tuple[AcceptanceCriterion, ...] = Field(min_length=1, max_length=100)
    priority: int = Field(default=0, ge=0, le=100, strict=True)

    @model_validator(mode="after")
    def validate_criteria(self) -> IntentContent:
        ids = [item.id for item in self.acceptance]
        if len(ids) != len(set(ids)):
            raise ValueError("acceptance criterion ids must be unique")
        return self


class HintContent(KnowledgeContent):
    kind: Literal["hint"] = "hint"
    suggestion: str = Field(min_length=1, max_length=20000)
    applies_when: str = Field(min_length=1, max_length=4000)
    rationale: str = Field(min_length=1, max_length=4000)
    source_refs: tuple[ObjectRef, ...] = ()
    confidence: float | None = Field(default=None, ge=0, le=1)
    expires_at: AwareDatetime | None = None


class ObjectMetadata(BoardModel):
    id: NonEmpty
    board_id: NonEmpty
    schema_version: Literal[1] = 1
    revision: int = Field(ge=1, strict=True)
    created_at: AwareDatetime
    created_by: NonEmpty
    origin_activation_id: NonEmpty | None = None
    content_digest: Digest

    @property
    def ref(self) -> ObjectRef:
        return ObjectRef(object_id=self.id, revision=self.revision)


class Verification(BoardModel):
    receipt_id: NonEmpty
    verifier_id: NonEmpty
    verifier_version: NonEmpty
    method: NonEmpty
    verified_at: AwareDatetime
    content_digest: Digest
    checks: tuple[NonEmpty, ...] = ()
    premise_refs: tuple[ObjectRef, ...] = ()


class Fact(ObjectMetadata, FactContent):
    verification: Verification
    lifecycle: FactLifecycle = "valid"


class Intent(ObjectMetadata, IntentContent):
    status: IntentStatus = "open"
    satisfied_by: tuple[ObjectRef, ...] = ()
    blockers: tuple[ObjectRef, ...] = ()
    status_reason: str = ""


class Hint(ObjectMetadata, HintContent):
    status: HintStatus = "active"
    status_reason: str = ""


BoardObject = Annotated[Fact | Intent | Hint, Field(discriminator="kind")]


class FactProposal(FactContent):
    local_id: NonEmpty
    verification_receipt_id: NonEmpty


class IntentProposal(IntentContent):
    local_id: NonEmpty


class HintProposal(HintContent):
    local_id: NonEmpty


ObjectProposal = Annotated[
    FactProposal | IntentProposal | HintProposal, Field(discriminator="kind")
]


def content_of(item: BoardObject | ObjectProposal) -> KnowledgeContent:
    cls = {"fact": FactContent, "intent": IntentContent, "hint": HintContent}[item.kind]
    return cls.model_validate(item.model_dump(include=set(cls.model_fields)))
