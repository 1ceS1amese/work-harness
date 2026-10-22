"""Public source contracts and private invocation input/output."""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import AliasChoices, AwareDatetime, Field, model_validator

from work_harness.blackboard.models import (
    BoardModel,
    BoardObject,
    Intent,
    NonEmpty,
    ObjectKind,
    ObjectProposal,
    ObjectRef,
)
from work_harness.blackboard.protocol import FactQuery as FactQuery
from work_harness.blackboard.protocol import ObjectChange


class ActivationRule(BoardModel):
    """A finite, deterministic matching rule; no LLM calls or executable expressions."""

    intent_topics: tuple[NonEmpty, ...] = ()
    required_facts: tuple[FactQuery, ...] = ()
    absent_facts: tuple[FactQuery, ...] = ()
    hint_topics: tuple[NonEmpty, ...] = ()
    absent_hint_topics: tuple[NonEmpty, ...] = ()
    accept_blocked: bool = False
    reason: str = Field(min_length=1, max_length=2000)


class SourceLimits(BoardModel):
    max_output_tokens: int = Field(
        default=4096,
        ge=0,
        strict=True,
        validation_alias=AliasChoices("max_output_tokens", "token_budget"),
        description="Maximum output tokens per model call; never a cumulative allowance.",
    )
    cost_budget_usd: float = Field(default=0, ge=0, deprecated=True)
    timeout_seconds: float = Field(default=30, gt=0, le=3600)
    max_attempts: int = Field(default=1, ge=1, le=5, strict=True)


class KnowledgeSourceSpec(BoardModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    version: NonEmpty
    input_topics: tuple[NonEmpty, ...] = ()
    accepted_kinds: tuple[ObjectKind, ...] = ("fact", "hint", "intent")
    emitted_kinds: tuple[ObjectKind, ...]
    activation_rule: ActivationRule
    allowed_tools: tuple[NonEmpty, ...] = ()
    model_route: NonEmpty | None = None
    limits: SourceLimits = Field(default_factory=SourceLimits)
    adapter_type: Literal["observation", "agent", "verification", "rule"]
    strategy_version: NonEmpty = "1"
    can_retract_facts: bool = False
    can_supersede_facts: bool = False

    @model_validator(mode="after")
    def verification_authority(self) -> KnowledgeSourceSpec:
        if (
            self.can_retract_facts or self.can_supersede_facts
        ) and self.adapter_type != "verification":
            raise ValueError("fact lifecycle authority requires a verification adapter")
        return self


class BoardChangeSet(BoardModel):
    from_revision: int = Field(ge=0)
    to_revision: int = Field(ge=1)
    changed_ids: tuple[NonEmpty, ...] = ()


class BoardView(BoardModel):
    board_id: NonEmpty
    board_revision: int = Field(ge=1)
    root_intent: Intent
    intent: Intent
    objects: tuple[BoardObject, ...]
    evaluated_at: AwareDatetime | None = None

    def by_id(self) -> dict[str, BoardObject]:
        return {item.id: item for item in self.objects}


class ActivationCandidate(BoardModel):
    id: NonEmpty
    board_id: NonEmpty
    source_id: NonEmpty
    source_version: NonEmpty
    intent_ref: ObjectRef
    input_refs: tuple[ObjectRef, ...]
    input_fingerprint: NonEmpty
    strategy_version: NonEmpty
    reason: str
    conflict: bool = False
    intent_priority: int


class UsageRecord(BoardModel):
    call_id: NonEmpty
    input_tokens: int = Field(default=0, ge=0, strict=True)
    output_tokens: int = Field(default=0, ge=0, strict=True)
    cached_tokens: int = Field(default=0, ge=0, strict=True)
    usage_complete: bool = True
    cost_usd: float = Field(default=0, ge=0)

    @model_validator(mode="after")
    def cache_is_part_of_input(self) -> UsageRecord:
        if self.cached_tokens > self.input_tokens:
            raise ValueError("cached tokens must be included in input tokens")
        return self

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class KnowledgeOutput(BoardModel):
    proposals: tuple[ObjectProposal, ...] = ()
    changes: tuple[ObjectChange, ...] = ()
    status: Literal["completed", "no_progress", "failed", "retryable_error"] = "completed"
    reason: str = ""
    usage_complete: bool = True


class KnowledgeSource(Protocol):
    spec: KnowledgeSourceSpec

    def match(self, view: BoardView, changes: BoardChangeSet) -> ActivationCandidate | None: ...

    def build(self, execution: Any) -> Any:
        """Return a compiled, per-invocation subgraph with the common reference-only output."""
        ...
