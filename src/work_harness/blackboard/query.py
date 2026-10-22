"""Public reads expose committed knowledge and allowlisted activation provenance."""

from __future__ import annotations

import base64
import binascii
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from pydantic import AwareDatetime, Field, ValidationError

from work_harness.blackboard.goals import GoalEvaluation, contributes_to, evaluate
from work_harness.blackboard.models import (
    BoardModel,
    BoardObject,
    Fact,
    FactLifecycle,
    HintStatus,
    Intent,
    IntentStatus,
    ObjectKind,
    ObjectRef,
)
from work_harness.blackboard.protocol import (
    BoardEvent,
    BoardFinalization,
    BoardSnapshot,
    HistoryUnavailableError,
    ObjectRevision,
    StoredContent,
)
from work_harness.blackboard.store import BlackboardStore

if TYPE_CHECKING:
    from work_harness.blackboard.execution import ActivationStore

ObjectStatus = FactLifecycle | IntentStatus | HintStatus


class QueryNotFoundError(ValueError):
    pass


class QueryConflictError(ValueError):
    pass


class ObjectFilter(BoardModel):
    kind: ObjectKind | None = None
    status: ObjectStatus | None = None
    topic: str | None = Field(default=None, min_length=1, max_length=500)
    intent_id: str | None = Field(default=None, min_length=1, max_length=500)


class ReadCursor(BoardModel):
    version: Literal[1] = 1
    kind: Literal["objects", "history", "events"]
    board_id: str = Field(min_length=1, max_length=500)
    revision: int = Field(ge=0, le=2**63 - 1, strict=True)
    after_id: str | None = Field(default=None, min_length=1, max_length=500)
    object_id: str | None = Field(default=None, min_length=1, max_length=500)
    after_revision: int = Field(default=0, ge=0, strict=True)
    filters: ObjectFilter = Field(default_factory=ObjectFilter)

    def encode(self) -> str:
        return base64.urlsafe_b64encode(self.model_dump_json().encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, token: str, *, board_id: str, kind: str) -> ReadCursor:
        try:
            if not token or len(token) > 16384:
                raise ValueError("invalid cursor length")
            raw = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
            cursor = cls.model_validate_json(raw)
            if cursor.board_id != board_id or cursor.kind != kind:
                raise ValueError("cursor belongs to another board or query")
            if kind != "objects" and (cursor.after_id or cursor.filters != ObjectFilter()):
                raise ValueError("invalid cursor fields")
            if kind == "events" and (cursor.object_id or cursor.after_revision):
                raise ValueError("invalid event cursor")
            if kind == "objects" and (cursor.object_id or cursor.after_revision):
                raise ValueError("invalid objects cursor")
            if kind != "events" and cursor.revision < 1:
                raise ValueError("cursor must bind a board revision")
            return cursor
        except (ValueError, ValidationError, binascii.Error) as exc:
            raise ValueError("invalid or incompatible blackboard cursor") from exc


def event_cursor(board_id: str, sequence: int) -> str:
    return ReadCursor(kind="events", board_id=board_id, revision=sequence).encode()


class SnapshotSummary(BoardModel):
    board_id: str
    revision: int
    evaluated_at: AwareDatetime
    counts: dict[ObjectKind, int]
    root_intent: Intent
    goal: GoalEvaluation
    finalization: BoardFinalization | None
    objects_cursor: str
    event_cursor: str


class ObjectPage(BoardModel):
    board_id: str
    revision: int
    filters: ObjectFilter
    items: tuple[BoardObject, ...]
    total: int
    limit: int
    next_cursor: str | None


class ActivationProvenance(BoardModel):
    activation_id: str
    source_id: str
    source_version: str
    reason: str
    intent_ref: ObjectRef
    input_refs: tuple[ObjectRef, ...]


class ObjectDetail(BoardModel):
    board_id: str
    revision: int
    item: ObjectRevision
    activation: ActivationProvenance | None = None


class HistoryPage(BoardModel):
    board_id: str
    revision: int
    object_id: str
    items: tuple[ObjectRevision, ...]
    total: int
    limit: int
    next_cursor: str | None


class EventPage(BoardModel):
    board_id: str
    revision: int
    items: tuple[BoardEvent, ...]
    next_cursor: str
    has_more: bool
    finalization: BoardFinalization | None


class ContentDetail(BoardModel):
    board_id: str
    revision: int
    content: StoredContent


class BlackboardQuery:
    def __init__(
        self, store: BlackboardStore, board_id: str, *, ledger: ActivationStore | None = None
    ) -> None:
        self.store = store
        self.board_id = board_id
        self.ledger = ledger

    async def _snapshot(self, revision: int | None) -> BoardSnapshot:
        current = await self.store.snapshot(self.board_id)
        if revision is None or revision == current.revision:
            return current
        if revision < 1 or revision > current.revision:
            raise QueryConflictError("requested board revision does not exist")
        return await self.store.snapshot(self.board_id, revision=revision)

    async def summary(self, *, at_revision: int | None = None) -> SnapshotSummary:
        snapshot = await self._snapshot(at_revision)
        evaluated_at = datetime.now(UTC)
        if snapshot.finalization:
            evaluated_at = snapshot.finalization.finalized_at
        elif at_revision is not None:
            page = await self.events(
                after=event_cursor(self.board_id, snapshot.revision - 1), limit=1
            )
            evaluated_at = page.items[0].committed_at
        objects = snapshot.by_id()
        root = objects[snapshot.root_intent_id]
        assert isinstance(root, Intent)
        return SnapshotSummary(
            board_id=self.board_id,
            revision=snapshot.revision,
            evaluated_at=evaluated_at,
            counts={
                kind: sum(item.kind == kind for item in snapshot.objects)
                for kind in ("fact", "intent", "hint")
            },
            root_intent=root,
            goal=evaluate(root, objects, evaluated_at),
            finalization=snapshot.finalization,
            objects_cursor=ReadCursor(
                kind="objects", board_id=self.board_id, revision=snapshot.revision
            ).encode(),
            event_cursor=event_cursor(self.board_id, snapshot.revision),
        )

    @staticmethod
    def _limit(limit: int) -> None:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")

    def _cursor(self, token: str | None, kind: str, revision: int | None) -> ReadCursor | None:
        if token is None:
            return None
        cursor = ReadCursor.decode(token, board_id=self.board_id, kind=kind)
        if revision is not None and revision != cursor.revision:
            raise ValueError("cursor and board revision disagree")
        return cursor

    @staticmethod
    def _in_intent(item: BoardObject, intent_id: str, objects: dict[str, BoardObject]) -> bool:
        target = item
        seen: set[str] = set()
        while isinstance(target, Intent) and target.id not in seen:
            if target.id == intent_id:
                return True
            seen.add(target.id)
            target = (
                objects.get(target.parent_intent_ref.object_id)
                if target.parent_intent_ref
                else None
            )
        return contributes_to(item, intent_id, objects)

    async def objects(
        self,
        *,
        filters: ObjectFilter | None = None,
        cursor: str | None = None,
        at_revision: int | None = None,
        limit: int = 50,
    ) -> ObjectPage:
        self._limit(limit)
        bound = self._cursor(cursor, "objects", at_revision)
        selected = filters or ObjectFilter()
        if bound:
            for key, value in selected.model_dump(exclude_none=True).items():
                if getattr(bound.filters, key) != value:
                    raise ValueError("filters disagree with the cursor")
            selected = bound.filters
        snapshot = await self._snapshot(bound.revision if bound else at_revision)
        objects = snapshot.by_id()
        if selected.intent_id and not isinstance(objects.get(selected.intent_id), Intent):
            raise QueryNotFoundError("Intent not found in this snapshot")
        matches = sorted(
            (
                item
                for item in snapshot.objects
                if (selected.kind is None or item.kind == selected.kind)
                and (selected.topic is None or item.topic == selected.topic)
                and (
                    selected.status is None
                    or (item.lifecycle if isinstance(item, Fact) else item.status)
                    == selected.status
                )
                and (
                    selected.intent_id is None or self._in_intent(item, selected.intent_id, objects)
                )
            ),
            key=lambda item: item.id,
        )
        after = bound.after_id if bound else None
        if after and not any(item.id == after for item in matches):
            raise ValueError("cursor position is absent from the selected snapshot")
        remaining = [item for item in matches if after is None or item.id > after]
        items = tuple(remaining[:limit])
        next_cursor = None
        if len(remaining) > limit:
            next_cursor = ReadCursor(
                kind="objects",
                board_id=self.board_id,
                revision=snapshot.revision,
                filters=selected,
                after_id=items[-1].id,
            ).encode()
        return ObjectPage(
            board_id=self.board_id,
            revision=snapshot.revision,
            filters=selected,
            items=items,
            total=len(matches),
            limit=limit,
            next_cursor=next_cursor,
        )

    async def history(
        self,
        object_id: str,
        *,
        cursor: str | None = None,
        at_revision: int | None = None,
        limit: int = 50,
    ) -> HistoryPage:
        self._limit(limit)
        bound = self._cursor(cursor, "history", at_revision)
        if bound and bound.object_id != object_id:
            raise ValueError("history cursor belongs to another object")
        snapshot = await self._snapshot(bound.revision if bound else at_revision)
        current = snapshot.by_id().get(object_id)
        if current is None:
            raise QueryNotFoundError("object not found in this snapshot")
        after = bound.after_revision if bound else 0
        if after > current.revision:
            raise ValueError("history cursor is ahead of the object")
        items = await self._history(
            object_id, snapshot.revision, after, min(limit, current.revision - after)
        )
        next_cursor = None
        if after + len(items) < current.revision:
            next_cursor = ReadCursor(
                kind="history",
                board_id=self.board_id,
                revision=snapshot.revision,
                object_id=object_id,
                after_revision=items[-1].object.revision,
            ).encode()
        return HistoryPage(
            board_id=self.board_id,
            revision=snapshot.revision,
            object_id=object_id,
            items=items,
            total=current.revision,
            limit=limit,
            next_cursor=next_cursor,
        )

    async def _history(
        self, object_id: str, through: int, after: int, count: int
    ) -> tuple[ObjectRevision, ...]:
        if count == 0:
            return ()
        items = await self.store.object_history(
            self.board_id,
            object_id,
            through=through,
            after=after,
            limit=count,
        )
        if [item.object.revision for item in items] != list(range(after + 1, after + count + 1)):
            raise HistoryUnavailableError("object history is incomplete")
        if any(item.object.ref not in item.event.changed_refs for item in items):
            raise HistoryUnavailableError("object history does not match its events")
        return items

    async def object(
        self,
        object_id: str,
        *,
        object_revision: int | None = None,
        at_revision: int | None = None,
    ) -> ObjectDetail:
        snapshot = await self._snapshot(at_revision)
        current = snapshot.by_id().get(object_id)
        if current is None or (
            object_revision is not None and not 1 <= object_revision <= current.revision
        ):
            raise QueryNotFoundError("object revision not found in this snapshot")
        revision = current.revision if object_revision is None else object_revision
        items = await self._history(object_id, snapshot.revision, revision - 1, 1)
        item = items[0]
        identity = item.event.activation_id or item.object.origin_activation_id
        activation = None
        if identity and self.ledger is not None:
            # Query only the owning board. Never expose mutable attempt status,
            # private output, prompts or the full execution view. The referenced
            # activation must predate this object version's committed event.
            for record in await self.ledger.records(self.board_id):
                candidate = record.candidate
                if candidate.id == identity and record.view.board_revision < item.event.sequence:
                    activation = ActivationProvenance(
                        activation_id=candidate.id,
                        source_id=candidate.source_id,
                        source_version=candidate.source_version,
                        reason=candidate.reason,
                        intent_ref=candidate.intent_ref,
                        input_refs=candidate.input_refs,
                    )
                    break
        return ObjectDetail(
            board_id=self.board_id, revision=snapshot.revision, item=item, activation=activation
        )

    async def events(self, *, after: str | None = None, limit: int = 100) -> EventPage:
        self._limit(limit)
        position = self._cursor(after, "events", None)
        sequence = position.revision if position else 0
        window = await self.store.event_window(self.board_id, after=sequence, limit=limit)
        if sequence > window.revision:
            raise QueryConflictError("event cursor is ahead of the durable log")
        expected = list(range(sequence + 1, min(sequence + limit, window.revision) + 1))
        if [event.sequence for event in window.events] != expected:
            raise HistoryUnavailableError("event log is incomplete; obtain a new snapshot")
        last = window.events[-1].sequence if window.events else sequence
        return EventPage(
            board_id=self.board_id,
            revision=window.revision,
            items=window.events,
            next_cursor=event_cursor(self.board_id, last),
            has_more=last < window.revision,
            finalization=window.finalization,
        )

    async def content(self, digest: str, *, at_revision: int | None = None) -> ContentDetail:
        snapshot = await self._snapshot(at_revision)
        ref = next(
            (
                ref
                for item in snapshot.objects
                for ref in item.attachments
                if ref.content_digest == digest
            ),
            None,
        )
        if ref is None:
            raise QueryNotFoundError("attachment is not referenced by this snapshot")
        return ContentDetail(
            board_id=self.board_id,
            revision=snapshot.revision,
            content=await self.store.content(self.board_id, ref),
        )
