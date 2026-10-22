"""Time-driven progress, durable waiting and proof invalidation without model polling."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from test_blackboard_execution_storage import setup
from test_blackboard_recovery import CONFIG, Crash

from work_harness.blackboard.controller import BLACKBOARD_WORKFLOW_REVISION, BlackboardController
from work_harness.blackboard.execution import RunLimits
from work_harness.blackboard.models import (
    AcceptanceCriterion,
    Evidence,
    Fact,
    FactContent,
    FactProposal,
    HintProposal,
    Intent,
    IntentContent,
    IntentProposal,
    Link,
    digest,
)
from work_harness.blackboard.protocol import (
    BlackboardDelta,
    BlackboardError,
    CommitContext,
    StaleWriterError,
)
from work_harness.knowledge_sources.adapters.callback import RuleKnowledgeSource
from work_harness.knowledge_sources.base import (
    ActivationRule,
    FactQuery,
    KnowledgeOutput,
    KnowledgeSourceSpec,
    SourceLimits,
    UsageRecord,
)
from work_harness.knowledge_sources.registry import KnowledgeSourceRegistry
from work_harness.storage.blackboard_execution_sqlite import SQLiteActivationStore
from work_harness.storage.blackboard_sqlite import SQLiteBlackboardStore
from work_harness.workflow.blackboard_runner import BlackboardRunner

LIMITS = RunLimits(token_budget=500, timeout_seconds=120, retry_delay_seconds=2)


@dataclass
class Clock:
    now: datetime = datetime(2026, 10, 21, tzinfo=UTC)
    waits: list[float] = field(default_factory=list)

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += timedelta(seconds=seconds)
        await asyncio.sleep(0)


def criterion(key):
    return AcceptanceCriterion(
        id=key,
        description=f"Verify {key}",
        claim_key=key,
        claim_type="tool_observation",
        operator="equals",
        expected=42,
        required_checks=("checked",),
    )


async def timed(store, clock, *, create=True, goals=("answer",)):
    execution = await setup(store, create=False)
    execution.kernel.clock = clock
    execution.now = clock()
    if create:
        await execution.kernel.create(
            "board",
            IntentContent(
                topic="answer",
                objective="Verify the answer",
                acceptance=tuple(criterion(key) for key in goals),
            ),
            source_id="user",
        )
    return execution


async def proposal(execution, *, key="answer", value=42, name=None, links=None, **kwargs):
    board = await execution.store.snapshot("board")
    proof = {"value": value}
    content = FactContent(
        topic=key,
        statement=f"Verified {key} is {value}",
        claim_key=key,
        claim_type="tool_observation",
        value=value,
        evidence=(
            Evidence(
                source="tool",
                locator=f"fixture:{key}",
                content_digest=digest(proof),
                description="Trusted observation",
            ),
        ),
        links=links
        if links is not None
        else (Link(relation="contributes_to", target=board.by_id()[board.root_intent_id].ref),),
        **kwargs,
    )
    receipt = await execution.kernel.verification.attest(
        "board", content, verifier_id="fixture", evidence=proof
    )
    return FactProposal(
        **content.model_dump(), local_id=name or key, verification_receipt_id=receipt.receipt_id
    )


async def submit(execution, *proposals):
    board = await execution.store.snapshot("board")
    receipt = await execution.kernel.commit(
        "board",
        BlackboardDelta(
            expected_revision=board.revision,
            idempotency_key=f"seed:{board.revision}",
            proposals=proposals,
        ),
        context=CommitContext(source_id="fixture"),
    )
    board = await execution.store.snapshot("board")
    return {key: board.by_id()[ref.object_id] for key, ref in receipt.local_refs.items()}


def source_spec(*, rule=None, attempts=2, timeout=30):
    return KnowledgeSourceSpec(
        id="observe",
        version="1",
        emitted_kinds=("fact", "hint"),
        adapter_type="rule",
        activation_rule=rule or ActivationRule(reason="Verify after the time condition"),
        limits=SourceLimits(token_budget=50, timeout_seconds=timeout, max_attempts=attempts),
    )


def controller(execution, clock, *, work=None, source=None, sleep=None, limits=LIMITS):
    registry = KnowledgeSourceRegistry()
    if work is not None:
        registry.register(RuleKnowledgeSource(source or source_spec(), work))
    return BlackboardController(
        execution.kernel,
        registry,
        ledger=execution.ledger,
        limits=limits,
        sleep=sleep or clock.sleep,
    )


async def test_future_fact_satisfies_goal_without_a_source_or_new_input(blackboard_store):
    clock = Clock()
    execution = await timed(blackboard_store, clock)
    start = clock.now
    created = await submit(
        execution, await proposal(execution, valid_from=start + timedelta(seconds=2))
    )
    assert created["answer"].lifecycle == "valid"
    ctrl = controller(execution, clock)
    observed = []

    async def sleep(seconds):
        observed.append((await execution.ledger.run("board")).wakeup)
        await clock.sleep(seconds)

    ctrl.sleep = sleep
    result = await BlackboardRunner(ctrl).run("board")
    assert result["status"] == "succeeded"
    assert clock.now == start + timedelta(seconds=2) and clock.waits == [1, 1]
    assert all(
        plan.next_at == clock.now and plan.causes[0].kind == "fact_start" for plan in observed
    )
    assert (await execution.ledger.summary("board")).attempts == 0
    assert (await execution.ledger.run("board")).wakeup is None


async def test_future_input_activates_a_source_when_it_becomes_effective(blackboard_store):
    clock = Clock()
    execution = await timed(blackboard_store, clock)
    at = clock.now + timedelta(seconds=2)
    await submit(execution, await proposal(execution, key="operand", valid_from=at))
    calls = []

    async def work(invocation, view):
        calls.append(clock.now)
        assert any(isinstance(item, Fact) and item.claim_key == "operand" for item in view.objects)
        return KnowledgeOutput(proposals=(await proposal(execution),))

    ctrl = controller(
        execution,
        clock,
        work=work,
        source=source_spec(
            rule=ActivationRule(
                required_facts=(FactQuery(claim_key="operand"),),
                reason="Read the now-effective input",
            )
        ),
    )
    assert (await BlackboardRunner(ctrl).run("board"))["status"] == "succeeded"
    assert calls == [at]


async def test_hint_expiry_wakes_matching_without_an_external_commit(blackboard_store):
    clock = Clock()
    execution = await timed(blackboard_store, clock)
    board = await execution.store.snapshot("board")
    at = clock.now + timedelta(seconds=3)
    hints = await submit(
        execution,
        HintProposal(
            local_id="hold",
            topic="hold",
            suggestion="Wait for the observation window",
            applies_when="This window is closed",
            rationale="An observation will be available later",
            expires_at=at,
            links=(
                Link(relation="contributes_to", target=board.by_id()[board.root_intent_id].ref),
            ),
        ),
    )
    calls = []

    async def work(invocation, view):
        calls.append(clock.now)
        return KnowledgeOutput(proposals=(await proposal(execution),))

    ctrl = controller(
        execution,
        clock,
        work=work,
        source=source_spec(
            rule=ActivationRule(
                absent_hint_topics=("hold",),
                reason="The waiting hint has expired",
            )
        ),
    )
    assert (await BlackboardRunner(ctrl).run("board"))["status"] == "succeeded"
    final = await execution.store.snapshot("board")
    assert final.by_id()[hints["hold"].id].status == "expired"
    assert calls == [at]


async def test_retry_backoff_uses_persisted_time_and_the_same_clock(blackboard_store):
    clock = Clock()
    execution = await timed(blackboard_store, clock)
    start = clock.now
    calls, waits = [], []

    async def work(invocation, view):
        calls.append((invocation.record.candidate.id, invocation.record.attempt, clock.now))
        await invocation.record_usage(UsageRecord(call_id="read", input_tokens=7))
        if invocation.record.attempt == 1:
            return KnowledgeOutput(status="retryable_error", reason="temporary failure")
        return KnowledgeOutput(proposals=(await proposal(execution),))

    async def sleep(seconds):
        plan = (await execution.ledger.run("board")).wakeup
        waits.append(plan)
        assert len(calls) == 1
        await clock.sleep(seconds)

    ctrl = controller(execution, clock, work=work, sleep=sleep)
    assert (await BlackboardRunner(ctrl).run("board"))["status"] == "succeeded"
    assert calls[0][:2] == (calls[1][0], 1) and calls[1][1] == 2
    assert calls[1][2] == start + timedelta(seconds=2)
    assert all(plan.causes[0].kind == "retry" and plan.next_at == calls[1][2] for plan in waits)
    summary = await execution.ledger.summary("board")
    assert summary.tokens == 14 and summary.reserved_tokens == 0
    assert (await execution.ledger.run("board")).deadline == start + timedelta(seconds=120)


async def test_expiry_invalidates_proof_and_reopens_child_but_preserves_independent_fact(
    blackboard_store,
):
    clock = Clock()
    execution = await timed(blackboard_store, clock, goals=("answer", "delivery"))
    board = await execution.store.snapshot("board")
    root = board.by_id()[board.root_intent_id]
    child = (
        await submit(
            execution,
            IntentProposal(
                local_id="child",
                topic="answer",
                objective="Verify answer",
                root_intent_ref=root.ref,
                parent_intent_ref=root.ref,
                acceptance=(criterion("answer"),),
                links=(Link(relation="contributes_to", target=root.ref),),
            ),
        )
    )["child"]
    initial = await submit(
        execution,
        await proposal(execution, key="premise", valid_until=clock.now + timedelta(seconds=2)),
        await proposal(execution, key="independent"),
    )
    derived = (
        await submit(
            execution,
            await proposal(
                execution,
                premise_refs=(initial["premise"].ref,),
                links=(Link(relation="contributes_to", target=child.ref),),
            ),
        )
    )["answer"]
    assert (await execution.store.snapshot("board")).by_id()[child.id].status == "satisfied"
    ctrl = controller(execution, clock)
    assert (await BlackboardRunner(ctrl).run("board"))["status"] == "partial"
    objects = (await execution.store.snapshot("board")).by_id()
    assert objects[initial["premise"].id].lifecycle == "expired"
    assert objects[derived.id].lifecycle == "stale"
    assert objects[child.id].status == "open" and not objects[child.id].satisfied_by
    assert objects[initial["independent"].id] == initial["independent"]


async def test_future_derived_fact_is_not_stale_just_because_its_own_start_is_future(
    blackboard_store,
):
    clock = Clock()
    execution = await timed(blackboard_store, clock)
    premise = (await submit(execution, await proposal(execution, key="premise")))["premise"]
    answer = (
        await submit(
            execution,
            await proposal(
                execution,
                valid_from=clock.now + timedelta(seconds=2),
                premise_refs=(premise.ref,),
            ),
        )
    )["answer"]
    assert answer.lifecycle == "valid"
    assert (await BlackboardRunner(controller(execution, clock)).run("board"))[
        "status"
    ] == "succeeded"
    assert (await execution.store.snapshot("board")).by_id()[answer.id].lifecycle == "valid"


async def test_future_conflict_is_applied_at_its_start_and_only_once(blackboard_store):
    clock = Clock()
    execution = await timed(blackboard_store, clock)
    facts = await submit(
        execution,
        await proposal(execution, key="input", name="now"),
        await proposal(
            execution,
            key="input",
            value=0,
            name="future",
            valid_from=clock.now + timedelta(seconds=2),
        ),
    )
    assert all(fact.lifecycle == "valid" for fact in facts.values())
    ctrl = controller(execution, clock)
    assert (await BlackboardRunner(ctrl).run("board"))["status"] == "failed"
    board = await execution.store.snapshot("board")
    assert all(board.by_id()[fact.id].lifecycle == "contested" for fact in facts.values())
    assert len([item for item in board.objects if isinstance(item, Intent) and item.blockers]) == 1


async def test_input_expiry_during_batch_rejects_old_proof_and_keeps_usage(blackboard_store):
    clock = Clock()
    execution = await timed(blackboard_store, clock)
    facts = await submit(
        execution,
        await proposal(execution, key="operand", valid_until=clock.now + timedelta(seconds=1)),
        await proposal(execution, key="independent"),
    )

    async def work(invocation, view):
        clock.now += timedelta(seconds=2)
        await invocation.record_usage(UsageRecord(call_id="read", input_tokens=7))
        return KnowledgeOutput(
            proposals=(await proposal(execution, premise_refs=(facts["operand"].ref,)),)
        )

    ctrl = controller(
        execution,
        clock,
        work=work,
        source=source_spec(
            attempts=1,
            rule=ActivationRule(
                required_facts=(FactQuery(claim_key="operand"),),
                reason="Derive from the observation",
            ),
        ),
    )
    assert (await BlackboardRunner(ctrl).run("board"))["status"] == "partial"
    objects = (await execution.store.snapshot("board")).by_id()
    assert objects[facts["operand"].id].lifecycle == "expired"
    assert objects[facts["independent"].id] == facts["independent"]
    assert not any(
        isinstance(item, Fact) and item.claim_key == "answer" for item in objects.values()
    )
    assert (await execution.ledger.summary("board")).tokens == 7


async def test_late_output_does_not_become_a_fact_even_if_wall_clock_timeout_has_not_fired(
    blackboard_store,
):
    clock = Clock()
    execution = await timed(blackboard_store, clock)

    async def work(invocation, view):
        clock.now += timedelta(seconds=31)
        await invocation.record_usage(UsageRecord(call_id="read", input_tokens=7))
        return KnowledgeOutput(proposals=(await proposal(execution),))

    ctrl = controller(execution, clock, work=work, source=source_spec(attempts=1))
    assert (await BlackboardRunner(ctrl).run("board"))["status"] == "failed"
    record = (await execution.ledger.records("board"))[0]
    assert record.output_at == clock.now and record.reason == "attempt_timeout"
    assert record.output.proposals and record.status == "failed"
    assert not any(
        isinstance(item, Fact) for item in (await execution.store.snapshot("board")).objects
    )
    assert (await execution.ledger.summary("board")).tokens == 7


@pytest.mark.parametrize("action", ("cancel", "input", "takeover"))
async def test_wait_observes_cancel_input_and_owner_change(blackboard_store, action):
    clock = Clock()
    execution = await timed(blackboard_store, clock)
    await submit(
        execution,
        await proposal(execution, key="future", valid_from=clock.now + timedelta(seconds=20)),
    )

    async def sleep(seconds):
        assert not clock.waits
        if action == "cancel":
            await execution.kernel.cancel("board")
        elif action == "input":
            await submit(execution, await proposal(execution))
        else:
            await execution.kernel.writer("board", takeover=True)
        await clock.sleep(seconds)

    ctrl = controller(execution, clock, sleep=sleep)
    if action == "takeover":
        with pytest.raises(StaleWriterError):
            await BlackboardRunner(ctrl).run("board")
    else:
        result = await BlackboardRunner(ctrl).run("board")
        assert result["status"] == ("cancelled" if action == "cancel" else "succeeded")
        assert (await execution.ledger.run("board")).wakeup is None
    assert len(clock.waits) == 1 and (await execution.ledger.summary("board")).attempts == 0


async def test_time_crossing_after_matching_is_not_lost_before_wait_is_saved(
    blackboard_store, monkeypatch
):
    clock = Clock()
    execution = await timed(blackboard_store, clock)
    await submit(
        execution,
        await proposal(execution, key="operand", valid_from=clock.now + timedelta(seconds=1)),
    )
    calls = []

    async def work(invocation, view):
        calls.append(clock.now)
        return KnowledgeOutput(proposals=(await proposal(execution),))

    ctrl = controller(
        execution,
        clock,
        work=work,
        source=source_spec(
            rule=ActivationRule(
                required_facts=(FactQuery(claim_key="operand"),),
                reason="Use future observation",
            )
        ),
    )
    original = ctrl.registry.discover

    def discover(*args):
        result = original(*args)
        if not result:
            clock.now += timedelta(seconds=2)
        return result

    monkeypatch.setattr(ctrl.registry, "discover", discover)
    assert (await BlackboardRunner(ctrl).run("board"))["status"] == "succeeded"
    assert len(calls) == 1 and not clock.waits


@asynccontextmanager
async def durable(path, clock, *, create=False, sleep=None, work=None, source=None):
    store = SQLiteBlackboardStore(path / "board.sqlite")
    await store.initialize()
    try:
        execution = await timed(store, clock, create=create)
        ctrl = controller(execution, clock, sleep=sleep, work=work, source=source)
        async with AsyncSqliteSaver.from_conn_string(str(path / "checkpoints.sqlite")) as saver:
            yield execution, BlackboardRunner(ctrl, checkpointer=saver)
    finally:
        await store.close()


@pytest.mark.parametrize("lose_schedule", (False, True))
async def test_restart_rebuilds_overdue_wakeup_and_preserves_original_deadline(
    tmp_path, lose_schedule
):
    clock = Clock()

    async def crash(seconds):
        raise Crash("waiting")

    async with durable(tmp_path, clock, create=True, sleep=crash) as (execution, runner):
        await submit(
            execution, await proposal(execution, valid_from=clock.now + timedelta(seconds=10))
        )
        with pytest.raises(Crash):
            await runner.run("board", CONFIG)
        run = await execution.ledger.run("board")
        assert run.wakeup.next_at == clock.now + timedelta(seconds=10)
        checkpoint = await runner.graph.aget_state(CONFIG)
        assert checkpoint.next == ("wait",)
    if lose_schedule:
        with closing(sqlite3.connect(tmp_path / "board.sqlite")) as connection, connection:
            payload = json.loads(
                connection.execute("SELECT payload FROM board_control").fetchone()[0]
            )
            payload["wakeup"] = None
            connection.execute("UPDATE board_control SET payload = ?", (json.dumps(payload),))
    clock.now += timedelta(seconds=11)
    async with durable(tmp_path, clock) as (execution, runner):
        assert (await runner.run("board", CONFIG))["status"] == "succeeded"
        restored = await execution.ledger.run("board")
        assert restored.deadline == run.deadline and restored.wakeup is None
        assert (await execution.ledger.summary("board")).attempts == 0
        assert not clock.waits


async def test_completed_in_time_output_can_commit_after_restart_past_attempt_deadline(
    tmp_path, monkeypatch
):
    clock = Clock()

    async with durable(tmp_path, clock, create=True) as (execution, _):

        async def work(invocation, view):
            await invocation.record_usage(UsageRecord(call_id="read", input_tokens=7))
            return KnowledgeOutput(proposals=(await proposal(execution),))

        ctrl = controller(execution, clock, work=work)
        original = execution.ledger.save_output

        async def save(*args, **kwargs):
            await original(*args, **kwargs)
            raise Crash("output_saved")

        monkeypatch.setattr(execution.ledger, "save_output", save)
        with pytest.raises(Crash):
            await BlackboardRunner(ctrl).run("board")
        record = (await execution.ledger.records("board"))[0]
        assert record.output_at == clock.now
    clock.now += timedelta(seconds=31)

    async def never(invocation, view):
        pytest.fail("completed work must not run again")

    async with durable(tmp_path, clock, work=never) as (execution, runner):
        assert (await runner.run("board", CONFIG))["status"] == "succeeded"
        assert (await execution.ledger.get(record.candidate.id)).attempt == 1
        assert (await execution.ledger.summary("board")).tokens == 7


async def test_v6_ledger_reads_without_timer_fields_but_requires_compatible_workflow(tmp_path):
    clock = Clock()
    async with durable(tmp_path, clock, create=True) as (execution, runner):
        await runner.controller.bootstrap({"board_id": "board"})
    with closing(sqlite3.connect(tmp_path / "board.sqlite")) as connection, connection:
        payload = json.loads(connection.execute("SELECT payload FROM board_control").fetchone()[0])
        payload.pop("wakeup")
        payload["workflow_revision"] = "blackboard-v4"
        connection.execute("UPDATE board_control SET payload = ?", (json.dumps(payload),))
        connection.execute("UPDATE blackboard_schema SET version = 6")
    async with durable(tmp_path, clock) as (execution, runner):
        ledger = SQLiteActivationStore(execution.store)
        assert (await ledger.run("board")).wakeup is None
        with pytest.raises(BlackboardError, match="incompatible"):
            await ledger.bind_contract("board", BLACKBOARD_WORKFLOW_REVISION, digest([]))


async def test_restart_assesses_a_missed_start_before_waiting_for_a_later_expiry(tmp_path):
    clock = Clock()
    calls = []

    async def crash(seconds):
        raise Crash("waiting")

    async def work(invocation, view):
        calls.append(clock.now)
        return KnowledgeOutput(proposals=(await proposal(execution),))

    source = source_spec(
        rule=ActivationRule(
            required_facts=(FactQuery(claim_key="operand"),),
            reason="Use the time-dependent observation",
        )
    )
    async with durable(tmp_path, clock, create=True, sleep=crash, work=work, source=source) as (
        execution,
        runner,
    ):
        await submit(
            execution,
            await proposal(execution, key="operand", valid_from=clock.now + timedelta(seconds=10)),
            await proposal(
                execution, key="independent", valid_until=clock.now + timedelta(seconds=20)
            ),
        )
        with pytest.raises(Crash):
            await runner.run("board", CONFIG)
        before = await execution.store.snapshot("board")
    clock.now += timedelta(seconds=11)
    async with durable(tmp_path, clock, work=work, source=source) as (execution, runner):
        # The first event changes query visibility without changing an object's lifecycle.
        assert (await execution.store.snapshot("board")).revision == before.revision
        assert (await runner.run("board", CONFIG))["status"] == "succeeded"
        assert calls == [clock.now] and not clock.waits
        assert clock.now.second == 11


async def test_run_deadline_stops_a_wait_before_any_retry_can_execute(blackboard_store):
    clock = Clock()
    execution = await timed(blackboard_store, clock)
    calls = []

    async def work(invocation, view):
        calls.append(clock.now)
        await invocation.record_usage(UsageRecord(call_id="failed-read", input_tokens=7))
        return KnowledgeOutput(status="retryable_error", reason="retry later")

    async def sleep(seconds):
        await clock.sleep(5)

    ctrl = controller(
        execution,
        clock,
        work=work,
        sleep=sleep,
        limits=LIMITS.model_copy(update={"timeout_seconds": 3}),
    )
    result = await BlackboardRunner(ctrl).run("board")
    assert result["status"] == "failed" and result["stop_reason"] == "timeout"
    assert len(calls) == 1
    summary = await execution.ledger.summary("board")
    assert summary.tokens == 7 and summary.attempts == 1 and summary.reserved_tokens == 0


async def test_time_condition_beyond_run_deadline_does_not_keep_run_alive(blackboard_store):
    clock = Clock()
    execution = await timed(blackboard_store, clock)
    await submit(
        execution, await proposal(execution, valid_from=clock.now + timedelta(seconds=200))
    )
    result = await BlackboardRunner(controller(execution, clock)).run("board")
    assert result["status"] == "failed" and result["stop_reason"] == "blocked"
    assert not clock.waits


async def test_final_time_recheck_does_not_override_explicit_cancellation(blackboard_store):
    clock = Clock()
    execution = await timed(blackboard_store, clock)
    ctrl = controller(execution, clock)
    await ctrl.bootstrap({"board_id": "board"})
    await submit(execution, await proposal(execution))
    result = await ctrl.finish(
        {"board_id": "board", "status": "cancelled", "stop_reason": "user_cancelled"}
    )
    assert result["status"] == "cancelled"
    assert (await execution.ledger.run("board")).wakeup is None
