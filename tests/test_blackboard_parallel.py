"""Bounded batches, branch fairness, independent commits and partial batch recovery."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager, closing, suppress
from datetime import timedelta

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from test_blackboard_execution_storage import setup
from test_blackboard_recovery import CONFIG, ROOT

from work_harness.blackboard.activation import make_view, match_rule
from work_harness.blackboard.controller import BLACKBOARD_WORKFLOW_REVISION, BlackboardController
from work_harness.blackboard.execution import ActivationOffer, LedgerState, RunLimits
from work_harness.blackboard.models import (
    AcceptanceCriterion,
    Fact,
    FactProposal,
    Hint,
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
from work_harness.domain.models import ModelResponse, Usage
from work_harness.knowledge_sources.adapters.agent import AgentKnowledgeSource
from work_harness.knowledge_sources.adapters.callback import RuleKnowledgeSource
from work_harness.knowledge_sources.base import (
    ActivationRule,
    KnowledgeOutput,
    KnowledgeSourceSpec,
    SourceLimits,
    UsageRecord,
)
from work_harness.knowledge_sources.execution import SourceInput, SourceOutput, SourceState
from work_harness.knowledge_sources.registry import KnowledgeSourceRegistry
from work_harness.llm.mock import MockProvider
from work_harness.observability.trace import TraceRecorder
from work_harness.runtime import build_runtime
from work_harness.storage.blackboard_execution_sqlite import SQLiteActivationStore
from work_harness.storage.blackboard_factory import build_activation_store
from work_harness.storage.blackboard_sqlite import SQLiteBlackboardStore
from work_harness.tools.gateway import ToolGateway
from work_harness.workflow.agent_services import AgentServices
from work_harness.workflow.agents import WorkerAgent
from work_harness.workflow.blackboard_runner import BlackboardRunner

LIMITS = RunLimits(
    token_budget=1000,
    cost_budget_usd=1,
    timeout_seconds=300,
    max_concurrency=2,
    max_per_intent=1,
    max_wait_batches=2,
    retry_delay_seconds=0,
)


def criterion(name):
    return AcceptanceCriterion(
        id=name,
        description=f"Verify {name}",
        claim_key=name,
        claim_type="tool_observation",
        operator="equals",
        expected=42,
        required_checks=("checked",),
    )


async def branching(store, names=("a", "b", "c"), *, create=True):
    execution = await setup(store, create=False)
    if create:
        board = await execution.kernel.create(
            "board",
            IntentContent(
                topic="goal",
                objective="Verify each independent input",
                acceptance=tuple(criterion(name) for name in names),
            ),
            source_id="user",
        )
        root = board.by_id()[board.root_intent_id]
        await execution.kernel.commit(
            "board",
            BlackboardDelta(
                expected_revision=board.revision,
                idempotency_key="branches",
                proposals=tuple(
                    IntentProposal(
                        local_id=name,
                        topic=name,
                        objective=f"Obtain input {name}",
                        root_intent_ref=root.ref,
                        parent_intent_ref=root.ref,
                        links=(Link(relation="contributes_to", target=root.ref),),
                        acceptance=(criterion(name),),
                    )
                    for name in names
                ),
            ),
            context=CommitContext(source_id="host"),
        )
    return execution


def spec(name="observe", topics=("a", "b", "c"), *, timeout=120):
    return KnowledgeSourceSpec(
        id=name,
        version="1",
        emitted_kinds=("fact", "hint"),
        accepted_kinds=("intent",),
        activation_rule=ActivationRule(intent_topics=topics, reason="Verify independent inputs"),
        adapter_type="rule",
        limits=SourceLimits(
            token_budget=50, cost_budget_usd=0.01, timeout_seconds=timeout, max_attempts=2
        ),
    )


async def offer(execution, branch, source="observe", *, critical=False):
    board = await execution.store.snapshot("board")
    intent = next(
        item for item in board.objects if isinstance(item, Intent) and item.topic == branch
    )
    source_spec = spec(source)
    view = make_view(board, intent, source_spec, execution.now)
    return ActivationOffer(
        candidate=match_rule(source_spec, view), view=view, spec=source_spec, critical_gap=critical
    )


async def fact(execution, record):
    observation = execution.observation(record)
    content = observation.content.model_copy(
        update={"claim_key": record.view.intent.topic, "topic": record.view.intent.topic}
    )
    receipt = await execution.kernel.verification.attest(
        "board", content, verifier_id="fixture", evidence=observation.proof
    )
    return FactProposal(
        **content.model_dump(), local_id="verified", verification_receipt_id=receipt.receipt_id
    )


async def finish(ledger, batch, *, known=True):
    for item in batch.items:
        await ledger.save_output(
            item.activation_id, item.attempt, KnowledgeOutput(usage_complete=known)
        )
        await ledger.finish(
            item.activation_id,
            attempt=item.attempt,
            status="no_progress",
            reason="fixture complete",
            progress=False,
        )


@pytest.mark.parametrize("quota", (1, 2))
async def test_batch_reserves_bounded_slots_and_branch_quota_atomically(blackboard_store, quota):
    execution = await branching(blackboard_store)
    offers = tuple(
        [await offer(execution, "a", f"a{n}") for n in range(3)]
        + [await offer(execution, "b"), await offer(execution, "c")]
    )
    limits = LIMITS.model_copy(update={"max_per_intent": quota})
    first, duplicate = await asyncio.gather(
        *[
            execution.ledger.reserve_batch("board", offers, limits, now=execution.now)
            for _ in range(2)
        ]
    )
    batch = first.batch or duplicate.batch
    assert first.busy != duplicate.busy
    records = await execution.ledger.records("board")
    assert len(records) == len(batch.items) == 2
    assert len({record.candidate.intent_ref.object_id for record in records}) == 2
    assert all(record.batch_id == batch.id and not record.started for record in records)
    assert (await execution.ledger.summary("board")).reserved_tokens == 0
    assert len((await execution.ledger.run("board")).batches) == 1
    # Even a completed output awaiting commit occupies its slot and batch barrier.
    await execution.ledger.save_output(batch.items[0].activation_id, 1, KnowledgeOutput())
    assert (await execution.ledger.reserve_batch("board", offers, limits)).busy
    await finish(execution.ledger, batch)
    replay = tuple(
        item for item in offers if item.candidate.id in {record.candidate.id for record in records}
    )
    assert (await execution.ledger.reserve_batch("board", replay, limits)).batch is None
    assert (await execution.ledger.summary("board")).attempts == 2


async def test_one_intent_cannot_fill_all_slots_and_old_writer_cannot_reserve(blackboard_store):
    execution = await branching(blackboard_store)
    # Attaches the in-memory ledger to the same ownership authority as the controller.
    controller = BlackboardController(
        execution.kernel, KnowledgeSourceRegistry(), ledger=execution.ledger, limits=LIMITS
    )
    old = await controller.execution.writer("board")
    current = await execution.kernel.writer("board", takeover=True)
    offers = tuple([await offer(execution, "a", f"source-{n}") for n in range(4)])
    limits = LIMITS.model_copy(update={"max_concurrency": 4, "max_per_intent": 2})
    with pytest.raises(StaleWriterError):
        await execution.ledger.reserve_batch("board", offers, limits, writer=old)
    assert not await execution.ledger.records("board")
    result = await execution.ledger.reserve_batch("board", offers, limits, writer=current)
    assert len(result.batch.items) == 2


async def test_duplicate_runner_does_not_recover_or_redispatch_a_live_batch(blackboard_store):
    execution = await branching(blackboard_store, names=("a", "b"))
    registry = KnowledgeSourceRegistry()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def work(invocation, view):
        calls.append(view.intent.topic)
        if len(calls) == 2:
            entered.set()
        await release.wait()
        return KnowledgeOutput(proposals=(await fact(execution, invocation.record),))

    registry.register(RuleKnowledgeSource(spec(), work))
    controller = BlackboardController(
        execution.kernel, registry, ledger=execution.ledger, limits=LIMITS
    )
    first, duplicate = BlackboardRunner(controller), BlackboardRunner(controller)
    running = asyncio.create_task(first.run("board"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        before = await execution.ledger.records("board")
        with pytest.raises(BlackboardError, match="already executing"):
            await duplicate.run("board")
        assert await execution.ledger.records("board") == before
        release.set()
        assert (await running)["status"] == "succeeded"
        assert sorted(calls) == ["a", "b"]
        # The process-local guard is released when the first call has actually completed.
        assert (await duplicate.run("board"))["status"] == "succeeded"
    finally:
        running.cancel()
        with suppress(asyncio.CancelledError):
            await running


async def test_batch_failure_rolls_back_all_reservations_and_fairness(
    blackboard_store, monkeypatch
):
    execution = await branching(blackboard_store)
    offers = (await offer(execution, "a"), await offer(execution, "b"))
    await execution.ledger.start_run("board", LIMITS, now=execution.now)
    before = await execution.ledger.run("board")
    original = LedgerState.reserve

    def fail_second(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if len(self.attempts) == 2:
            raise RuntimeError("failed second reservation")
        return result

    monkeypatch.setattr(LedgerState, "reserve", fail_second)
    with pytest.raises(RuntimeError, match="second reservation"):
        await execution.ledger.reserve_batch("board", offers, LIMITS, now=execution.now)
    assert not await execution.ledger.records("board")
    assert await execution.ledger.run("board") == before
    assert (await execution.ledger.summary("board")).reserved_tokens == 0


async def test_unknown_usage_stays_visible_without_blocking_another_branch(blackboard_store):
    execution = await branching(blackboard_store)
    offers = tuple([await offer(execution, branch) for branch in ("a", "b")])
    limits = LIMITS.model_copy(update={"token_budget": 120})
    first = await execution.ledger.reserve_batch("board", offers, limits, now=execution.now)
    await finish(execution.ledger, first.batch, known=False)
    third = (await offer(execution, "c"),)
    blocked = await execution.ledger.reserve_batch("board", third, limits, now=execution.now)
    assert not blocked.busy and not blocked.budget_blocked and blocked.batch is not None
    assert (await execution.ledger.summary("board")).unresolved_attempts == 2
    assert (await execution.ledger.summary("board")).reserved_tokens == 0
    item = first.batch.items[0]
    await execution.ledger.complete_usage(item.activation_id, item.attempt)
    assert (await execution.ledger.summary("board")).unresolved_attempts == 1


async def test_waiting_branch_gets_a_turn_despite_continuous_priority_work(blackboard_store):
    execution = await branching(blackboard_store)
    limits = LIMITS.model_copy(update={"max_concurrency": 1})
    selected = []
    for round_no in range(4):
        offers = (
            await offer(execution, "a", f"priority-{round_no}", critical=True),
            await offer(execution, "b"),
            await offer(execution, "c"),
        )
        # Fresh adapters must preserve the same waiting counters.
        ledger = build_activation_store(blackboard_store)
        reservation = await ledger.reserve_batch("board", offers, limits, now=execution.now)
        item = reservation.batch.items[0]
        record = await ledger.get(item.activation_id, item.attempt)
        selected.append(record.view.intent.topic)
        assert record.selection.policy == ("priority" if round_no < 2 else "waiting_turn")
        await finish(ledger, reservation.batch)
    assert selected[:2] == ["a", "a"] and set(selected[2:]) == {"b", "c"}


@pytest.mark.parametrize("fail_branch", (None, "a"))
async def test_actual_parallel_sources_keep_independent_results_and_usage(
    blackboard_store, fail_branch
):
    execution = await branching(blackboard_store)
    registry = KnowledgeSourceRegistry()
    first_batch = asyncio.Event()
    active = 0
    peak = 0
    calls = []

    async def work(invocation, view):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        calls.append(view.intent.topic)
        try:
            await invocation.record_usage(UsageRecord(call_id="read", input_tokens=7))
            if len(calls) == 2:
                first_batch.set()
            # A serial implementation cannot pass this barrier.
            await asyncio.wait_for(first_batch.wait(), 3)
            if view.intent.topic == fail_branch:
                raise ValueError("one branch is unavailable")
            return KnowledgeOutput(proposals=(await fact(execution, invocation.record),))
        finally:
            active -= 1

    registry.register(RuleKnowledgeSource(spec(), work))
    controller = BlackboardController(
        execution.kernel, registry, ledger=execution.ledger, limits=LIMITS
    )
    result = await BlackboardRunner(controller).run("board")
    assert peak == 2 and sorted(calls) == ["a", "b", "c"]
    assert result["status"] == ("succeeded" if fail_branch is None else "partial")
    board = await execution.store.snapshot("board")
    assert {item.claim_key for item in board.objects if isinstance(item, Fact)} == (
        {"a", "b", "c"} - {fail_branch}
    )
    records = await execution.ledger.records("board")
    assert all(record.attempt == 1 for record in records)
    summary = await execution.ledger.summary("board")
    assert summary.tokens == 21 and summary.reserved_tokens == 0


async def test_stale_proposal_does_not_discard_an_independent_sibling(blackboard_store):
    execution = await branching(blackboard_store, names=("a", "b"))
    registry = KnowledgeSourceRegistry()

    async def hint(invocation, view):
        await invocation.record_usage(UsageRecord(call_id="read", input_tokens=7))
        return KnowledgeOutput(
            proposals=(
                HintProposal(
                    local_id="hint",
                    topic="shared",
                    suggestion=invocation.record.spec.id,
                    applies_when="Branch a needs evidence",
                    rationale="Explore this candidate",
                    links=(Link(relation="contributes_to", target=view.intent.ref),),
                ),
            )
        )

    for name in ("a-first", "a-second"):
        source_spec = spec(name, topics=("a",)).model_copy(
            update={
                "accepted_kinds": ("intent", "hint"),
                "activation_rule": ActivationRule(
                    intent_topics=("a",), absent_hint_topics=("shared",), reason="Explore a"
                ),
            }
        )
        registry.register(RuleKnowledgeSource(source_spec, hint))

    async def observe(invocation, view):
        await invocation.record_usage(UsageRecord(call_id="read", input_tokens=7))
        return KnowledgeOutput(proposals=(await fact(execution, invocation.record),))

    registry.register(RuleKnowledgeSource(spec("b-independent", topics=("b",)), observe))
    controller = BlackboardController(
        execution.kernel,
        registry,
        ledger=execution.ledger,
        limits=LIMITS.model_copy(update={"max_concurrency": 3, "max_per_intent": 2}),
    )
    result = await BlackboardRunner(controller).run("board")
    records = {record.spec.id: record for record in await execution.ledger.records("board")}
    assert result["status"] == "partial"
    assert records["a-first"].status == records["b-independent"].status == "completed"
    assert records["a-second"].status == "retryable"
    board = await execution.store.snapshot("board")
    assert len([item for item in board.objects if isinstance(item, Hint)]) == 1
    assert {item.claim_key for item in board.objects if isinstance(item, Fact)} == {"b"}
    assert (await execution.ledger.summary("board")).tokens == 21


async def test_sqlite_batch_insert_failure_is_atomic_and_two_connections_share_limits(tmp_path):
    path = tmp_path / "board.sqlite"
    first, second = SQLiteBlackboardStore(path), SQLiteBlackboardStore(path)
    await first.initialize()
    await second.initialize()
    try:
        execution = await branching(first)
        offers = (await offer(execution, "a"), await offer(execution, "b"))
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute(
                "CREATE TRIGGER fail_second BEFORE INSERT ON board_activation_attempts "
                "WHEN NEW.ordinal = 2 BEGIN SELECT RAISE(ABORT, 'second insert'); END"
            )
        with pytest.raises(sqlite3.IntegrityError, match="second insert"):
            await execution.ledger.reserve_batch("board", offers, LIMITS)
        assert not await SQLiteActivationStore(second).records("board")
        with pytest.raises(BlackboardError, match="not been registered"):
            await execution.ledger.run("board")
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("DROP TRIGGER fail_second")
        limits = LIMITS.model_copy(update={"token_budget": 75})
        results = await asyncio.gather(
            execution.ledger.reserve_batch("board", offers, limits),
            SQLiteActivationStore(second).reserve_batch("board", offers, limits),
        )
        assert sum(result.batch is not None for result in results) == 1
        assert sum(result.busy for result in results) == 1
        assert (await execution.ledger.summary("board")).reserved_tokens == 0
    finally:
        await first.close()
        await second.close()


class SplitState(SourceState, total=False):
    read_branch: str


class SplitSource(RuleKnowledgeSource):
    """Two private steps expose whether sibling recovery replays an already saved read."""

    def __init__(self, source_spec, execution, reads, reached, block):
        super().__init__(source_spec, None)
        self.host = execution
        self.reads, self.reached, self.block = reads, reached, block

    def build(self, execution):
        async def read(state):
            invocation = await execution.invocation(state)
            branch = invocation.record.view.intent.topic
            self.reads.append((branch, invocation.record.attempt))
            await invocation.record_usage(UsageRecord(call_id="private-read", input_tokens=7))
            return {"read_branch": branch}

        async def reason(state):
            self.reached[state["read_branch"]].set()
            if self.block is True or self.block == state["read_branch"]:
                await asyncio.Event().wait()
            invocation = await execution.invocation(state)
            output = KnowledgeOutput(proposals=(await fact(self.host, invocation.record),))
            return {"output": output.model_dump(mode="json")}

        graph = StateGraph(SplitState, input_schema=SourceInput, output_schema=SourceOutput)
        graph.add_node("read", read)
        graph.add_node("reason", reason)
        graph.add_node("publish", execution.publish)
        graph.add_edge(START, "read")
        graph.add_edge("read", "reason")
        graph.add_edge("reason", "publish")
        graph.add_edge("publish", END)
        return graph.compile()


@asynccontextmanager
async def parallel_harness(
    path, reads, reached, *, create=False, block=False, now=None, split=False
):
    store = SQLiteBlackboardStore(path / "board.sqlite")
    await store.initialize()
    try:
        execution = await branching(store, names=("a", "b"), create=create)
        if now is not None:
            execution.kernel.clock = lambda: now
        registry = KnowledgeSourceRegistry()
        source_specs = (
            (spec("a-short", topics=("a",), timeout=30), spec("b-long", topics=("b",), timeout=120))
            if split
            else (spec(),)
        )
        for source_spec in source_specs:
            registry.register(SplitSource(source_spec, execution, reads, reached, block))
        controller = BlackboardController(
            execution.kernel, registry, ledger=execution.ledger, limits=LIMITS
        )
        async with AsyncSqliteSaver.from_conn_string(str(path / "checkpoints.sqlite")) as saver:
            yield execution, BlackboardRunner(controller, checkpointer=saver)
    finally:
        await store.close()


async def test_recovery_maps_sends_before_child_checkpoints_exist(tmp_path):
    reads, reached = [], {name: asyncio.Event() for name in ("a", "b")}
    async with parallel_harness(tmp_path, reads, reached, create=True) as (execution, runner):
        await runner.graph.ainvoke({"board_id": "board"}, CONFIG, interrupt_before=["observe"])
        checkpoint = await runner.graph.aget_state(CONFIG, subgraphs=True)
        assert len(checkpoint.tasks) == 2
        assert not any(getattr(task.state, "values", None) for task in checkpoint.tasks)
        assert len((await execution.ledger.run("board")).batches) == 1 and not reads
    async with parallel_harness(tmp_path, reads, reached) as (execution, runner):
        assert (await runner.run("board", CONFIG))["status"] == "succeeded"
        assert sorted(reads) == [("a", 1), ("b", 1)]
        assert len((await execution.ledger.run("board")).batches) == 1


@pytest.mark.parametrize("expire_one", (False, True))
async def test_partial_batch_resume_preserves_each_private_checkpoint(tmp_path, expire_one):
    reads, reached = [], {name: asyncio.Event() for name in ("a", "b")}
    async with parallel_harness(
        tmp_path, reads, reached, create=True, block=True, split=expire_one
    ) as (execution, runner):
        running = asyncio.create_task(runner.run("board", CONFIG))
        try:
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in reached.values())), 5)
        finally:
            running.cancel()
            with suppress(asyncio.CancelledError):
                await running
        before = await execution.ledger.records("board")
        deadline = (await execution.ledger.run("board")).deadline
        now = (
            min(record.deadline for record in before) + timedelta(seconds=1) if expire_one else None
        )
        assert sorted(reads) == [("a", 1), ("b", 1)]
    async with parallel_harness(tmp_path, reads, reached, now=now, split=expire_one) as (
        execution,
        runner,
    ):
        result = await runner.run("board", CONFIG)
        assert result["status"] == "succeeded"
        assert sorted(reads) == (
            [("a", 1), ("a", 2), ("b", 1)] if expire_one else [("a", 1), ("b", 1)]
        )
        assert (await execution.ledger.run("board")).deadline == deadline
        assert (await execution.ledger.summary("board")).tokens == (21 if expire_one else 14)
        records = await execution.ledger.records("board")
        assert all(record.status == "completed" for record in records)
        assert next(record for record in records if record.view.intent.topic == "b").attempt == 1


async def test_v5_history_upgrades_but_old_active_workflow_is_not_reinterpreted(tmp_path):
    path = tmp_path / "board.sqlite"
    first = SQLiteBlackboardStore(path)
    await first.initialize()
    execution = await branching(first)
    old = await offer(execution, "a")
    record = await execution.ledger.reserve(old.candidate, old.view, old.spec, LIMITS)
    await execution.ledger.bind_contract("board", "blackboard-v3", digest([]))
    await first.close()
    with closing(sqlite3.connect(path)) as connection, connection:
        payload = json.loads(connection.execute("SELECT payload FROM board_control").fetchone()[0])
        for name in ("batches", "waiting_batches", "last_scheduled"):
            payload.pop(name)
        for name in ("max_concurrency", "max_per_intent", "max_wait_batches"):
            payload["limits"].pop(name)
        connection.execute("UPDATE board_control SET payload = ?", (json.dumps(payload),))
        payload = json.loads(
            connection.execute("SELECT payload FROM board_activation_attempts").fetchone()[0]
        )
        for name in ("batch_id", "selection"):
            payload.pop(name)
        connection.execute(
            "UPDATE board_activation_attempts SET payload = ?", (json.dumps(payload),)
        )
        connection.execute("UPDATE blackboard_schema SET version = 5")
    reopened = SQLiteBlackboardStore(path)
    await reopened.initialize()
    try:
        ledger = SQLiteActivationStore(reopened)
        assert (await ledger.get(record.candidate.id)).candidate == record.candidate
        assert not (await ledger.run("board")).batches
        with pytest.raises(BlackboardError, match="incompatible"):
            await ledger.bind_contract("board", BLACKBOARD_WORKFLOW_REVISION, digest([]))
    finally:
        await reopened.close()


async def test_recovery_keeps_batch_barrier_and_reservation_commit_order(tmp_path, monkeypatch):
    reads, reached = [], {name: asyncio.Event() for name in ("a", "b")}
    ready = asyncio.Event()
    async with parallel_harness(tmp_path, reads, reached, create=True, split=True, block="a") as (
        execution,
        runner,
    ):
        original = execution.ledger.save_output

        async def saved(*args, **kwargs):
            await original(*args, **kwargs)
            ready.set()

        monkeypatch.setattr(execution.ledger, "save_output", saved)
        running = asyncio.create_task(runner.run("board", CONFIG))
        try:
            await asyncio.wait_for(ready.wait(), 5)
            records = await execution.ledger.records("board")
            assert [(r.view.intent.topic, r.status) for r in records] == [
                ("a", "running"),
                ("b", "result_ready"),
            ]
            assert not any(
                isinstance(item, Fact) for item in (await execution.store.snapshot("board")).objects
            )
        finally:
            running.cancel()
            with suppress(asyncio.CancelledError):
                await running
    async with parallel_harness(tmp_path, reads, reached, split=True) as (execution, runner):
        assert (await runner.run("board", CONFIG))["status"] == "succeeded"
        events = [
            event.activation_id
            for event in await execution.store.events("board")
            if event.activation_id
        ]
        records = await execution.ledger.records("board")
        assert events == [record.candidate.id for record in records]
        assert sorted(reads) == [("a", 1), ("b", 1)]


@asynccontextmanager
async def model_harness(path, *, create=False):
    store = SQLiteBlackboardStore(path / "board.sqlite")
    await store.initialize()
    try:
        execution = await branching(store, names=("a", "b"), create=create)
        async with build_runtime(
            ROOT,
            checkpoint_backend="memory",
            run_store_backend="memory",
            provider_store_backend="memory",
        ) as runtime:
            services = AgentServices(
                config=runtime.config,
                gateway=runtime.model_gateway,
                prompts=runtime.prompt_registry,
                trace=TraceRecorder(enabled=False),
                tool_gateway=runtime.tool_gateway,
                skills=runtime.skill_registry,
            )
            registry = KnowledgeSourceRegistry()
            registry.register(
                AgentKnowledgeSource(
                    KnowledgeSourceSpec(
                        id="agent",
                        version="1",
                        emitted_kinds=("hint",),
                        adapter_type="agent",
                        activation_rule=ActivationRule(
                            intent_topics=("a", "b"),
                            absent_hint_topics=("a", "b"),
                            reason="Draft each independent answer",
                        ),
                        model_route="worker-fast",
                        allowed_tools=("echo_read",),
                        limits=SourceLimits(
                            token_budget=1000, cost_budget_usd=0.1, timeout_seconds=120
                        ),
                    ),
                    services,
                )
            )

            async def verify(invocation, view):
                await invocation.record_usage(UsageRecord(call_id="verify", input_tokens=7))
                return KnowledgeOutput(proposals=(await fact(execution, invocation.record),))

            for branch in ("a", "b"):
                registry.register(
                    RuleKnowledgeSource(
                        spec(f"verify-{branch}", topics=(branch,)).model_copy(
                            update={
                                "accepted_kinds": ("intent", "hint"),
                                "activation_rule": ActivationRule(
                                    intent_topics=(branch,),
                                    hint_topics=(branch,),
                                    reason="Verify candidate",
                                ),
                            }
                        ),
                        verify,
                    )
                )
            controller = BlackboardController(
                execution.kernel,
                registry,
                ledger=execution.ledger,
                limits=LIMITS.model_copy(update={"token_budget": 5000}),
            )
            async with AsyncSqliteSaver.from_conn_string(str(path / "checkpoints.sqlite")) as saver:
                yield execution, BlackboardRunner(controller, checkpointer=saver)
    finally:
        await store.close()


@pytest.mark.parametrize("resume", (False, True))
async def test_same_agent_parallel_invocations_isolate_usage_tools_and_resume(
    tmp_path, monkeypatch, resume
):
    original_complete, original_tool, original_model = (
        MockProvider.complete,
        ToolGateway.execute,
        WorkerAgent.model,
    )
    model_barrier = asyncio.Barrier(2)
    reached = {name: asyncio.Event() for name in ("a", "b")}
    block = resume
    budgets = {"a": [], "b": []}
    tools = []

    async def complete(self, request, *, model):
        branch = request.metadata["payload"]["task"]["context"]["blackboard"]["intent"]["topic"]
        budgets[branch].append(request.max_output_tokens)
        if not request.metadata["tool_round"]:
            await asyncio.wait_for(model_barrier.wait(), 5)
            return ModelResponse(
                provider=self.name,
                model=model,
                usage=Usage(input_tokens=10 if branch == "a" else 20, output_tokens=5),
                tool_calls=[
                    {
                        "id": f"read-{branch}",
                        "type": "function",
                        "function": {
                            "name": "echo_read",
                            "arguments": json.dumps({"text": branch}),
                        },
                    }
                ],
            )
        return await original_complete(self, request, model=model)

    async def tool(self, **kwargs):
        tools.append(kwargs["arguments"]["text"])
        return await original_tool(self, **kwargs)

    async def model(self, state):
        if block and state.get("tool_calls"):
            branch = state["assignment"]["task"]["context"]["blackboard"]["intent"]["topic"]
            reached[branch].set()
            await asyncio.Event().wait()
        return await original_model(self, state)

    monkeypatch.setattr(MockProvider, "complete", complete)
    monkeypatch.setattr(ToolGateway, "execute", tool)
    monkeypatch.setattr(WorkerAgent, "model", model)

    async def verify_result(execution, result):
        assert result["status"] == "succeeded"
        assert sorted(tools) == ["a", "b"]
        assert budgets == {"a": [1000, 1000], "b": [1000, 1000]}
        records = [
            record
            for record in await execution.ledger.records("board")
            if record.spec.id == "agent"
        ]
        assert len(records) == 2 and all(record.attempt == 1 for record in records)
        for record in records:
            calls = await execution.ledger.usage(record.candidate.id, record.attempt)
            assert len(calls) == 2
            assert sum(call.tokens for call in calls) == (
                207 if record.view.intent.topic == "a" else 217
            )
        summary = await execution.ledger.summary("board")
        assert summary.tokens == 438 and summary.reserved_tokens == 0

    async with model_harness(tmp_path, create=True) as (execution, runner):
        if not resume:
            await verify_result(execution, await runner.run("board", CONFIG))
            return
        running = asyncio.create_task(runner.run("board", CONFIG))
        try:
            await asyncio.wait_for(
                asyncio.gather(*(event.wait() for event in reached.values())), 10
            )
        finally:
            running.cancel()
            with suppress(asyncio.CancelledError):
                await running
        assert (await execution.ledger.summary("board")).tokens == 40
    block = False
    async with model_harness(tmp_path) as (execution, runner):
        await verify_result(execution, await runner.run("board", CONFIG))
