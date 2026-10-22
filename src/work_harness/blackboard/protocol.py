"""Commands and read views for the blackboard, not additional knowledge kinds."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, ConfigDict, Field, model_validator

from work_harness.blackboard.models import (
    AcceptanceCriterion,
    BoardModel,
    BoardObject,
    ClaimType,
    ContentRef,
    Digest,
    NonEmpty,
    ObjectKind,
    ObjectProposal,
    ObjectRef,
)


class FactQuery(BoardModel):
    claim_key: NonEmpty
    claim_type: ClaimType | None = None
    scope: dict[str, str] = Field(default_factory=dict)


class ReadSelector(BoardModel):
    """The same bounded branch/topic projection that was supplied to a source."""

    intent_id: NonEmpty
    input_topics: tuple[NonEmpty, ...] = ()
    accepted_kinds: tuple[ObjectKind, ...] = ("fact", "hint", "intent")


class FactQueryPrecondition(BoardModel):
    kind: Literal["fact_query"] = "fact_query"
    selector: ReadSelector
    query: FactQuery
    exists: bool


class HintQueryPrecondition(BoardModel):
    kind: Literal["hint_query"] = "hint_query"
    selector: ReadSelector
    topic: NonEmpty
    exists: bool


class ViewPrecondition(BoardModel):
    kind: Literal["view"] = "view"
    selector: ReadSelector
    expected_refs: tuple[ObjectRef, ...] = Field(max_length=10000)


class IntentPrecondition(BoardModel):
    kind: Literal["intent_open"] = "intent_open"
    intent_id: NonEmpty
    allow_blocked: bool = False


QueryPrecondition = Annotated[
    FactQueryPrecondition | HintQueryPrecondition | ViewPrecondition | IntentPrecondition,
    Field(discriminator="kind"),
]


class ObjectChange(BoardModel):
    target: ObjectRef
    action: Literal[
        "consume_hint",
        "dismiss_hint",
        "block_intent",
        "reopen_intent",
        "abandon_intent",
        "retract_fact",
    ]
    reason: str = Field(min_length=1, max_length=4000)


class GoalRevision(BoardModel):
    target: ObjectRef
    objective: str = Field(min_length=1, max_length=20000)
    acceptance: tuple[AcceptanceCriterion, ...] = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=4000)


class BlackboardDelta(BoardModel):
    expected_revision: int = Field(ge=0, strict=True)
    idempotency_key: NonEmpty
    proposals: tuple[ObjectProposal, ...] = Field(default=(), max_length=100)
    changes: tuple[ObjectChange, ...] = Field(default=(), max_length=100)
    read_set: tuple[ObjectRef, ...] = Field(default=(), max_length=10000)
    concurrency: Literal["snapshot", "inputs"] = "snapshot"
    preconditions: tuple[QueryPrecondition, ...] = Field(default=(), max_length=256)
    goal_revision: GoalRevision | None = None

    @model_validator(mode="after")
    def unique_ids(self) -> BlackboardDelta:
        ids = [item.local_id for item in self.proposals]
        if len(ids) != len(set(ids)):
            raise ValueError("proposal local ids must be unique")
        if any(item.startswith("local:") for item in ids):
            raise ValueError("local ids must omit the local: reference prefix")
        targets = [item.target.object_id for item in self.changes]
        if len(targets) != len(set(targets)):
            raise ValueError("one state change per object per commit")
        read_ids = [ref.object_id for ref in self.read_set]
        if len(read_ids) != len(set(read_ids)):
            raise ValueError("read set must contain one version per object")
        if self.concurrency == "inputs" and not self.read_set and not self.preconditions:
            raise ValueError("input concurrency requires an explicit read condition")
        return self


class CommitContext(BoardModel):
    """Assigned by the host. Never deserialize this from model output."""

    source_id: NonEmpty
    activation_id: NonEmpty | None = None
    allowed_kinds: tuple[ObjectKind, ...] = ("fact", "intent", "hint")
    can_retract_facts: bool = False
    can_supersede_facts: bool = False
    is_user: bool = False
    max_objects: int = Field(default=10000, ge=1, strict=True)
    max_child_intents: int = Field(default=64, ge=1, strict=True)


class StoredContent(BoardModel):
    # Attachment bytes must agree with their digest, including surrounding whitespace.
    model_config = ConfigDict(str_strip_whitespace=False)

    ref: ContentRef
    body: str = Field(max_length=2_000_000)


TerminalStatus = Literal["succeeded", "partial", "failed", "cancelled"]


class WriterToken(BoardModel):
    board_id: NonEmpty
    owner_id: NonEmpty
    session_id: NonEmpty
    generation: int = Field(ge=1, strict=True)


class BoardFinalization(BoardModel):
    board_id: NonEmpty
    status: TerminalStatus
    reason: NonEmpty
    finalized_at_revision: int = Field(ge=1, strict=True)
    finalized_at: AwareDatetime
    output: str
    witnesses: tuple[ObjectRef, ...] = ()
    missing_criteria: tuple[NonEmpty, ...] = ()
    attempted_directions: tuple[NonEmpty, ...] = ()


class BoardControl(BoardModel):
    board_id: NonEmpty
    generation: int = Field(default=0, ge=0, strict=True)
    owner_id: NonEmpty | None = None
    session_id: NonEmpty | None = None
    cancelled: bool = False
    finalization: BoardFinalization | None = None
    wakeup_revision: int = Field(default=0, ge=0, strict=True)
    status_pending: bool = False


class BoardSnapshot(BoardModel):
    board_id: NonEmpty
    revision: int = Field(ge=1, strict=True)
    root_intent_id: NonEmpty
    objects: tuple[BoardObject, ...]
    finalization: BoardFinalization | None = None

    def by_id(self) -> dict[str, BoardObject]:
        return {item.id: item for item in self.objects}


class CommitReceipt(BoardModel):
    board_id: NonEmpty
    idempotency_key: NonEmpty
    request_digest: Digest
    board_revision: int = Field(ge=1, strict=True)
    object_refs: tuple[ObjectRef, ...]
    local_refs: dict[str, ObjectRef] = Field(default_factory=dict)
    committed_at: AwareDatetime


class BoardEvent(BoardModel):
    board_id: NonEmpty
    sequence: int = Field(ge=1, strict=True)
    source_id: NonEmpty
    activation_id: NonEmpty | None = None
    committed_at: AwareDatetime
    changed_refs: tuple[ObjectRef, ...]
    requested_changes: tuple[ObjectChange, ...] = ()
    event_type: Literal["knowledge", "finalized"] = "knowledge"
    finalization: BoardFinalization | None = None
    goal_revision: GoalRevision | None = None


class EventWindow(BoardModel):
    """An atomic, bounded read of the durable log and its high-water mark."""

    board_id: NonEmpty
    revision: int = Field(ge=1, strict=True)
    first_sequence: int | None
    events: tuple[BoardEvent, ...]
    finalization: BoardFinalization | None = None


class ObjectRevision(BoardModel):
    object: BoardObject
    event: BoardEvent


class BlackboardError(ValueError):
    """A rejected command; no partial board changes are committed."""


class BoardNotFoundError(BlackboardError):
    pass


class InputPendingError(BlackboardError):
    """A newer accepted input must be assessed before sealing the board."""


class HistoryUnavailableError(BlackboardError):
    """A requested historical read cannot be reconstructed without missing records."""


class StaleSnapshotError(BlackboardError):
    pass


class IdempotencyConflictError(BlackboardError):
    pass


class VerificationError(BlackboardError):
    pass


class StaleWriterError(BlackboardError):
    pass


class BoardClosedError(BlackboardError):
    pass


class GoalNotSatisfiedError(BlackboardError):
    pass


class BlackboardProcessLockError(BlackboardError):
    pass
