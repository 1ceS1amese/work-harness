"""Bounded, state-driven batches: discover opportunities, execute, and reconsider."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime

from langgraph.types import Send, interrupt

from work_harness.blackboard.execution import (
    ActivationOffer,
    ActivationRef,
    ActivationStore,
    DispatchItem,
    RunLimits,
)
from work_harness.blackboard.goals import evaluate, fact_is_effective
from work_harness.blackboard.inputs import HumanInput, submit_input
from work_harness.blackboard.kernel import BlackboardKernel
from work_harness.blackboard.models import Fact, Intent, digest
from work_harness.blackboard.projections import reconcile as reconcile_projection
from work_harness.blackboard.protocol import (
    BlackboardDelta,
    BlackboardError,
    BoardClosedError,
    BoardSnapshot,
    CommitContext,
    GoalNotSatisfiedError,
    InputPendingError,
    StaleSnapshotError,
    StaleWriterError,
)
from work_harness.blackboard.timing import WakeupPlan, next_wakeup
from work_harness.knowledge_sources.base import BoardChangeSet
from work_harness.knowledge_sources.execution import SourceExecution
from work_harness.knowledge_sources.registry import KnowledgeSourceRegistry
from work_harness.storage.blackboard_factory import build_activation_store
from work_harness.workflow.blackboard_state import BlackboardGraphState

BLACKBOARD_WORKFLOW_REVISION = "blackboard-v8"


class BlackboardController:
    def __init__(
        self,
        kernel: BlackboardKernel,
        registry: KnowledgeSourceRegistry,
        *,
        ledger: ActivationStore | None = None,
        limits: RunLimits | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.kernel = kernel
        self.registry = registry
        self.ledger = ledger or build_activation_store(kernel.store)
        self.ledger.bind(kernel.store)
        self.limits = limits or RunLimits()
        self.execution = SourceExecution(self.ledger, kernel)
        self.sleep = sleep

    async def submit_input(self, board_id: str, request: HumanInput):
        return await submit_input(
            self.kernel,
            board_id,
            request,
            writer=await self.execution.writer(board_id),
            max_objects=self.limits.max_objects,
            max_child_intents=self.limits.max_child_intents,
        )

    def human_intents(self, snapshot: BoardSnapshot) -> list[dict]:
        pending = []
        for item in snapshot.objects:
            if not isinstance(item, Intent) or item.status in {"satisfied", "abandoned"}:
                continue
            missing = evaluate(item, snapshot.by_id(), self.kernel.clock()).missing_criteria
            if item.status == "blocked" or any(
                criterion.id in missing and criterion.claim_type == "user_statement"
                for criterion in item.acceptance
            ):
                pending.append(item.ref.model_dump())
        return pending

    async def human(self, state: BlackboardGraphState) -> BlackboardGraphState:
        snapshot = await self.kernel.store.snapshot(state["board_id"])
        if snapshot.finalization is not None or snapshot.revision != state["observed_revision"]:
            return {"route": "assess", "status": "running"}
        interrupt(
            {
                "kind": "blackboard_input_required",
                "board_id": snapshot.board_id,
                "board_revision": snapshot.revision,
                "pending_intents": self.human_intents(snapshot),
                "missing_criteria": evaluate(
                    snapshot.by_id()[snapshot.root_intent_id], snapshot.by_id(), self.kernel.clock()
                ).missing_criteria,
            }
        )
        return {"route": "assess", "status": "running", "pending_intents": []}

    async def reconcile(
        self,
        board_id: str,
        *,
        resumable: frozenset[tuple[str, int]] = frozenset(),
        takeover: bool = False,
    ) -> None:
        snapshot = await self.kernel.store.snapshot(board_id)
        if snapshot.finalization is not None and await self.ledger.reconcile_terminal(board_id):
            return
        records = await self.ledger.records(board_id)
        writer = None
        if snapshot.finalization is None:
            for record in records:
                if record.status in {"running", "result_ready"}:
                    if self.registry.get(record.spec.id).spec != record.spec:
                        raise BlackboardError("knowledge source changed before recovery")
            await self.ledger.start_run(board_id, self.limits, now=self.kernel.clock())
            await self.ledger.bind_contract(
                board_id,
                BLACKBOARD_WORKFLOW_REVISION,
                digest([source.spec.model_dump(mode="json") for source in self.registry.sources()]),
            )
            writer = await self.execution.writer(board_id, takeover=takeover)
            snapshot = await self._refresh(snapshot)
            for record in records:
                if (
                    record.status in {"running", "result_ready"}
                    or (record.candidate.id, record.attempt) in resumable
                ):
                    await self.ledger.adopt(record.ref, writer, recovering=True)
        for record in records:
            if record.status != "running":
                continue
            closed = snapshot.finalization is not None
            expired = self.kernel.clock() >= record.deadline
            missing = record.started and (record.candidate.id, record.attempt) not in resumable
            if closed or expired or missing:
                await self.ledger.stop(
                    record.ref,
                    retry=not closed,
                    reason="board_closed"
                    if closed
                    else "attempt_timeout"
                    if expired
                    else "checkpoint_missing",
                    writer=writer,
                    now=self.kernel.clock(),
                )
        # A partial batch must keep its barrier across recovery too: committing a later
        # sibling before an earlier resumable one would change the deterministic outcome.
        records = await self.ledger.records(board_id)
        if not any(record.status == "running" for record in records):
            await self.commit(
                {
                    "board_id": board_id,
                    "completed_activations": [
                        r.ref.model_dump() for r in records if r.status == "result_ready"
                    ],
                }
            )
        await self._schedule(await self.kernel.store.snapshot(board_id))

    async def bootstrap(self, state: BlackboardGraphState) -> BlackboardGraphState:
        await self.reconcile(state["board_id"])
        return {"workflow_revision": BLACKBOARD_WORKFLOW_REVISION}

    async def _refresh(self, snapshot: BoardSnapshot) -> BoardSnapshot:
        if snapshot.finalization is not None:
            return snapshot
        now = self.kernel.clock()
        objects = snapshot.by_id()
        projected = dict(objects)
        reconcile_projection(
            projected,
            objects,
            board_id=snapshot.board_id,
            root_intent_id=snapshot.root_intent_id,
            now=now,
        )
        if projected != objects:
            await self.kernel.commit(
                snapshot.board_id,
                BlackboardDelta(
                    expected_revision=snapshot.revision,
                    idempotency_key=f"refresh:{snapshot.revision}",
                ),
                context=CommitContext(
                    source_id="blackboard.controller",
                    max_objects=self.limits.max_objects,
                    max_child_intents=self.limits.max_child_intents,
                ),
                writer=await self.execution.writer(snapshot.board_id),
            )
            return await self.kernel.store.snapshot(snapshot.board_id)
        return snapshot

    async def _schedule(
        self,
        snapshot: BoardSnapshot,
        *,
        retry_ids: frozenset[str] | None = None,
        now: datetime | None = None,
    ) -> WakeupPlan | None:
        board_id = snapshot.board_id
        if snapshot.finalization is not None:
            await self.ledger.set_wakeup(board_id, None, writer=None)
            return None
        execution = await self.ledger.run(board_id)
        plan = next_wakeup(
            snapshot,
            await self.ledger.records(board_id),
            now=now or self.kernel.clock(),
            deadline=execution.deadline,
            retry_ids=retry_ids,
        )
        await self.ledger.set_wakeup(board_id, plan, writer=await self.execution.writer(board_id))
        return plan

    async def wait(self, state: BlackboardGraphState) -> BlackboardGraphState:
        board_id = state["board_id"]
        execution = await self.ledger.run(board_id)
        plan = execution.wakeup
        if (
            plan is None
            or not plan.waitable
            or state.get("wakeup_at") != plan.next_at.isoformat()
            or state.get("observed_revision") != plan.board_revision
        ):
            return {"route": "assess"}
        while True:
            snapshot = await self.kernel.store.snapshot(board_id)
            if snapshot.finalization is not None or snapshot.revision != plan.board_revision:
                return {"route": "assess"}
            try:
                await self.execution.writer(board_id)
            except BoardClosedError:
                return {"route": "assess"}
            remaining = (
                min(plan.next_at, execution.deadline) - self.kernel.clock()
            ).total_seconds()
            if remaining <= 0:
                return {"route": "assess"}
            # A bounded read-only poll also observes cancellation or input commits whose
            # notification was lost. Neither matching nor model calls run while waiting.
            await self.sleep(min(1.0, remaining))

    async def assess(self, state: BlackboardGraphState) -> BlackboardGraphState:
        board_id = state["board_id"]
        try:
            snapshot = await self._refresh(await self.kernel.store.snapshot(board_id))
        except BoardClosedError:
            snapshot = await self.kernel.store.snapshot(board_id)
        if snapshot.finalization is not None:
            return {
                "workflow_revision": BLACKBOARD_WORKFLOW_REVISION,
                "observed_revision": snapshot.revision,
                "route": "finish",
                "status": snapshot.finalization.status,
                "stop_reason": snapshot.finalization.reason,
            }
        now = self.kernel.clock()
        writer = await self.execution.writer(board_id)
        execution = await self.ledger.start_run(
            board_id,
            self.limits,
            now=datetime.fromisoformat(state["started_at"]) if state.get("started_at") else now,
        )
        await self.ledger.capture_progress(
            board_id,
            tuple(source.spec for source in self.registry.sources()),
            writer,
            self.kernel.clock,
        )
        started_at = execution.started_at.isoformat()
        update: BlackboardGraphState = {
            "workflow_revision": BLACKBOARD_WORKFLOW_REVISION,
            "started_at": started_at,
            "observed_revision": snapshot.revision,
            "status": "running",
            "route": "dispatch",
        }
        root = snapshot.by_id()[snapshot.root_intent_id]
        if root.status == "abandoned":
            return {
                **update,
                "route": "finish",
                "status": "cancelled",
                "stop_reason": "user_cancelled",
            }
        if root.status == "satisfied":
            return {
                **update,
                "route": "finish",
                "status": "succeeded",
                "stop_reason": "goal_satisfied",
            }
        summary = await self.ledger.summary(board_id)
        if now >= execution.deadline:
            return {**update, **self._stopped(snapshot, "timeout")}
        # A durable reservation may exist even if the dispatch checkpoint was never written.
        records = await self.ledger.records(board_id)
        ready = [record.ref.model_dump() for record in records if record.status == "result_ready"]
        if ready and not any(record.status == "running" for record in records):
            await self.commit({"board_id": board_id, "completed_activations": ready})
            return {**update, "route": "assess"}
        pending = [record for record in records if record.status in {"running", "result_ready"}]
        if pending:
            await self._schedule(snapshot, now=now)
            return {
                **update,
                "batch_id": pending[0].batch_id,
                "dispatch_items": [
                    DispatchItem(**r.ref.model_dump(), source_id=r.spec.id).model_dump()
                    for r in pending
                ],
            }
        if summary.attempts >= self.limits.max_activations:
            return {**update, **self._stopped(snapshot, "activation_limit")}
        if summary.no_progress >= self.limits.max_no_progress:
            return {**update, **self._stopped(snapshot, "no_progress")}
        events = await self.kernel.store.events(board_id, after=state.get("observed_revision", 0))
        changes = BoardChangeSet(
            from_revision=state.get("observed_revision", 0),
            to_revision=snapshot.revision,
            changed_ids=tuple(
                sorted({ref.object_id for event in events for ref in event.changed_refs})
            ),
        )
        candidates = self.registry.discover(snapshot, changes, now)
        missing = evaluate(root, snapshot.by_id(), now).missing_criteria
        gaps = {
            (criterion.claim_key, criterion.claim_type)
            for criterion in root.acceptance
            if criterion.id in missing
        }
        reservation = await self.ledger.reserve_batch(
            board_id,
            tuple(
                ActivationOffer(
                    candidate=candidate,
                    view=view,
                    spec=self.registry.get(candidate.source_id).spec,
                    critical_gap=any(
                        (criterion.claim_key, criterion.claim_type) in gaps
                        for criterion in view.intent.acceptance
                    ),
                )
                for candidate, view in candidates
            ),
            self.limits,
            now=now,
            writer=writer,
        )
        if reservation.batch is not None:
            await self._schedule(snapshot, now=now)
            return {
                **update,
                "batch_id": reservation.batch.id,
                "dispatch_items": [item.model_dump() for item in reservation.batch.items],
            }
        if reservation.busy:
            return {**update, "route": "assess"}
        plan = await self._schedule(
            snapshot,
            retry_ids=frozenset(candidate.id for candidate, _ in candidates),
            now=now,
        )
        if plan is not None and plan.waitable:
            return {**update, "route": "wait", "wakeup_at": plan.next_at.isoformat()}
        if reservation.budget_blocked:
            return {**update, **self._stopped(snapshot, "budget")}
        if pending_intents := self.human_intents(snapshot):
            return {
                **update,
                "route": "finish",
                "status": "waiting_human",
                "stop_reason": "input_required",
                "pending_intents": pending_intents,
            }
        return {**update, **self._stopped(snapshot, "blocked")}

    def _stopped(self, snapshot: BoardSnapshot, reason: str) -> BlackboardGraphState:
        useful = any(
            isinstance(item, Fact)
            and fact_is_effective(
                item,
                snapshot.by_id(),
                self.kernel.clock(),
            )
            for item in snapshot.objects
        )
        return {
            "route": "finish",
            "status": "partial" if useful else "failed",
            "stop_reason": reason,
        }

    @staticmethod
    def dispatch(state: BlackboardGraphState) -> list[Send] | str:
        if state["route"] != "dispatch":
            return state["route"]
        return [
            Send(
                item["source_id"],
                {
                    "activation_id": item["activation_id"],
                    "activation_attempt": item["attempt"],
                },
            )
            for item in state["dispatch_items"]
        ]

    async def commit(self, state: BlackboardGraphState) -> BlackboardGraphState:
        records = []
        for value in state.get("completed_activations", []):
            ref = ActivationRef.model_validate(value)
            record = await self.ledger.get(ref.activation_id, ref.attempt)
            if record.candidate.board_id != state["board_id"]:
                raise BlackboardError("completion belongs to a different board")
            records.append(record)
        # Network completion order must not choose the order in which proposals are accepted.
        for record in sorted(records, key=lambda record: record.ordinal):
            identity, ref = record.candidate.id, record.ref
            if record.status != "result_ready":
                continue
            output = record.output
            if output is None:
                raise BlackboardError("completed source has no private result")
            limits = (await self.ledger.run(state["board_id"])).limits
            closed = (await self.kernel.store.control(state["board_id"])).finalization is not None
            writer = None if closed else await self.execution.writer(state["board_id"])
            if writer is not None and record.writer is None:
                record = await self.ledger.adopt(ref, writer)
            if writer is not None and record.writer != writer:
                raise StaleWriterError("pending output belongs to another writer; reconcile first")
            if not closed and record.output_at is not None and record.output_at >= record.deadline:
                try:
                    await self.ledger.finish(
                        identity,
                        attempt=record.attempt,
                        status="retryable",
                        reason="attempt_timeout",
                        progress=False,
                        retry_delay_seconds=limits.retry_delay_seconds,
                        now=self.kernel.clock(),
                        writer=writer,
                    )
                except BoardClosedError:
                    await self.ledger.finish(
                        identity,
                        attempt=record.attempt,
                        status="rejected",
                        reason="board_closed",
                        progress=False,
                        now=self.kernel.clock(),
                    )
                continue
            if output.status in {"failed", "retryable_error"}:
                try:
                    await self.ledger.finish(
                        identity,
                        attempt=record.attempt,
                        status="rejected"
                        if closed
                        else "retryable"
                        if output.status == "retryable_error"
                        else "failed",
                        reason=output.reason,
                        progress=False,
                        retry_delay_seconds=limits.retry_delay_seconds * record.attempt,
                        writer=writer,
                        now=self.kernel.clock(),
                    )
                except BoardClosedError:
                    await self.ledger.finish(
                        identity,
                        attempt=record.attempt,
                        status="rejected",
                        reason="board_closed",
                        progress=False,
                        now=self.kernel.clock(),
                    )
                continue
            try:
                delta, context = record.proposal(limits)
                receipt = await self.kernel.committed(state["board_id"], delta, context)
                if receipt is None:
                    if closed:
                        raise BoardClosedError("board_closed")
                    receipt = await self.kernel.commit(
                        state["board_id"],
                        delta,
                        context=context,
                        writer=writer,
                        settlement=self.ledger.settlement(
                            ref,
                            self.kernel.clock,
                            tuple(source.spec for source in self.registry.sources()),
                        ),
                    )
                else:
                    # Older runs may have committed knowledge before recording completion.
                    await self._reconcile_commit(record.ref, receipt, writer)
            except StaleWriterError:
                raise
            except BoardClosedError:
                # Sealing may race a committed proposal before its ledger completion.
                receipt = await self.kernel.committed(state["board_id"], delta, context)
                if receipt:
                    await self._reconcile_commit(record.ref, receipt, None)
                else:
                    await self.ledger.finish(
                        identity,
                        attempt=record.attempt,
                        status="rejected",
                        reason="board_closed",
                        progress=False,
                        now=self.kernel.clock(),
                    )
            except StaleSnapshotError as exc:
                await self.ledger.finish(
                    identity,
                    attempt=record.attempt,
                    status="retryable",
                    reason=str(exc),
                    progress=False,
                    retry_delay_seconds=self.limits.retry_delay_seconds,
                    writer=writer,
                    now=self.kernel.clock(),
                )
            except BlackboardError as exc:
                await self.ledger.finish(
                    identity,
                    attempt=record.attempt,
                    status="rejected",
                    reason=str(exc),
                    progress=False,
                    writer=writer,
                    now=self.kernel.clock(),
                )
        snapshot = await self.kernel.store.snapshot(state["board_id"])
        try:
            snapshot = await self._refresh(snapshot)
        except BoardClosedError:
            snapshot = await self.kernel.store.snapshot(state["board_id"])
        await self._schedule(snapshot)
        return {}

    async def _reconcile_commit(self, ref, receipt, writer) -> None:
        await self.ledger.reconcile_commit(
            ref,
            receipt,
            await self.kernel.store.snapshot(receipt.board_id, revision=receipt.board_revision - 1),
            await self.kernel.store.snapshot(receipt.board_id, revision=receipt.board_revision),
            tuple(source.spec for source in self.registry.sources()),
            writer=writer,
            now=receipt.committed_at,
        )

    async def finish(self, state: BlackboardGraphState) -> BlackboardGraphState:
        snapshot = await self.kernel.store.snapshot(state["board_id"])
        attempted = tuple(
            dict.fromkeys(
                f"{record.spec.id}: {record.view.intent.objective[:240]}"
                f"（{record.attempt} 次，{record.status}）"
                for record in await self.ledger.records(state["board_id"])
            )
        )
        if snapshot.finalization is None:
            try:
                snapshot = await self._refresh(snapshot)
            except BoardClosedError:
                snapshot = await self.kernel.store.snapshot(state["board_id"])
            root = snapshot.by_id()[snapshot.root_intent_id]
            if (
                state["status"] != "cancelled"
                and root.status != "abandoned"
                and evaluate(root, snapshot.by_id(), self.kernel.clock()).satisfied
            ):
                state = {**state, "status": "succeeded", "stop_reason": "goal_satisfied"}
        if snapshot.finalization is not None or state["status"] != "waiting_human":
            try:
                final = await self.kernel.finalize(
                    state["board_id"],
                    status=state["status"],
                    reason=state["stop_reason"],
                    writer=None
                    if snapshot.finalization
                    else await self.execution.writer(state["board_id"]),
                    attempted_directions=attempted,
                    observed_revision=state.get("observed_revision", snapshot.revision),
                )
            except (GoalNotSatisfiedError, InputPendingError):
                return {"route": "assess", "status": "running"}
            await self.reconcile(state["board_id"])
            return {
                "route": "end",
                "status": final.status,
                "stop_reason": final.reason,
                "final_output": final.output,
                "finalized_at_revision": final.finalized_at_revision,
            }
        root = snapshot.by_id()[snapshot.root_intent_id]
        result = evaluate(root, snapshot.by_id(), self.kernel.clock())
        statements = [snapshot.by_id()[ref.object_id].statement for ref in result.witnesses]
        if result.missing_criteria:
            statements.append("尚未满足的条件：" + "、".join(result.missing_criteria))
        if attempted:
            statements.append("已尝试方向：" + "；".join(attempted))
        return {
            "route": "human",
            "final_output": "\n".join(statements),
            "observed_revision": snapshot.revision,
            "pending_intents": self.human_intents(snapshot),
        }
