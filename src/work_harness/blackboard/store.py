"""Atomic persistence boundary for accepted blackboard commits."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from work_harness.blackboard.models import BoardObject, ContentRef, Intent, ObjectRef, digest
from work_harness.blackboard.protocol import (
    BlackboardDelta,
    BlackboardError,
    BoardControl,
    BoardEvent,
    BoardFinalization,
    BoardSnapshot,
    CommitContext,
    CommitReceipt,
    EventWindow,
    ObjectRevision,
    StoredContent,
    TerminalStatus,
    WriterToken,
)


@dataclass(frozen=True)
class Publication:
    snapshot: BoardSnapshot
    event: BoardEvent
    receipt: CommitReceipt


@dataclass(frozen=True)
class CommitRequest:
    """Host-only pure transition, invoked under the store's write transaction."""

    board_id: str
    idempotency_key: str
    request_digest: str
    apply: Callable[[BoardSnapshot], Publication]
    wakeup: bool = False


def commit_digest(delta: BlackboardDelta, context: CommitContext) -> str:
    return digest(
        {"delta": delta.model_dump(mode="json"), "context": context.model_dump(mode="json")}
    )


class CommitSettlement(Protocol):
    """Host-bound activation participant in the board's atomic commit."""

    def check_store(self, store: BlackboardStore) -> None: ...

    async def commit(self, request: CommitRequest, *, writer: WriterToken) -> CommitReceipt: ...


def validate_commit(request: CommitRequest, result: Publication, current: BoardSnapshot) -> None:
    if (
        result.snapshot.board_id != request.board_id
        or result.receipt.idempotency_key != request.idempotency_key
        or result.receipt.request_digest != request.request_digest
    ):
        raise BlackboardError("publication does not match its commit request")
    validate_publication(result.snapshot, result.event, previous=current, receipt=result.receipt)


class BlackboardStore(Protocol):
    async def initialize(self) -> None: ...

    async def close(self) -> None: ...

    async def put_content(
        self, board_id: str, body: str, *, media_type: str, writer: WriterToken
    ) -> ContentRef: ...

    async def control(self, board_id: str) -> BoardControl: ...

    async def claim_writer(
        self, board_id: str, owner_id: str, *, takeover: bool = False
    ) -> WriterToken: ...

    async def check_writer(self, writer: WriterToken) -> None: ...

    async def release_writer(self, writer: WriterToken) -> None: ...

    async def pending_reconciliations(self, *, limit: int = 200) -> tuple[str, ...]: ...

    async def acknowledge_finalization(self, board_id: str, revision: int) -> None: ...

    async def acknowledge_wakeup(
        self, board_id: str, through: int, *, writer: WriterToken
    ) -> None: ...

    async def finalize(
        self,
        board_id: str,
        *,
        status: TerminalStatus,
        reason: str,
        writer: WriterToken | None,
        clock: Callable[[], datetime],
        attempted_directions: tuple[str, ...] = (),
        observed_revision: int | None = None,
    ) -> BoardFinalization:
        """Cancel may preempt the owner; other terminal writes require its current token."""
        ...

    async def content(self, board_id: str, ref: ContentRef) -> StoredContent: ...

    async def create(self, snapshot: BoardSnapshot, event: BoardEvent) -> BoardSnapshot: ...

    async def snapshot(self, board_id: str, *, revision: int | None = None) -> BoardSnapshot:
        """Read one consistent board version; omitted revision means the latest."""
        ...

    async def get_object(self, board_id: str, ref: ObjectRef) -> BoardObject: ...

    async def receipt(self, board_id: str, key: str) -> CommitReceipt | None: ...

    async def events(self, board_id: str, *, after: int = 0) -> tuple[BoardEvent, ...]: ...

    async def event_window(
        self, board_id: str, *, after: int = 0, limit: int = 100
    ) -> EventWindow: ...

    async def object_history(
        self, board_id: str, object_id: str, *, through: int, after: int = 0, limit: int = 100
    ) -> tuple[ObjectRevision, ...]:
        """Versions after an object revision, bounded by a board revision."""
        ...

    async def commit(
        self,
        request: CommitRequest,
        *,
        writer: WriterToken,
    ) -> CommitReceipt: ...


def validate_publication(
    snapshot: BoardSnapshot,
    event: BoardEvent,
    *,
    previous: BoardSnapshot | None = None,
    receipt: CommitReceipt | None = None,
) -> None:
    """Storage invariants shared by adapters; business validation stays in the kernel."""
    expected = previous.revision + 1 if previous else 1
    if (
        snapshot.finalization is not None
        or event.finalization is not None
        or event.event_type != "knowledge"
    ):
        raise BlackboardError("terminal metadata can only be written by finalization")
    if (
        snapshot.revision != expected
        or event.sequence != expected
        or event.board_id != snapshot.board_id
        or (
            receipt is not None
            and (receipt.board_id != snapshot.board_id or receipt.board_revision != expected)
        )
    ):
        raise BlackboardError("inconsistent commit revisions")
    objects = snapshot.by_id()
    if len(objects) != len(snapshot.objects) or any(
        item.board_id != snapshot.board_id for item in snapshot.objects
    ):
        raise BlackboardError("object identities must be unique and belong to this board")
    if not isinstance(objects.get(snapshot.root_intent_id), Intent):
        raise BlackboardError("board root must be an Intent")
    original = previous.by_id() if previous else {}
    if previous and (
        previous.board_id != snapshot.board_id
        or previous.root_intent_id != snapshot.root_intent_id
        or not original.keys() <= objects.keys()
    ):
        raise BlackboardError("publication cannot replace the root or remove object history")
    changed: set[ObjectRef] = set()
    for item in snapshot.objects:
        old = original.get(item.id)
        if item == old:
            continue
        if item.revision != (old.revision + 1 if old else 1) or (
            old is not None and old.kind != item.kind
        ):
            raise BlackboardError("object revisions are immutable and must advance by one")
        changed.add(item.ref)
    for refs in (event.changed_refs, *((receipt.object_refs,) if receipt else ())):
        if set(refs) != changed or len(refs) != len(changed):
            raise BlackboardError("event and receipt must describe every changed object")
    if receipt and any(
        ref.object_id not in objects or objects[ref.object_id].ref != ref
        for ref in receipt.local_refs.values()
    ):
        raise BlackboardError("receipt references must point to published object versions")
