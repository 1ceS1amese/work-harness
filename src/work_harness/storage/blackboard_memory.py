"""In-memory atomic store with historical reads and detached snapshots."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from work_harness.blackboard.control import (
    check_writer,
    claim_writer,
    close_control,
    release_writer,
    seal_snapshot,
)
from work_harness.blackboard.models import BoardObject, ContentRef, ObjectRef
from work_harness.blackboard.protocol import (
    BlackboardError,
    BoardControl,
    BoardEvent,
    BoardFinalization,
    BoardNotFoundError,
    BoardSnapshot,
    CommitReceipt,
    EventWindow,
    HistoryUnavailableError,
    IdempotencyConflictError,
    InputPendingError,
    ObjectRevision,
    StoredContent,
    TerminalStatus,
    WriterToken,
)
from work_harness.blackboard.store import CommitRequest, validate_commit, validate_publication

if TYPE_CHECKING:
    from work_harness.blackboard.execution import MemoryLedgerData


class MemoryBlackboardStore:
    def __init__(self) -> None:
        self._boards: dict[str, BoardSnapshot] = {}
        self._history: dict[tuple[str, str, int], BoardObject] = {}
        self._receipts: dict[tuple[str, str], CommitReceipt] = {}
        self._events: dict[str, list[BoardEvent]] = {}
        self._content: dict[tuple[str, str], StoredContent] = {}
        self._controls: dict[str, BoardControl] = {}
        self._session_id = uuid4().hex
        self._lock = asyncio.Lock()
        self._activation_data: MemoryLedgerData | None = None

    async def initialize(self) -> None:
        pass

    async def close(self) -> None:
        async with self._lock:
            self._session_id = uuid4().hex

    def _control(self, board_id: str) -> BoardControl:
        if board_id not in self._controls:
            raise BoardNotFoundError(f"unknown board: {board_id}")
        return self._controls[board_id]

    async def control(self, board_id: str) -> BoardControl:
        async with self._lock:
            return self._control(board_id).model_copy(deep=True)

    async def claim_writer(
        self, board_id: str, owner_id: str, *, takeover: bool = False
    ) -> WriterToken:
        async with self._lock:
            updated, token = claim_writer(
                self._control(board_id), owner_id, self._session_id, takeover=takeover
            )
            self._controls[board_id] = updated
            return token

    async def check_writer(self, writer: WriterToken) -> None:
        async with self._lock:
            check_writer(self._control(writer.board_id), writer, self._session_id)

    async def release_writer(self, writer: WriterToken) -> None:
        async with self._lock:
            self._controls[writer.board_id] = release_writer(
                self._control(writer.board_id),
                writer,
                self._session_id,
            )

    async def pending_reconciliations(self, *, limit: int = 200) -> tuple[str, ...]:
        async with self._lock:
            return tuple(
                sorted(
                    key
                    for key, control in self._controls.items()
                    if control.wakeup_revision or control.status_pending
                )[:limit]
            )

    async def acknowledge_finalization(self, board_id: str, revision: int) -> None:
        async with self._lock:
            control = self._control(board_id)
            if (
                control.finalization is None
                or control.finalization.finalized_at_revision != revision
            ):
                raise BlackboardError("finalization acknowledgment does not match the board")
            self._controls[board_id] = control.model_copy(update={"status_pending": False})

    async def acknowledge_wakeup(self, board_id: str, through: int, *, writer: WriterToken) -> None:
        async with self._lock:
            control = self._control(board_id)
            check_writer(control, writer, self._session_id)
            if control.wakeup_revision <= through:
                self._controls[board_id] = control.model_copy(update={"wakeup_revision": 0})

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
        async with self._lock:
            control = self._control(board_id)
            if control.finalization is not None:
                return control.finalization.model_copy(deep=True)
            if status != "cancelled":
                if writer is None:
                    raise BlackboardError("finalization requires a writer token")
                check_writer(control, writer, self._session_id)
                if control.wakeup_revision > (observed_revision or 0):
                    raise InputPendingError("new human input must be assessed before finalization")
            snapshot, event, final = seal_snapshot(
                self._boards[board_id],
                status,
                reason,
                clock(),
                attempted_directions,
            )
            self._boards[board_id] = snapshot
            self._controls[board_id] = close_control(control, final)
            self._events[board_id].append(event)
            return final.model_copy(deep=True)

    async def put_content(
        self, board_id: str, body: str, *, media_type: str, writer: WriterToken
    ) -> ContentRef:
        encoded = body.encode("utf-8")
        ref = ContentRef(
            content_digest=hashlib.sha256(encoded).hexdigest(),
            size_bytes=len(encoded),
            media_type=media_type,
        )
        async with self._lock:
            check_writer(self._control(board_id), writer, self._session_id)
            stored = StoredContent(ref=ref, body=body)
            key = (board_id, ref.content_digest)
            if key in self._content and self._content[key] != stored:
                raise BlackboardError("content digest already registered with different metadata")
            self._content[key] = stored
        return ref

    async def content(self, board_id: str, ref: ContentRef) -> StoredContent:
        async with self._lock:
            stored = self._content.get((board_id, ref.content_digest))
            if stored is None or stored.ref != ref:
                raise BlackboardError("content reference is missing from this board")
            return stored.model_copy(deep=True)

    async def create(self, snapshot: BoardSnapshot, event: BoardEvent) -> BoardSnapshot:
        async with self._lock:
            if snapshot.board_id in self._boards:
                raise BlackboardError("board already exists")
            validate_publication(snapshot, event)
            self._boards[snapshot.board_id] = snapshot.model_copy(deep=True)
            self._controls[snapshot.board_id] = BoardControl(board_id=snapshot.board_id)
            self._events[snapshot.board_id] = [event.model_copy(deep=True)]
            for item in snapshot.objects:
                self._history[(snapshot.board_id, item.id, item.revision)] = item.model_copy(
                    deep=True
                )
            return snapshot.model_copy(deep=True)

    async def snapshot(self, board_id: str, *, revision: int | None = None) -> BoardSnapshot:
        async with self._lock:
            return self.snapshot_in_transaction(board_id, revision=revision)

    def snapshot_in_transaction(
        self, board_id: str, *, revision: int | None = None
    ) -> BoardSnapshot:
        """Read a historical version while the caller holds _lock."""
        if board_id not in self._boards:
            raise BoardNotFoundError(f"unknown board: {board_id}")
        current = self._boards[board_id]
        if revision is None or revision == current.revision:
            return current.model_copy(deep=True)
        if not 1 <= revision <= current.revision:
            raise BlackboardError("unknown board revision")
        history = [event for event in self._events[board_id] if event.sequence <= revision]
        if [event.sequence for event in history] != list(range(1, revision + 1)):
            raise HistoryUnavailableError("snapshot history is incomplete")
        refs = {
            ref.object_id: ref
            for event in self._events[board_id]
            if event.sequence <= revision
            for ref in event.changed_refs
        }
        return BoardSnapshot(
            board_id=board_id,
            revision=revision,
            root_intent_id=current.root_intent_id,
            objects=tuple(
                self._history[(board_id, identity, refs[identity].revision)]
                for identity in sorted(refs)
            ),
            finalization=current.finalization
            if current.finalization and current.finalization.finalized_at_revision <= revision
            else None,
        ).model_copy(deep=True)

    async def get_object(self, board_id: str, ref: ObjectRef) -> BoardObject:
        async with self._lock:
            item = self._history.get((board_id, ref.object_id, ref.revision))
            if item is None:
                raise BlackboardError("reference is missing from this board")
            return item.model_copy(deep=True)

    async def receipt(self, board_id: str, key: str) -> CommitReceipt | None:
        async with self._lock:
            result = self._receipts.get((board_id, key))
            return result.model_copy(deep=True) if result else None

    async def events(self, board_id: str, *, after: int = 0) -> tuple[BoardEvent, ...]:
        async with self._lock:
            if board_id not in self._boards:
                raise BoardNotFoundError(f"unknown board: {board_id}")
            return tuple(
                item.model_copy(deep=True)
                for item in self._events[board_id]
                if item.sequence > after
            )

    async def event_window(self, board_id: str, *, after: int = 0, limit: int = 100) -> EventWindow:
        if after < 0 or not 1 <= limit <= 200:
            raise ValueError("invalid event window bounds")
        async with self._lock:
            self._control(board_id)
            board = self._boards[board_id]
            events = self._events[board_id]
            return EventWindow(
                board_id=board_id,
                revision=board.revision,
                first_sequence=events[0].sequence if events else None,
                events=tuple(event for event in events if event.sequence > after)[:limit],
                finalization=board.finalization,
            ).model_copy(deep=True)

    async def object_history(
        self, board_id: str, object_id: str, *, through: int, after: int = 0, limit: int = 100
    ) -> tuple[ObjectRevision, ...]:
        if through < 1 or after < 0 or not 1 <= limit <= 200:
            raise ValueError("invalid history bounds")
        async with self._lock:
            self._control(board_id)
            events = {
                ref.revision: event
                for event in self._events[board_id]
                if event.sequence <= through
                for ref in event.changed_refs
                if ref.object_id == object_id
            }
            selected = sorted(revision for revision in events if revision > after)[:limit]
            if any((board_id, object_id, revision) not in self._history for revision in selected):
                raise HistoryUnavailableError("object revision is unavailable")
            return tuple(
                ObjectRevision(
                    object=self._history[(board_id, object_id, revision)], event=events[revision]
                ).model_copy(deep=True)
                for revision in selected
            )

    async def commit(
        self,
        request: CommitRequest,
        *,
        writer: WriterToken,
    ) -> CommitReceipt:
        async with self._lock:
            receipt, _, publish = self.prepare_commit(request, writer=writer)
            publish()
            return receipt

    def prepare_commit(
        self, request: CommitRequest, *, writer: WriterToken
    ) -> tuple[CommitReceipt, BoardSnapshot, Callable[[], None]]:
        """Stage a detached publication under _lock; the returned assignment cannot await."""
        check_writer(self._control(request.board_id), writer, self._session_id)
        key = (request.board_id, request.idempotency_key)
        prior = self._receipts.get(key)
        if prior:
            if prior.request_digest != request.request_digest:
                raise IdempotencyConflictError("idempotency key reused with different content")
            return (
                prior.model_copy(deep=True),
                self._boards[request.board_id].model_copy(deep=True),
                lambda: None,
            )
        current = self._boards[request.board_id]
        result = request.apply(current.model_copy(deep=True))
        validate_commit(request, result, current)
        snapshot = result.snapshot.model_copy(deep=True)
        event = result.event.model_copy(deep=True)
        receipt = result.receipt.model_copy(deep=True)
        boards = {**self._boards, snapshot.board_id: snapshot}
        receipts = {**self._receipts, key: receipt}
        events = {**self._events, snapshot.board_id: [*self._events[snapshot.board_id], event]}
        history = dict(self._history)
        for item in snapshot.objects:
            identity = (snapshot.board_id, item.id, item.revision)
            if identity not in history:
                history[identity] = item.model_copy(deep=True)
        detached = receipt.model_copy(deep=True)
        control = self._control(request.board_id)
        if request.wakeup:
            control = control.model_copy(update={"wakeup_revision": snapshot.revision})

        def publish() -> None:
            self._boards, self._receipts, self._events, self._history = (
                boards,
                receipts,
                events,
                history,
            )
            self._controls[request.board_id] = control

        return detached, snapshot, publish
