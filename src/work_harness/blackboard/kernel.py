"""Blackboard commands: validate proposals, reconcile knowledge, atomically publish."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from graphlib import CycleError, TopologicalSorter
from typing import Any
from uuid import uuid4

from work_harness.blackboard.deduplication import deduplicate
from work_harness.blackboard.goals import fact_is_effective
from work_harness.blackboard.models import (
    BoardObject,
    Fact,
    FactProposal,
    Hint,
    Intent,
    IntentContent,
    IntentProposal,
    ObjectRef,
    content_of,
    digest,
)
from work_harness.blackboard.projections import (
    object_id,
    reconcile,
    referenced_objects,
    revise,
)
from work_harness.blackboard.protocol import (
    BlackboardDelta,
    BlackboardError,
    BoardClosedError,
    BoardEvent,
    BoardFinalization,
    BoardSnapshot,
    CommitContext,
    CommitReceipt,
    IdempotencyConflictError,
    ObjectChange,
    StaleSnapshotError,
    StaleWriterError,
    TerminalStatus,
    WriterToken,
)
from work_harness.blackboard.queries import check_preconditions
from work_harness.blackboard.store import (
    BlackboardStore,
    CommitRequest,
    CommitSettlement,
    Publication,
    commit_digest,
)
from work_harness.blackboard.verification import VerificationRegistry


def initial_board(
    board_id: str, goal: IntentContent, *, source_id: str, now: datetime
) -> tuple[BoardSnapshot, BoardEvent]:
    """Construct the accepted root, including cancellation before the first execution."""
    goal = IntentContent.model_validate(goal.model_dump())
    if (
        goal.root_intent_ref
        or goal.parent_intent_ref
        or goal.derived_from
        or goal.links
        or goal.attachments
    ):
        raise BlackboardError("root goal cannot reference a board that does not exist yet")
    root = Intent(
        **goal.model_dump(),
        id=object_id(board_id, "root"),
        board_id=board_id,
        revision=1,
        created_at=now,
        created_by=source_id,
        content_digest=digest(goal),
    )
    return (
        BoardSnapshot(board_id=board_id, revision=1, root_intent_id=root.id, objects=(root,)),
        BoardEvent(
            board_id=board_id,
            sequence=1,
            source_id=source_id,
            committed_at=now,
            changed_refs=(root.ref,),
        ),
    )


class BlackboardKernel:
    def __init__(
        self,
        store: BlackboardStore,
        verification: VerificationRegistry,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        owner_id: str | None = None,
    ) -> None:
        self.store = store
        self.verification = verification
        self.clock = clock
        self.owner_id = owner_id or uuid4().hex
        self._writers: dict[str, WriterToken] = {}

    async def writer(self, board_id: str, *, takeover: bool = False) -> WriterToken:
        if takeover or board_id not in self._writers:
            self._writers[board_id] = await self.store.claim_writer(
                board_id, self.owner_id, takeover=takeover
            )
        return self._writers[board_id]

    async def put_content(
        self, board_id: str, body: str, *, media_type: str, writer: WriterToken | None = None
    ):
        return await self.store.put_content(
            board_id, body, media_type=media_type, writer=writer or await self.writer(board_id)
        )

    async def finalize(
        self,
        board_id: str,
        *,
        status: TerminalStatus,
        reason: str,
        writer: WriterToken | None = None,
        attempted_directions: tuple[str, ...] = (),
        observed_revision: int | None = None,
    ) -> BoardFinalization:
        control = await self.store.control(board_id)
        if control.finalization is not None:
            return control.finalization
        try:
            return await self.store.finalize(
                board_id,
                status=status,
                reason=reason,
                writer=None if status == "cancelled" else writer or await self.writer(board_id),
                clock=self.clock,
                attempted_directions=attempted_directions,
                observed_revision=observed_revision,
            )
        except BoardClosedError:
            final = (await self.store.control(board_id)).finalization
            if final is None:
                raise
            return final

    async def cancel(self, board_id: str, *, reason: str = "user_cancelled") -> BoardFinalization:
        """Host entry point for an authorized cancellation; atomically revokes all writers."""
        return await self.finalize(board_id, status="cancelled", reason=reason)

    async def committed(
        self, board_id: str, delta: BlackboardDelta, context: CommitContext
    ) -> CommitReceipt | None:
        """Read-only reconciliation remains possible after a board has been sealed."""
        previous = await self.store.receipt(board_id, delta.idempotency_key)
        if previous and previous.request_digest != commit_digest(delta, context):
            raise IdempotencyConflictError("idempotency key reused with different content")
        return previous

    async def create(self, board_id: str, goal: IntentContent, *, source_id: str) -> BoardSnapshot:
        snapshot, event = initial_board(board_id, goal, source_id=source_id, now=self.clock())
        created = await self.store.create(snapshot, event)
        await self.writer(board_id)
        return created

    async def commit(
        self,
        board_id: str,
        delta: BlackboardDelta,
        *,
        context: CommitContext,
        writer: WriterToken | None = None,
        settlement: CommitSettlement | None = None,
        user_request_digest: str | None = None,
    ) -> CommitReceipt:
        # Revalidate at the trust boundary, even if a caller used model_copy(update=...).
        delta = BlackboardDelta.model_validate(delta.model_dump())
        context = CommitContext.model_validate(context.model_dump())
        if settlement is not None:
            settlement.check_store(self.store)
        writer = writer or await self.writer(board_id)
        if writer.board_id != board_id:
            raise StaleWriterError("writer belongs to a different board")
        await self.store.check_writer(writer)
        if user_request_digest is not None and (
            not context.is_user or context.source_id != "user" or settlement is not None
        ):
            raise BlackboardError("only host user commands can publish an input wakeup")
        request_digest = user_request_digest or commit_digest(delta, context)
        previous = await self.store.receipt(board_id, delta.idempotency_key)
        if previous:
            if previous.request_digest != request_digest:
                raise IdempotencyConflictError("idempotency key reused with different content")
            if settlement is None:
                return previous

            def missing_receipt(snapshot: BoardSnapshot) -> Publication:
                raise BlackboardError("previously committed receipt is missing")

            return await settlement.commit(
                CommitRequest(board_id, delta.idempotency_key, request_digest, missing_receipt),
                writer=writer,
            )
        objects: dict[str, BoardObject] = {}
        now = self.clock()
        local_refs = {
            item.local_id: ObjectRef(
                object_id=object_id(board_id, delta.idempotency_key, item.local_id),
                revision=1,
            )
            for item in delta.proposals
        }
        for proposal in delta.proposals:
            if proposal.kind not in context.allowed_kinds:
                raise BlackboardError("knowledge source cannot emit this object kind")
            content = content_of(proposal)
            data = content.model_dump()
            for field in (
                "derived_from",
                "links",
                "premise_refs",
                "root_intent_ref",
                "parent_intent_ref",
                "source_refs",
            ):
                if field in data:
                    data[field] = self._resolve_locals(data[field], local_refs)
            metadata = {
                "id": local_refs[proposal.local_id].object_id,
                "board_id": board_id,
                "revision": 1,
                "created_at": now,
                "created_by": context.source_id,
                "origin_activation_id": context.activation_id,
            }
            data["content_digest"] = digest(type(content).model_validate(data))
            if isinstance(proposal, FactProposal):
                verification = await self.verification.verify(
                    board_id,
                    content,
                    proposal.verification_receipt_id,
                )
                item = Fact(**data, **metadata, verification=verification)
            elif isinstance(proposal, IntentProposal):
                item = Intent(**data, **metadata)
            else:
                item = Hint(**data, **metadata)
            objects[item.id] = item

        await self._validate_external_references(board_id, objects)
        return await (settlement or self.store).commit(
            CommitRequest(
                board_id=board_id,
                idempotency_key=delta.idempotency_key,
                request_digest=request_digest,
                apply=lambda snapshot: self._apply(
                    snapshot, delta, context, request_digest, objects, local_refs
                ),
                wakeup=user_request_digest is not None,
            ),
            writer=writer,
        )

    def _apply(
        self,
        snapshot: BoardSnapshot,
        delta: BlackboardDelta,
        context: CommitContext,
        request_digest: str,
        prepared: dict[str, BoardObject],
        local_refs: dict[str, ObjectRef],
    ) -> Publication:
        now = self.clock()
        check_preconditions(snapshot, delta, now)
        board_id = snapshot.board_id
        original = snapshot.by_id()
        objects = dict(original)
        if delta.goal_revision is not None:
            change = delta.goal_revision
            root = objects[snapshot.root_intent_id]
            if not context.is_user or "intent" not in context.allowed_kinds:
                raise BlackboardError("only user input may revise the root goal")
            if change.target != root.ref:
                raise StaleSnapshotError("goal revision requires the current root reference")
            objects[root.id] = revise(
                root,
                original,
                objective=change.objective,
                acceptance=change.acceptance,
                status="open",
                status_reason=change.reason,
                satisfied_by=(),
            )
        objects.update(
            {key: item.model_copy(update={"created_at": now}) for key, item in prepared.items()}
        )
        local_refs = dict(local_refs)
        new_ids = {ref.object_id for ref in local_refs.values()}
        self._validate_references(snapshot, objects, new_ids, now)
        self._validate_acyclic(objects, new_ids)
        if not context.is_user:
            parents = {
                item.id: [item.parent_intent_ref.object_id] if item.parent_intent_ref else []
                for item in objects.values()
                if isinstance(item, Intent)
            }
            for item_id in TopologicalSorter(parents).static_order():
                item = objects[item_id]
                if item_id in new_ids and isinstance(item, Intent) and item.parent_intent_ref:
                    parent = objects[item.parent_intent_ref.object_id]
                    objects[item_id] = revise(item, original, priority=parent.priority)
        new_ids = deduplicate(objects, new_ids, local_refs)
        for change in delta.changes:
            self._change(change, objects, original, snapshot.root_intent_id, context)
        for item_id in new_ids:
            item = objects[item_id]
            for link in item.links:
                if link.relation != "supersedes":
                    continue
                target = objects[link.target.object_id]
                if (
                    not context.can_supersede_facts
                    or not isinstance(item, Fact)
                    or not isinstance(
                        target,
                        Fact,
                    )
                ):
                    raise BlackboardError("superseding facts requires a verified, authorized fact")
                if (
                    item.claim_key is None
                    or item.claim_key != target.claim_key
                    or item.claim_type != target.claim_type
                    or item.scope != target.scope
                    or item.valid_from != target.valid_from
                    or item.valid_until != target.valid_until
                ):
                    raise BlackboardError("replacement must cover the same claim and scope")
                if link.target.revision != target.revision:
                    raise StaleSnapshotError("cannot supersede an outdated fact reference")
                objects[target.id] = revise(target, original, lifecycle="superseded")

        reconcile(
            objects, original, board_id=board_id, root_intent_id=snapshot.root_intent_id, now=now
        )
        if len(objects) > context.max_objects:
            raise BlackboardError("board object limit exceeded")
        children: dict[str, int] = {}
        for item in objects.values():
            if isinstance(item, Intent) and item.parent_intent_ref:
                parent_id = item.parent_intent_ref.object_id
                children[parent_id] = children.get(parent_id, 0) + 1
        if any(count > context.max_child_intents for count in children.values()):
            raise BlackboardError("child Intent limit exceeded")
        changed = tuple(item.ref for item in objects.values() if original.get(item.id) != item)
        receipt = CommitReceipt(
            board_id=board_id,
            idempotency_key=delta.idempotency_key,
            request_digest=request_digest,
            board_revision=snapshot.revision + 1,
            object_refs=changed,
            committed_at=now,
            local_refs={key: objects[ref.object_id].ref for key, ref in local_refs.items()},
        )
        updated = BoardSnapshot(
            board_id=board_id,
            revision=receipt.board_revision,
            root_intent_id=snapshot.root_intent_id,
            objects=tuple(sorted(objects.values(), key=lambda item: item.id)),
        )
        return Publication(
            updated,
            BoardEvent(
                board_id=board_id,
                sequence=updated.revision,
                source_id=context.source_id,
                activation_id=context.activation_id,
                committed_at=now,
                changed_refs=changed,
                requested_changes=delta.changes,
                goal_revision=delta.goal_revision,
            ),
            receipt,
        )

    @staticmethod
    def _resolve_locals(value: Any, local_refs: dict[str, ObjectRef]) -> Any:
        if isinstance(value, dict):
            if set(value) == {"object_id", "revision"} and str(value["object_id"]).startswith(
                "local:"
            ):
                name = value["object_id"][len("local:") :]
                if name not in local_refs or value["revision"] != 1:
                    raise BlackboardError("unknown local object reference")
                return local_refs[name].model_dump()
            return {
                key: BlackboardKernel._resolve_locals(item, local_refs)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [BlackboardKernel._resolve_locals(item, local_refs) for item in value]
        return value

    async def _validate_external_references(
        self, board_id: str, objects: dict[str, BoardObject]
    ) -> None:
        # These rows are immutable. All mutable-state checks run in _apply under the write lock.
        for item in objects.values():
            for attachment in item.attachments:
                await self.store.content(board_id, attachment)
            for ref in referenced_objects(item):
                if ref.object_id not in objects:
                    await self.store.get_object(board_id, ref)

    def _validate_references(
        self,
        snapshot: BoardSnapshot,
        objects: dict[str, BoardObject],
        new_ids: set[str],
        now: datetime,
    ) -> None:
        for item_id in new_ids:
            item = objects[item_id]
            for ref in referenced_objects(item):
                if ref.object_id in new_ids:
                    if ref.revision != 1:
                        raise BlackboardError("new references must address revision 1")
            targets = [link.target for link in item.links if link.relation == "contributes_to"]
            if not targets or any(
                not isinstance(objects.get(ref.object_id), Intent) for ref in targets
            ):
                raise BlackboardError("every object must contribute to an Intent on this board")
            for link in item.links:
                if link.relation == "refines" and (
                    not isinstance(item, Intent)
                    or not isinstance(objects.get(link.target.object_id), Intent)
                    or item.parent_intent_ref != link.target
                ):
                    raise BlackboardError("refines must agree with the parent Intent")
            if isinstance(item, Intent):
                if (
                    item.root_intent_ref is None
                    or item.root_intent_ref.object_id != snapshot.root_intent_id
                    or item.parent_intent_ref is None
                    or not isinstance(objects.get(item.parent_intent_ref.object_id), Intent)
                ):
                    raise BlackboardError("child Intent must connect to the existing root")
            if isinstance(item, Fact):
                for ref in item.premise_refs:
                    premise = objects.get(ref.object_id)
                    if not isinstance(premise, Fact):
                        raise BlackboardError("verification premise is not a current valid Fact")
                    if premise.revision != ref.revision or not fact_is_effective(
                        premise, objects, now
                    ):
                        raise StaleSnapshotError(
                            "verification premise is no longer a current valid Fact"
                        )

    @staticmethod
    def _validate_acyclic(objects: dict[str, BoardObject], new_ids: set[str]) -> None:
        graphs = {
            "derived_from": {
                item_id: [
                    ref.object_id
                    for ref in objects[item_id].derived_from
                    if ref.object_id in new_ids
                ]
                for item_id in new_ids
            },
            "parent_intent_ref": {
                item.id: [item.parent_intent_ref.object_id] if item.parent_intent_ref else []
                for item in objects.values()
                if isinstance(item, Intent)
            },
        }
        for relation, graph in graphs.items():
            try:
                tuple(TopologicalSorter(graph).static_order())
            except CycleError as exc:
                raise BlackboardError(f"cycle in {relation}") from exc

    @staticmethod
    def _change(
        change: ObjectChange,
        objects: dict[str, BoardObject],
        original: dict[str, BoardObject],
        root_id: str,
        context: CommitContext,
    ) -> None:
        item = objects.get(change.target.object_id)
        if item is None or item.revision != change.target.revision:
            raise StaleSnapshotError("state change requires the current object revision")
        if item.kind not in context.allowed_kinds:
            raise BlackboardError("knowledge source cannot change this kind")
        if change.action == "retract_fact" and isinstance(item, Fact):
            if not context.can_retract_facts:
                raise BlackboardError("fact retraction is not authorized")
            objects[item.id] = revise(item, original, lifecycle="retracted")
        elif isinstance(item, Hint) and change.action in {"consume_hint", "dismiss_hint"}:
            if item.status != "active":
                raise BlackboardError("only an active Hint can be consumed or dismissed")
            objects[item.id] = revise(
                item,
                original,
                status="consumed" if change.action == "consume_hint" else "dismissed",
                status_reason=change.reason,
            )
        elif isinstance(item, Intent) and change.action in {
            "block_intent",
            "reopen_intent",
            "abandon_intent",
        }:
            if (
                item.id == root_id
                and not context.is_user
                and (change.action == "abandon_intent" or item.status == "abandoned")
            ):
                raise BlackboardError("only user input may abandon or reactivate the root goal")
            statuses = {
                "block_intent": "blocked",
                "reopen_intent": "open",
                "abandon_intent": "abandoned",
            }
            objects[item.id] = revise(
                item,
                original,
                status=statuses[change.action],
                status_reason=change.reason,
                satisfied_by=(),
            )
        else:
            raise BlackboardError("state change does not apply to this object kind")
