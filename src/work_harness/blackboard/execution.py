"""Activation and accounting rules shared by memory and durable stores.

These are private execution records, never blackboard knowledge objects.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal, TypeVar

from pydantic import AwareDatetime, Field

from work_harness.blackboard.activation import activation_preconditions, candidate_for_view
from work_harness.blackboard.models import BoardModel, digest
from work_harness.blackboard.progress import (
    ProgressDecision,
    ProgressState,
    advance,
    initial,
    permits_exploration,
    summarize,
)
from work_harness.blackboard.protocol import (
    BlackboardDelta,
    BlackboardError,
    BoardSnapshot,
    CommitContext,
    CommitReceipt,
    StaleWriterError,
    WriterToken,
)
from work_harness.blackboard.store import BlackboardStore, CommitRequest, commit_digest
from work_harness.blackboard.timing import WakeupPlan
from work_harness.knowledge_sources.base import (
    ActivationCandidate,
    BoardView,
    KnowledgeOutput,
    KnowledgeSourceSpec,
    UsageRecord,
)

_Result = TypeVar("_Result")
FinishedStatus = Literal["completed", "no_progress", "rejected", "failed", "retryable"]


class RunLimits(BoardModel):
    # Historical execution metadata only. Usage never controls admission.
    token_budget: int | None = Field(default=None, ge=0, strict=True, deprecated=True)
    cost_budget_usd: float | None = Field(default=None, ge=0, deprecated=True)
    max_activations: int = Field(default=40, ge=1, strict=True)
    max_no_progress: int = Field(default=5, ge=1, strict=True)
    max_explorations: int = Field(default=4, ge=0, le=64, strict=True)
    max_objects: int = Field(default=256, ge=1, strict=True)
    max_child_intents: int = Field(default=16, ge=1, strict=True)
    max_concurrency: int = Field(default=4, ge=1, le=64, strict=True)
    max_per_intent: int = Field(default=2, ge=1, le=64, strict=True)
    max_wait_batches: int = Field(default=3, ge=1, strict=True)
    timeout_seconds: float = Field(default=300, gt=0, le=86400)
    retry_delay_seconds: float = Field(default=0.1, ge=0, le=30)


class ActivationRef(BoardModel):
    activation_id: str
    attempt: int = Field(ge=1, strict=True)


class DispatchItem(ActivationRef):
    source_id: str


class DispatchBatch(BoardModel):
    id: str
    board_id: str
    ordinal: int = Field(ge=1)
    reserved_at: AwareDatetime
    items: tuple[DispatchItem, ...]


class ActivationOffer(BoardModel):
    candidate: ActivationCandidate
    view: BoardView
    spec: KnowledgeSourceSpec
    # Set by the host from the full board's missing root criteria, never source self-rating.
    critical_gap: bool = False


class SelectionReason(BoardModel):
    policy: Literal["priority", "waiting_turn"]
    waited_batches: int = Field(ge=0)
    conflict: bool
    critical_gap: bool
    intent_priority: int
    shared_inputs: int = Field(default=0, ge=0)


class BatchReservation(BoardModel):
    batch: DispatchBatch | None = None
    budget_blocked: bool = False
    retry_at: AwareDatetime | None = None
    busy: bool = False


class RunExecution(BoardModel):
    board_id: str
    limits: RunLimits
    started_at: AwareDatetime
    deadline: AwareDatetime
    no_progress: int = Field(default=0, ge=0)
    first_seen: dict[str, int] = Field(default_factory=dict)
    workflow_revision: str | None = None
    registry_digest: str | None = None
    checkpoint_thread_id: str | None = None
    batches: tuple[DispatchBatch, ...] = ()
    waiting_batches: dict[str, int] = Field(default_factory=dict)
    last_scheduled: dict[str, int] = Field(default_factory=dict)
    wakeup: WakeupPlan | None = None
    progress: ProgressState | None = None
    progress_sources: tuple[KnowledgeSourceSpec, ...] | None = None


class ActivationRecord(BoardModel):
    candidate: ActivationCandidate
    view: BoardView
    spec: KnowledgeSourceSpec
    attempt: int = Field(default=1, ge=1)
    ordinal: int = Field(ge=1)
    deadline: AwareDatetime
    status: Literal[
        "running", "result_ready", "completed", "no_progress", "rejected", "failed", "retryable"
    ] = "running"
    output: KnowledgeOutput | None = None
    output_at: AwareDatetime | None = None
    reason: str = ""
    retry_at: AwareDatetime | None = None
    usage_complete: bool = False
    writer: WriterToken | None = None
    # Older ledgers did not record dispatch start; conservatively treat those as started.
    started: bool = True
    usage_uncertain: bool = False
    pending_calls: tuple[str, ...] = ()
    batch_id: str | None = None
    selection: SelectionReason | None = None
    progress: ProgressDecision | None = None

    @property
    def ref(self) -> ActivationRef:
        return ActivationRef(activation_id=self.candidate.id, attempt=self.attempt)

    def proposal(self, limits: RunLimits) -> tuple[BlackboardDelta, CommitContext]:
        """Reconstruct the host envelope from the persisted invocation, never source identity."""
        if self.output is None or self.output.status not in {"completed", "no_progress"}:
            raise BlackboardError("attempt has no committable private output")
        return (
            BlackboardDelta(
                expected_revision=self.view.board_revision,
                idempotency_key=f"{self.candidate.id}:{self.attempt}",
                proposals=self.output.proposals,
                changes=self.output.changes,
                read_set=self.candidate.input_refs,
                concurrency="inputs",
                preconditions=activation_preconditions(self.spec, self.view),
            ),
            CommitContext(
                source_id=self.spec.id,
                activation_id=self.candidate.id,
                allowed_kinds=self.spec.emitted_kinds,
                can_retract_facts=self.spec.can_retract_facts,
                can_supersede_facts=self.spec.can_supersede_facts,
                max_objects=limits.max_objects,
                max_child_intents=limits.max_child_intents,
            ),
        )


class UsageSummary(BoardModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    tokens: int = 0
    cost_usd: float = 0
    calls: int = 0
    unknown_calls: int = 0
    unresolved_attempts: int = 0
    usage_complete: bool = True
    # Kept as zero for readers of the old summary contract; nothing is reserved.
    reserved_tokens: int = 0
    reserved_cost_usd: float = 0
    attempts: int = 0
    no_progress: int = 0


class BudgetExceeded(BlackboardError):
    pass


class CapacityExceeded(BlackboardError):
    pass


@dataclass
class LedgerState:
    """One board's bounded history, loaded inside a storage transaction."""

    board_id: str
    execution: RunExecution | None = None
    attempts: dict[tuple[str, int], ActivationRecord] = field(default_factory=dict)
    calls: dict[tuple[str, int, str], UsageRecord] = field(default_factory=dict)

    def require_run(self) -> RunExecution:
        if self.execution is None:
            raise BlackboardError("execution has not been registered")
        return self.execution

    def start(self, limits: RunLimits, now: datetime) -> RunExecution:
        if self.execution is not None:
            if self.execution.limits != limits:
                raise BlackboardError("run limits cannot change after execution begins")
        else:
            self.execution = RunExecution(
                board_id=self.board_id,
                limits=limits,
                started_at=now,
                deadline=now + timedelta(seconds=limits.timeout_seconds),
            )
        return self.execution

    def records(self) -> tuple[ActivationRecord, ...]:
        latest: dict[str, ActivationRecord] = {}
        for (identity, number), record in self.attempts.items():
            if identity not in latest or number > latest[identity].attempt:
                latest[identity] = record
        return tuple(sorted(latest.values(), key=lambda item: item.ordinal))

    def get(self, activation_id: str, attempt: int | None = None) -> ActivationRecord:
        if attempt is not None:
            record = self.attempts.get((activation_id, attempt))
        else:
            record = next((r for r in self.records() if r.candidate.id == activation_id), None)
        if record is None:
            raise BlackboardError("unknown activation attempt")
        return record

    def usage(self, activation_id: str, attempt: int) -> tuple[UsageRecord, ...]:
        return tuple(
            record
            for (identity, number, _), record in sorted(self.calls.items())
            if identity == activation_id and number == attempt
        )

    def summary(self) -> UsageSummary:
        pending = sum(len(r.pending_calls) for r in self.attempts.values())
        unknown = pending + sum(not c.usage_complete for c in self.calls.values())
        unresolved = sum(
            bool(r.pending_calls)
            or r.usage_uncertain
            or (r.status != "running" and not r.usage_complete)
            for r in self.attempts.values()
        )
        return UsageSummary(
            input_tokens=sum(item.input_tokens for item in self.calls.values()),
            output_tokens=sum(item.output_tokens for item in self.calls.values()),
            cached_tokens=sum(item.cached_tokens for item in self.calls.values()),
            tokens=sum(item.tokens for item in self.calls.values()),
            cost_usd=sum(item.cost_usd for item in self.calls.values()),
            calls=len(self.calls) + pending,
            unknown_calls=unknown,
            unresolved_attempts=unresolved,
            usage_complete=not (unknown or unresolved),
            attempts=len(self.attempts),
            no_progress=self.execution.no_progress if self.execution else 0,
        )

    def remember(self, candidate_ids: tuple[str, ...]) -> dict[str, int]:
        execution = self.require_run()
        first_seen = dict(execution.first_seen)
        for identity in candidate_ids:
            first_seen.setdefault(identity, len(self.attempts))
        self.execution = execution.model_copy(update={"first_seen": first_seen})
        return first_seen

    def bind_contract(self, revision: str, registry_digest: str) -> None:
        execution = self.require_run()
        if execution.workflow_revision not in (None, revision) or execution.registry_digest not in (
            None,
            registry_digest,
        ):
            raise BlackboardError("execution configuration is incompatible with recovery")
        self.execution = execution.model_copy(
            update={"workflow_revision": revision, "registry_digest": registry_digest}
        )

    def bind_thread(self, thread_id: str) -> None:
        execution = self.require_run()
        if execution.checkpoint_thread_id not in (None, thread_id):
            raise BlackboardError("run is bound to another checkpoint thread")
        self.execution = execution.model_copy(update={"checkpoint_thread_id": thread_id})

    def adopt(
        self, ref: ActivationRef, writer: WriterToken, *, recovering: bool = False
    ) -> ActivationRecord:
        record = self.get(ref.activation_id, ref.attempt)
        if writer.board_id != self.board_id or self.get(ref.activation_id).attempt != ref.attempt:
            raise BlackboardError("cannot adopt an outdated or foreign attempt")
        record = record.model_copy(
            update={
                "writer": writer,
                "usage_uncertain": record.usage_uncertain
                or (
                    recovering
                    and record.started
                    and record.status == "running"
                    and (record.spec.adapter_type != "agent" or bool(record.pending_calls))
                ),
            }
        )
        self.attempts[(ref.activation_id, ref.attempt)] = record
        return record

    def check_attempt(self, ref: ActivationRef, writer: WriterToken | None) -> ActivationRecord:
        record = self.get(ref.activation_id, ref.attempt)
        if writer is not None and record.writer != writer:
            raise StaleWriterError("attempt belongs to another writer")
        return record

    def begin(self, ref: ActivationRef, writer: WriterToken) -> ActivationRecord:
        record = self.check_attempt(ref, writer)
        if self.get(ref.activation_id).attempt != ref.attempt:
            raise BlackboardError("cannot execute an outdated attempt")
        if record.output is not None:
            return record
        if record.status != "running":
            raise BlackboardError("cannot execute a stopped attempt")
        record = record.model_copy(update={"started": True})
        self.attempts[(ref.activation_id, ref.attempt)] = record
        return record

    def reserve(
        self,
        candidate: ActivationCandidate,
        view: BoardView,
        spec: KnowledgeSourceSpec,
        now: datetime,
        run_deadline: datetime | None,
        writer: WriterToken | None = None,
    ) -> ActivationRecord | None:
        execution = self.require_run()
        if writer is not None and writer.board_id != self.board_id:
            raise BlackboardError("writer belongs to a different board")
        expected = candidate_for_view(spec, view)
        if candidate.board_id != self.board_id or candidate.model_dump(
            exclude={"reason"}
        ) != expected.model_dump(exclude={"reason"}):
            raise BlackboardError("activation identity does not match its input and source")
        old = next((r for r in self.records() if r.candidate.id == candidate.id), None)
        if old and old.spec != spec:
            raise BlackboardError("source configuration changed for an existing activation")
        if old and (
            old.status != "retryable"
            or old.attempt >= spec.limits.max_attempts
            or (old.retry_at and old.retry_at > now)
        ):
            return None
        deadline = min(execution.deadline, run_deadline) if run_deadline else execution.deadline
        if now >= deadline:
            raise BudgetExceeded("run deadline has elapsed")
        summary = self.summary()
        limits = execution.limits
        if summary.attempts >= limits.max_activations:
            raise BudgetExceeded("activation count limit reached")
        occupied = [r for r in self.records() if r.status in {"running", "result_ready"}]
        if (
            len(occupied) >= limits.max_concurrency
            or sum(
                r.candidate.intent_ref.object_id == candidate.intent_ref.object_id for r in occupied
            )
            >= limits.max_per_intent
        ):
            raise CapacityExceeded("activation concurrency or intent quota is full")
        record = ActivationRecord(
            candidate=candidate,
            view=view,
            spec=spec,
            attempt=old.attempt + 1 if old else 1,
            ordinal=summary.attempts + 1,
            deadline=min(now + timedelta(seconds=spec.limits.timeout_seconds), deadline),
            writer=writer,
            started=False,
        )
        self.attempts[(candidate.id, record.attempt)] = record
        return record

    def reserve_batch(
        self, offers: tuple[ActivationOffer, ...], now: datetime, writer: WriterToken | None
    ) -> BatchReservation:
        execution = self.require_run()
        limits = execution.limits
        # Batches are barriers: never top up while a previous batch still has unfinished work.
        if any(r.status in {"running", "result_ready"} for r in self.records()):
            return BatchReservation(busy=True)
        eligible: dict[str, ActivationOffer] = {}
        retry_times: list[datetime] = []
        for offer in offers:
            candidate, view, spec = offer.candidate, offer.view, offer.spec
            expected = candidate_for_view(spec, view)
            if candidate.board_id != self.board_id or candidate.model_dump(
                exclude={"reason"}
            ) != expected.model_dump(exclude={"reason"}):
                raise BlackboardError("activation identity does not match its input and source")
            old = next((r for r in self.records() if r.candidate.id == candidate.id), None)
            if old and old.spec != spec:
                raise BlackboardError("source configuration changed for an existing activation")
            if old:
                if old.status != "retryable" or old.attempt >= spec.limits.max_attempts:
                    continue
                if old.retry_at and old.retry_at > now:
                    retry_times.append(old.retry_at)
                    continue
            eligible[candidate.id] = offer
        first_seen = self.remember(tuple(eligible))
        waits = execution.waiting_batches

        def rank(offer: ActivationOffer) -> tuple:
            candidate = offer.candidate
            branch = candidate.intent_ref.object_id
            return (
                not candidate.conflict,
                not offer.critical_gap,
                -candidate.intent_priority,
                -waits.get(branch, 0),
                first_seen[candidate.id],
                candidate.source_id,
                candidate.id,
            )

        ordered = sorted(eligible.values(), key=rank)
        summary = self.summary()
        affordable = {
            offer.candidate.id
            for offer in ordered
            if now < execution.deadline and summary.attempts < limits.max_activations
        }
        # One overdue branch gets the first slot, even with a concurrency limit of one.
        overdue = [
            offer
            for offer in ordered
            if offer.candidate.id in affordable
            and waits.get(offer.candidate.intent_ref.object_id, 0) >= limits.max_wait_batches
        ]
        fairness = min(
            overdue,
            key=lambda offer: (
                -waits[offer.candidate.intent_ref.object_id],
                execution.last_scheduled.get(offer.candidate.intent_ref.object_id, 0),
                offer.candidate.intent_ref.object_id,
                rank(offer),
            ),
            default=None,
        )
        selected: list[ActivationRecord] = []
        selected_inputs: set[str] = set()
        budget_blocked = False

        def overlap(offer: ActivationOffer) -> int:
            # Every view includes the root anchor. Sharing it alone does not imply that
            # independent branches compete; actual commit preconditions remain authoritative.
            return len(
                selected_inputs
                & {
                    ref.object_id
                    for ref in offer.candidate.input_refs
                    if ref.object_id != offer.view.root_intent.id
                }
            )

        remaining = list(ordered)
        while remaining and len(selected) < limits.max_concurrency:
            if not selected and fairness is not None and fairness in remaining:
                offer = fairness
            else:
                offer = min(
                    remaining, key=lambda item: (*rank(item)[:4], overlap(item), *rank(item)[4:])
                )
            remaining.remove(offer)
            try:
                record = self.reserve(
                    offer.candidate, offer.view, offer.spec, now, execution.deadline, writer
                )
            except BudgetExceeded:
                budget_blocked = True
                continue
            except CapacityExceeded:
                continue
            if record is not None:
                selected.append(
                    record.model_copy(
                        update={
                            "selection": SelectionReason(
                                policy="waiting_turn" if offer is fairness else "priority",
                                waited_batches=waits.get(offer.candidate.intent_ref.object_id, 0),
                                conflict=offer.candidate.conflict,
                                critical_gap=offer.critical_gap,
                                intent_priority=offer.candidate.intent_priority,
                                shared_inputs=overlap(offer),
                            )
                        }
                    )
                )
                selected_inputs.update(ref.object_id for ref in offer.candidate.input_refs)
        retry_at = min(retry_times) if retry_times else None
        if not selected:
            return BatchReservation(budget_blocked=budget_blocked, retry_at=retry_at)
        ordinal = len(execution.batches) + 1
        items = tuple(DispatchItem(**r.ref.model_dump(), source_id=r.spec.id) for r in selected)
        batch = DispatchBatch(
            id=digest([self.board_id, ordinal, [item.model_dump() for item in items]]),
            board_id=self.board_id,
            ordinal=ordinal,
            reserved_at=now,
            items=items,
        )
        selected_branches = {r.candidate.intent_ref.object_id for r in selected}
        eligible_branches = {
            offer.candidate.intent_ref.object_id
            for offer in ordered
            if offer.candidate.id in affordable
        }
        for record in selected:
            self.attempts[(record.candidate.id, record.attempt)] = record.model_copy(
                update={"batch_id": batch.id}
            )
        self.execution = self.require_run().model_copy(
            update={
                "batches": (*execution.batches, batch),
                "waiting_batches": {
                    branch: 0 if branch in selected_branches else waits.get(branch, 0) + 1
                    for branch in eligible_branches
                },
                "last_scheduled": {
                    **execution.last_scheduled,
                    **{branch: ordinal for branch in selected_branches},
                },
            }
        )
        return BatchReservation(batch=batch, budget_blocked=budget_blocked, retry_at=retry_at)

    def record_usage(self, activation_id: str, attempt: int, usage: UsageRecord) -> None:
        record = self.get(activation_id, attempt)
        key = (activation_id, attempt, usage.call_id)
        old = self.calls.get(key)
        if old is not None:
            if old == usage:
                return
            # A trusted receipt may complete partial usage once, without adding another call.
            if (
                old.usage_complete
                or not usage.usage_complete
                or any(
                    getattr(usage, name) < getattr(old, name)
                    for name in ("input_tokens", "output_tokens", "cached_tokens", "cost_usd")
                )
            ):
                raise BlackboardError("usage call id was reused with different content")
        # Late actual usage still belongs to the original attempt, even after it stopped.
        self.calls[key] = usage
        self.attempts[(activation_id, attempt)] = record.model_copy(
            update={
                "pending_calls": tuple(
                    call for call in record.pending_calls if call != usage.call_id
                )
            }
        )

    def save_output(
        self,
        activation_id: str,
        attempt: int,
        output: KnowledgeOutput,
        writer: WriterToken | None = None,
        now: datetime | None = None,
    ) -> None:
        record = self.check_attempt(
            ActivationRef(activation_id=activation_id, attempt=attempt), writer
        )
        if record.output is not None:
            if record.output != output:
                raise BlackboardError("attempt returned different outputs")
            return
        if record.attempt != self.get(activation_id).attempt or record.status != "running":
            raise BlackboardError("output belongs to an outdated or stopped attempt")
        self.attempts[(activation_id, attempt)] = record.model_copy(
            update={
                "status": "result_ready",
                "output": output,
                "output_at": now or datetime.now(UTC),
                "usage_complete": output.usage_complete
                and all(c.usage_complete for c in self.usage(activation_id, attempt))
                and not record.usage_uncertain
                and not record.pending_calls,
            }
        )

    def complete_usage(self, activation_id: str, attempt: int) -> None:
        record = self.get(activation_id, attempt)
        if record.status == "running":
            raise BlackboardError("cannot settle usage for a running attempt")
        self.attempts[(activation_id, attempt)] = record.model_copy(
            update={"usage_complete": True, "usage_uncertain": False, "pending_calls": ()}
        )

    def start_call(self, ref: ActivationRef, call_id: str, writer: WriterToken) -> None:
        record = self.check_attempt(ref, writer)
        if record.status != "running" or self.get(ref.activation_id).attempt != ref.attempt:
            raise BlackboardError("cannot start a call for a stopped or outdated attempt")
        if record.pending_calls or (ref.activation_id, ref.attempt, call_id) in self.calls:
            raise BlackboardError("a model call is unresolved or its identity was reused")
        self.attempts[(ref.activation_id, ref.attempt)] = record.model_copy(
            update={"pending_calls": (call_id,)}
        )

    def stop(
        self, ref: ActivationRef, *, reason: str, retry: bool, now: datetime | None = None
    ) -> None:
        """Reconcile an interrupted call without inventing an output or releasing unknown usage."""
        record = self.get(ref.activation_id, ref.attempt)
        if record.status != "running":
            return
        self.attempts[(ref.activation_id, ref.attempt)] = record.model_copy(
            update={"status": "result_ready"}
        )
        self.finish(
            ref.activation_id,
            ref.attempt,
            "retryable" if retry else "rejected",
            reason,
            False,
            0,
            now or datetime.now(UTC),
        )

    def finish(
        self,
        activation_id: str,
        attempt: int,
        status: FinishedStatus,
        reason: str,
        progress: bool,
        retry_delay_seconds: float,
        now: datetime,
        decision: ProgressDecision | None = None,
    ) -> None:
        if status not in {"completed", "no_progress", "rejected", "failed", "retryable"}:
            raise BlackboardError("finish requires a terminal or retryable attempt status")
        record = self.get(activation_id, attempt)
        if record.status != "result_ready":
            return
        if status == "retryable" and attempt >= record.spec.limits.max_attempts:
            status = "failed"
        self.attempts[(activation_id, attempt)] = ActivationRecord.model_validate(
            {
                **record.model_dump(),
                "status": status,
                "reason": reason,
                "progress": decision,
                "retry_at": now + timedelta(seconds=retry_delay_seconds)
                if status == "retryable"
                else None,
            }
        )
        execution = self.require_run()
        self.execution = execution.model_copy(
            update={
                "no_progress": 0
                if progress
                else execution.no_progress
                + (0 if decision and decision.kind == "exploration" else 1)
            }
        )

    def observe_progress(
        self,
        snapshot: BoardSnapshot,
        specs: tuple[KnowledgeSourceSpec, ...],
        now: datetime,
    ) -> ProgressState:
        execution = self.require_run()
        if snapshot.board_id != self.board_id:
            raise BlackboardError("progress snapshot belongs to a different board")
        if execution.progress_sources is not None and execution.progress_sources != specs:
            raise BlackboardError("progress source contracts cannot change during a run")
        summary = summarize(snapshot, specs, now)
        progress = execution.progress
        if progress is None:
            progress = initial(summary, execution.limits.max_explorations)
        elif (summary.board_revision, summary.evaluated_at) >= (
            progress.snapshot.board_revision,
            progress.snapshot.evaluated_at,
        ) and summary.model_dump(exclude={"evaluated_at"}) != progress.snapshot.model_dump(
            exclude={"evaluated_at"}
        ):
            progress, decision = advance(progress, summary, allow_exploration=False)
            if decision.kind == "goal_progress":
                execution = execution.model_copy(update={"no_progress": 0})
        self.execution = execution.model_copy(
            update={
                "progress": progress,
                "progress_sources": tuple(spec.model_copy(deep=True) for spec in specs),
            }
        )
        return progress

    def validate_settlement(
        self, request: CommitRequest, ref: ActivationRef, writer: WriterToken | None
    ) -> ActivationRecord:
        record = self.check_attempt(ref, writer)
        if (
            request.board_id != self.board_id
            or (writer is not None and writer.board_id != self.board_id)
            or self.get(ref.activation_id).attempt != ref.attempt
            or record.status not in {"result_ready", "completed", "no_progress"}
        ):
            raise BlackboardError("settlement belongs to a stopped, outdated or foreign attempt")
        delta, context = record.proposal(self.require_run().limits)
        if record.output_at is not None and record.output_at >= record.deadline:
            raise BlackboardError("settlement output was produced after the attempt deadline")
        if (
            request.idempotency_key != delta.idempotency_key
            or request.request_digest != commit_digest(delta, context)
        ):
            raise BlackboardError("commit does not match the persisted activation proposal")
        return record

    def settle(
        self,
        request: CommitRequest,
        ref: ActivationRef,
        writer: WriterToken | None,
        receipt: CommitReceipt,
        now: datetime,
        before: BoardSnapshot,
        after: BoardSnapshot,
        specs: tuple[KnowledgeSourceSpec, ...],
    ) -> None:
        record = self.validate_settlement(request, ref, writer)
        if (
            receipt.board_id != self.board_id
            or receipt.idempotency_key != request.idempotency_key
            or receipt.request_digest != request.request_digest
        ):
            raise BlackboardError("settlement receipt does not match the activation")
        if record.status != "result_ready":
            return
        if (
            before.board_id != self.board_id
            or after.board_id != self.board_id
            or before.revision + 1 != after.revision
            or after.revision != receipt.board_revision
        ):
            raise BlackboardError("progress must compare the exact committed board transition")
        progress = self.observe_progress(before, specs, now)
        baseline = progress.model_copy(update={"snapshot": summarize(before, specs, now)})
        updated, decision = advance(
            baseline,
            summarize(after, specs, now),
            allow_exploration=permits_exploration(before, after, specs, now),
        )
        if (progress.snapshot.board_revision, progress.snapshot.evaluated_at) > (
            updated.snapshot.board_revision,
            updated.snapshot.evaluated_at,
        ):
            # Historical receipt reconciliation must not roll back a newer progress view.
            updated = updated.model_copy(update={"snapshot": progress.snapshot})
        self.execution = self.require_run().model_copy(update={"progress": updated})
        self.finish(
            ref.activation_id,
            ref.attempt,
            "completed" if receipt.object_refs else "no_progress",
            record.output.reason,
            decision.kind == "goal_progress",
            0,
            now,
            decision=decision,
        )


@dataclass(frozen=True)
class ActivationSettlement:
    ledger: ActivationStore
    ref: ActivationRef
    clock: Callable[[], datetime]
    specs: tuple[KnowledgeSourceSpec, ...] = ()

    def check_store(self, store: BlackboardStore) -> None:
        self.ledger.bind(store)

    async def commit(self, request: CommitRequest, *, writer: WriterToken) -> CommitReceipt:
        return await self.ledger.commit_activation(
            request, self.ref, writer, self.clock, self.specs
        )


class ActivationStore(ABC):
    """Accounting contract; each adapter supplies a transaction over one board."""

    async def reconcile_terminal(self, board_id: str) -> bool:
        """Return true when the backend atomically reconciled a sealed board's ledger.

        Older single-process stores retain the controller's receipt-by-receipt recovery.
        Shared stores can provide a terminal-only operation without execution authority.
        """
        return False

    @abstractmethod
    def bind(self, store: BlackboardStore) -> None:
        """Require the same transactional board backend before a controller can execute."""
        ...

    def settlement(
        self,
        ref: ActivationRef,
        clock: Callable[[], datetime],
        specs: tuple[KnowledgeSourceSpec, ...] = (),
    ) -> ActivationSettlement:
        return ActivationSettlement(self, ref, clock, specs)

    @abstractmethod
    async def capture_progress(
        self,
        board_id: str,
        specs: tuple[KnowledgeSourceSpec, ...],
        writer: WriterToken,
        clock: Callable[[], datetime],
    ) -> ProgressState: ...

    async def reconcile_commit(
        self,
        ref: ActivationRef,
        receipt: CommitReceipt,
        before: BoardSnapshot,
        after: BoardSnapshot,
        specs: tuple[KnowledgeSourceSpec, ...],
        *,
        writer: WriterToken | None,
        now: datetime,
    ) -> None:
        """Complete historical two-transaction commits, including already sealed boards."""

        def reconcile(state: LedgerState) -> None:
            record = state.get(ref.activation_id, ref.attempt)
            delta, context = record.proposal(state.require_run().limits)

            def cannot_publish(_):
                raise BlackboardError("reconciliation cannot publish")

            request = CommitRequest(
                receipt.board_id,
                delta.idempotency_key,
                commit_digest(delta, context),
                cannot_publish,
            )
            state.settle(request, ref, writer, receipt, now, before, after, specs)

        await self._transaction(
            reconcile, activation_id=ref.activation_id, write=True, writer=writer
        )

    @abstractmethod
    async def commit_activation(
        self,
        request: CommitRequest,
        ref: ActivationRef,
        writer: WriterToken,
        clock: Callable[[], datetime],
        specs: tuple[KnowledgeSourceSpec, ...] = (),
    ) -> CommitReceipt: ...

    @abstractmethod
    async def _transaction(
        self,
        operation: Callable[[LedgerState], _Result],
        *,
        board_id: str | None = None,
        activation_id: str | None = None,
        write: bool = False,
        writer: WriterToken | None = None,
    ) -> _Result: ...

    async def start_run(
        self, board_id: str, limits: RunLimits, *, now: datetime | None = None
    ) -> RunExecution:
        return await self._transaction(
            lambda state: state.start(limits, now or datetime.now(UTC)),
            board_id=board_id,
            write=True,
        )

    async def run(self, board_id: str) -> RunExecution:
        return await self._transaction(lambda state: state.require_run(), board_id=board_id)

    async def set_wakeup(
        self, board_id: str, plan: WakeupPlan | None, *, writer: WriterToken | None
    ) -> None:
        if plan is not None:
            plan = WakeupPlan.model_validate(plan.model_dump())
            if writer is None:
                raise BlackboardError("scheduling a wake-up requires the current writer")

        def schedule(state: LedgerState) -> None:
            if state.execution is not None or plan is not None:
                state.execution = state.require_run().model_copy(update={"wakeup": plan})

        await self._transaction(schedule, board_id=board_id, write=True, writer=writer)

    async def bind_contract(self, board_id: str, revision: str, registry_digest: str) -> None:
        await self._transaction(
            lambda state: state.bind_contract(revision, registry_digest),
            board_id=board_id,
            write=True,
        )

    async def bind_thread(self, board_id: str, thread_id: str) -> None:
        await self._transaction(
            lambda state: state.bind_thread(thread_id), board_id=board_id, write=True
        )

    async def adopt(
        self, ref: ActivationRef, writer: WriterToken, *, recovering: bool = False
    ) -> ActivationRecord:
        return await self._transaction(
            lambda state: state.adopt(ref, writer, recovering=recovering),
            activation_id=ref.activation_id,
            write=True,
            writer=writer,
        )

    async def begin(self, ref: ActivationRef, writer: WriterToken) -> ActivationRecord:
        return await self._transaction(
            lambda state: state.begin(ref, writer),
            activation_id=ref.activation_id,
            write=True,
            writer=writer,
        )

    async def stop(
        self,
        ref: ActivationRef,
        *,
        reason: str,
        retry: bool,
        writer: WriterToken | None = None,
        now: datetime | None = None,
    ) -> None:
        def stop(state: LedgerState) -> None:
            state.check_attempt(ref, writer)
            state.stop(ref, reason=reason, retry=retry, now=now)

        await self._transaction(
            stop,
            activation_id=ref.activation_id,
            write=True,
            writer=writer,
        )

    async def remember_candidates(
        self, board_id: str, candidate_ids: tuple[str, ...]
    ) -> dict[str, int]:
        return await self._transaction(
            lambda state: state.remember(candidate_ids), board_id=board_id, write=True
        )

    async def records(self, board_id: str) -> tuple[ActivationRecord, ...]:
        return await self._transaction(lambda state: state.records(), board_id=board_id)

    async def attempts(self, activation_id: str) -> tuple[ActivationRecord, ...]:
        return await self._transaction(
            lambda state: tuple(
                record
                for (identity, _), record in sorted(state.attempts.items())
                if identity == activation_id
            ),
            activation_id=activation_id,
        )

    async def get(self, activation_id: str, attempt: int | None = None) -> ActivationRecord:
        return await self._transaction(
            lambda state: state.get(activation_id, attempt), activation_id=activation_id
        )

    async def usage(self, activation_id: str, attempt: int) -> tuple[UsageRecord, ...]:
        return await self._transaction(
            lambda state: state.usage(activation_id, attempt), activation_id=activation_id
        )

    async def summary(self, board_id: str) -> UsageSummary:
        return await self._transaction(lambda state: state.summary(), board_id=board_id)

    async def reserve(
        self,
        candidate: ActivationCandidate,
        view: BoardView,
        spec: KnowledgeSourceSpec,
        limits: RunLimits,
        *,
        now: datetime | None = None,
        run_deadline: datetime | None = None,
        writer: WriterToken | None = None,
    ) -> ActivationRecord | None:
        now = now or datetime.now(UTC)
        await self.start_run(candidate.board_id, limits, now=now)
        return await self._transaction(
            lambda state: state.reserve(candidate, view, spec, now, run_deadline, writer),
            board_id=candidate.board_id,
            write=True,
            writer=writer,
        )

    async def reserve_batch(
        self,
        board_id: str,
        offers: tuple[ActivationOffer, ...],
        limits: RunLimits,
        *,
        now: datetime | None = None,
        writer: WriterToken | None = None,
    ) -> BatchReservation:
        now = now or datetime.now(UTC)
        offers = tuple(ActivationOffer.model_validate(offer.model_dump()) for offer in offers)

        def reserve(state: LedgerState) -> BatchReservation:
            state.start(limits, now)
            return state.reserve_batch(offers, now, writer)

        return await self._transaction(reserve, board_id=board_id, write=True, writer=writer)

    async def record_usage(self, activation_id: str, attempt: int, usage: UsageRecord) -> None:
        usage = UsageRecord.model_validate(usage.model_dump())
        await self._usage_transaction(
            lambda state: state.record_usage(activation_id, attempt, usage),
            activation_id=activation_id,
        )

    async def start_call(self, ref: ActivationRef, call_id: str, writer: WriterToken) -> None:
        await self._transaction(
            lambda state: state.start_call(ref, call_id, writer),
            activation_id=ref.activation_id,
            write=True,
            writer=writer,
        )

    async def save_output(
        self,
        activation_id: str,
        attempt: int,
        output: KnowledgeOutput,
        *,
        writer: WriterToken | None = None,
        now: datetime | None = None,
    ) -> None:
        output = KnowledgeOutput.model_validate(output.model_dump())
        await self._transaction(
            lambda state: state.save_output(activation_id, attempt, output, writer, now),
            activation_id=activation_id,
            write=True,
            writer=writer,
        )

    async def complete_usage(self, activation_id: str, attempt: int) -> None:
        """Release an uncertain reservation only after trusted usage reconciliation."""
        await self._usage_transaction(
            lambda state: state.complete_usage(activation_id, attempt),
            activation_id=activation_id,
        )

    async def _usage_transaction(
        self, operation: Callable[[LedgerState], _Result], *, activation_id: str
    ) -> _Result:
        """Persist actual costs even when the execution authority has been revoked."""
        return await self._transaction(operation, activation_id=activation_id, write=True)

    async def finish(
        self,
        activation_id: str,
        *,
        attempt: int,
        status: FinishedStatus,
        reason: str,
        progress: bool,
        retry_delay_seconds: float = 0,
        now: datetime | None = None,
        writer: WriterToken | None = None,
    ) -> None:
        def finish(state: LedgerState) -> None:
            state.check_attempt(ActivationRef(activation_id=activation_id, attempt=attempt), writer)
            state.finish(
                activation_id,
                attempt,
                status,
                reason,
                progress,
                retry_delay_seconds,
                now or datetime.now(UTC),
            )

        await self._transaction(
            finish,
            activation_id=activation_id,
            write=True,
            writer=writer,
        )


@dataclass
class MemoryLedgerData:
    boards: dict[str, LedgerState] = field(default_factory=dict)
    activation_boards: dict[str, str] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class MemoryActivationStore(ActivationStore):
    def __init__(self, board_store: BlackboardStore | None = None) -> None:
        self._data = MemoryLedgerData()
        self.board_store: BlackboardStore | None = None
        if board_store is not None:
            self.bind(board_store)

    def bind(self, store: BlackboardStore) -> None:
        from work_harness.storage.blackboard_memory import MemoryBlackboardStore

        if not isinstance(store, MemoryBlackboardStore) or self.board_store not in (None, store):
            raise BlackboardError("memory activation ledger requires the same memory board store")
        if store._activation_data is None:
            store._activation_data = self._data
        elif store._activation_data is not self._data:
            if self._data.boards:
                raise BlackboardError("cannot merge independent populated activation ledgers")
            self._data = store._activation_data
        self.board_store = store

    async def commit_activation(
        self,
        request: CommitRequest,
        ref: ActivationRef,
        writer: WriterToken,
        clock: Callable[[], datetime],
        specs: tuple[KnowledgeSourceSpec, ...] = (),
    ) -> CommitReceipt:
        from work_harness.storage.blackboard_memory import MemoryBlackboardStore

        store = self.board_store
        if not isinstance(store, MemoryBlackboardStore):
            raise BlackboardError("activation ledger has no compatible board transaction")
        # All ledger writers take this lock first; no board operation takes them in reverse.
        async with self._data.lock, store._lock:
            state = deepcopy(self._data.boards.get(request.board_id, LedgerState(request.board_id)))
            record = state.validate_settlement(request, ref, writer)
            if (
                record.status != "result_ready"
                and (request.board_id, request.idempotency_key) not in store._receipts
            ):
                raise BlackboardError("completed activation has no commit receipt")
            before = store._boards[request.board_id]
            receipt, after, publish = store.prepare_commit(request, writer=writer)
            if before.revision >= receipt.board_revision:
                before = store.snapshot_in_transaction(
                    request.board_id, revision=receipt.board_revision - 1
                )
                after = store.snapshot_in_transaction(
                    request.board_id, revision=receipt.board_revision
                )
            state.settle(request, ref, writer, receipt, receipt.committed_at, before, after, specs)
            # Prepare every allocation before publishing either side. No await or validation
            # remains between these assignments, so neither readers nor cancellation interleave.
            boards = {**self._data.boards, request.board_id: deepcopy(state)}
            result = receipt.model_copy(deep=True)
            publish()
            self._data.boards = boards
            return result

    async def capture_progress(
        self,
        board_id: str,
        specs: tuple[KnowledgeSourceSpec, ...],
        writer: WriterToken,
        clock: Callable[[], datetime],
    ) -> ProgressState:
        from work_harness.blackboard.control import check_writer
        from work_harness.storage.blackboard_memory import MemoryBlackboardStore

        store = self.board_store
        if not isinstance(store, MemoryBlackboardStore):
            raise BlackboardError("progress requires a compatible board transaction")
        async with self._data.lock, store._lock:
            check_writer(store._control(board_id), writer, store._session_id)
            state = deepcopy(self._data.boards.get(board_id, LedgerState(board_id)))
            result = state.observe_progress(store._boards[board_id], specs, clock())
            self._data.boards[board_id] = state
            return result.model_copy(deep=True)

    async def _transaction(
        self,
        operation: Callable[[LedgerState], _Result],
        *,
        board_id: str | None = None,
        activation_id: str | None = None,
        write: bool = False,
        writer: WriterToken | None = None,
    ) -> _Result:
        async with self._data.lock:
            if writer is not None:
                if self.board_store is None:
                    raise BlackboardError("writer validation requires a board store")
                await self.board_store.check_writer(writer)
            if activation_id is not None:
                board_id = self._data.activation_boards.get(activation_id)
            if board_id is None:
                raise BlackboardError("unknown activation attempt")
            if writer is not None and writer.board_id != board_id:
                raise BlackboardError("writer belongs to a different board")
            state = self._data.boards.get(board_id, LedgerState(board_id))
            if write:
                state = deepcopy(state)
            result = operation(state)
            if write:
                self._data.boards[board_id] = deepcopy(state)
                for identity, _ in state.attempts:
                    self._data.activation_boards[identity] = board_id
            return deepcopy(result)
