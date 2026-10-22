"""Goal progress, finite exploration and recovery on real controller/storage boundaries."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from test_blackboard_execution_storage import setup
from test_blackboard_settlement import ready

from work_harness.blackboard.controller import BlackboardController
from work_harness.blackboard.execution import RunLimits
from work_harness.blackboard.models import (
    AcceptanceCriterion,
    Evidence,
    Fact,
    FactContent,
    FactProposal,
    HintProposal,
    IntentProposal,
    Link,
    digest,
)
from work_harness.blackboard.progress import advance, initial, summarize
from work_harness.blackboard.protocol import (
    BlackboardDelta,
    BlackboardError,
    CommitContext,
    FactQuery,
)
from work_harness.knowledge_sources.adapters.callback import RuleKnowledgeSource
from work_harness.knowledge_sources.base import (
    ActivationRule,
    KnowledgeOutput,
    KnowledgeSourceSpec,
    SourceLimits,
    UsageRecord,
)
from work_harness.knowledge_sources.registry import KnowledgeSourceRegistry
from work_harness.storage.blackboard_sqlite import SQLiteBlackboardStore
from work_harness.workflow.blackboard_runner import BlackboardRunner

LIMITS = RunLimits(max_no_progress=1, max_explorations=3, max_concurrency=1, max_activations=20)


class Crash(BaseException):
    pass


@pytest.fixture
async def execution(blackboard_store):
    return await setup(blackboard_store)


def spec(name, *, hints=(), absent=(), topics=("answer",), emitted=("hint",), facts=()):
    return KnowledgeSourceSpec(
        id=name,
        version="1",
        emitted_kinds=emitted,
        adapter_type="rule",
        activation_rule=ActivationRule(
            intent_topics=topics,
            hint_topics=hints,
            absent_hint_topics=absent,
            required_facts=facts,
            reason="按板面求证",
        ),
        limits=SourceLimits(token_budget=100, max_attempts=2),
    )


def hint(root, topic, wording="核验这一方向"):
    return HintProposal(
        local_id="hint",
        topic=topic,
        suggestion=wording,
        applies_when="目标未完成",
        rationale="等待验证",
        links=(Link(relation="contributes_to", target=root.ref),),
    )


async def fact(execution, root, *, key="answer", value=42, nonce=None):
    proof = {"value": value}
    content = FactContent(
        topic="answer",
        claim_key=key,
        claim_type="tool_observation",
        value=value,
        statement=f"Observed {key}: {value}, note {nonce}",
        scope={} if nonce is None else {"note": str(nonce)},
        evidence=(
            Evidence(
                source="tool",
                locator="test:progress",
                content_digest=digest(proof),
                description="fixture",
            ),
        ),
        links=(Link(relation="contributes_to", target=root.ref),),
    )
    verification = await execution.kernel.verification.attest(
        "board",
        content,
        verifier_id="fixture",
        evidence=proof,
    )
    return FactProposal(
        **content.model_dump(),
        local_id="fact",
        verification_receipt_id=verification.receipt_id,
    )


def chain(execution, calls, *, length=3):
    registry = KnowledgeSourceRegistry()
    for index in range(length):
        source = spec(
            f"step{index}",
            hints=(f"h{index}",) if index else (),
            absent=(f"h{index + 1}",) if index < length - 1 else (),
            emitted=("fact",) if index == length - 1 else ("hint",),
        )

        def work(step):
            async def execute(invocation, view):
                calls.append(step)
                await invocation.record_usage(UsageRecord(call_id="work", input_tokens=5))
                proposal = (
                    await fact(execution, view.root_intent)
                    if step == length - 1
                    else hint(view.intent, f"h{step + 1}")
                )
                return KnowledgeOutput(proposals=(proposal,))

            return execute

        registry.register(RuleKnowledgeSource(source, work(index)))
    return registry


async def run(execution, registry, limits=LIMITS):
    controller = BlackboardController(
        execution.kernel, registry, ledger=execution.ledger, limits=limits
    )
    return await BlackboardRunner(controller).run("board", {"recursion_limit": 100})


async def test_multi_step_verification_uses_finite_exploration_then_goal_progress(execution):
    calls = []
    result = await run(execution, chain(execution, calls))
    assert result["status"] == "succeeded" and calls == [0, 1, 2]
    records = await execution.ledger.records("board")
    assert [r.progress.kind for r in records] == ["exploration", "exploration", "goal_progress"]
    progress = (await execution.ledger.run("board")).progress
    assert progress.exploration_remaining == 1
    assert not progress.snapshot.gaps
    assert (await execution.ledger.summary("board")).no_progress == 0
    assert (await execution.ledger.summary("board")).tokens == 15


async def test_exploration_budget_is_a_run_limit_not_a_branch_allowance(execution):
    calls = []
    result = await run(
        execution,
        chain(execution, calls, length=5),
        LIMITS.model_copy(update={"max_explorations": 1}),
    )
    assert result["stop_reason"] == "no_progress" and calls == [0, 1]
    records = await execution.ledger.records("board")
    assert [r.progress.kind for r in records] == ["exploration", "no_progress"]
    assert records[-1].progress.reasons == ("exploration_budget_exhausted",)
    assert (await execution.ledger.run("board")).progress.exploration_remaining == 0
    assert "尚未满足的条件" in result["final_output"]
    assert "已尝试方向" in result["final_output"]
    final = (await execution.store.snapshot("board")).finalization
    assert len(final.attempted_directions) == 2 and final.missing_criteria == ("answer",)


@pytest.mark.parametrize("noise", ("hint", "intent", "fact"))
async def test_unrelated_objects_and_rewording_cannot_extend_run(execution, noise):
    registry = KnowledgeSourceRegistry()
    calls = []

    async def spam(invocation, view):
        calls.append(1)
        proposals = [hint(view.root_intent, "noise", f"换一种说法 {len(calls)}")]
        if noise == "intent":
            proposals.append(
                IntentProposal(
                    local_id="branch",
                    topic="irrelevant",
                    objective=f"无关分支 {len(calls)}",
                    root_intent_ref=view.root_intent.ref,
                    parent_intent_ref=view.root_intent.ref,
                    links=(Link(relation="contributes_to", target=view.root_intent.ref),),
                    acceptance=(
                        AcceptanceCriterion(
                            id="noise",
                            description="无关检查",
                            claim_key="unrelated",
                            claim_type="tool_observation",
                        ),
                    ),
                )
            )
        elif noise == "fact":
            proposals.append(
                await fact(execution, view.root_intent, key=f"noise{len(calls)}", value=0)
            )
        return KnowledgeOutput(proposals=tuple(proposals))

    registry.register(RuleKnowledgeSource(spec("spam", emitted=("hint", "intent", "fact")), spam))
    result = await run(execution, registry, LIMITS.model_copy(update={"max_no_progress": 2}))
    assert result["stop_reason"] == "no_progress" and len(calls) == 2
    records = await execution.ledger.records("board")
    assert all(r.progress.kind == "no_progress" for r in records)
    assert all(r.status == "completed" for r in records)  # accepted knowledge is not goal progress
    assert (await execution.ledger.run("board")).progress.exploration_remaining == 3


async def test_reworded_relevant_claim_and_extra_scope_do_not_reset_counter(execution):
    registry = KnowledgeSourceRegistry()
    calls = []

    async def repeat(invocation, view):
        calls.append(1)
        return KnowledgeOutput(
            proposals=(await fact(execution, view.intent, value=1, nonce=len(calls)),)
        )

    registry.register(RuleKnowledgeSource(spec("repeat", emitted=("fact",)), repeat))
    result = await run(execution, registry, LIMITS.model_copy(update={"max_no_progress": 2}))
    assert result["stop_reason"] == "no_progress" and len(calls) == 3
    assert [r.progress.kind for r in await execution.ledger.records("board")] == [
        "goal_progress",
        "no_progress",
        "no_progress",
    ]
    assert (await execution.ledger.run("board")).progress.snapshot.gaps


async def test_related_input_evidence_counts_before_final_acceptance(execution):
    registry = KnowledgeSourceRegistry()

    async def observe(invocation, view):
        return KnowledgeOutput(
            proposals=(await fact(execution, view.intent, key="operand", value=21),)
        )

    observer = spec("observe", emitted=("fact",))
    observer = observer.model_copy(
        update={
            "activation_rule": observer.activation_rule.model_copy(
                update={
                    "absent_facts": (FactQuery(claim_key="operand"),),
                }
            )
        }
    )
    registry.register(RuleKnowledgeSource(observer, observe))

    async def answer(invocation, view):
        return KnowledgeOutput(proposals=(await fact(execution, view.intent),))

    registry.register(
        RuleKnowledgeSource(
            spec("answer", facts=(FactQuery(claim_key="operand"),), emitted=("fact",)),
            answer,
        )
    )
    result = await run(execution, registry, LIMITS.model_copy(update={"max_explorations": 0}))
    assert result["status"] == "succeeded"
    records = await execution.ledger.records("board")
    assert len(records) == 2 and all(r.progress.kind == "goal_progress" for r in records)
    assert records[0].progress.new_evidence and not records[0].progress.closed_gaps


async def test_canonical_direction_ignores_new_intent_ids_and_descriptions(execution):
    board = await execution.store.snapshot("board")
    root = board.by_id()[board.root_intent_id]
    source = spec("branch", topics=("branch",))

    async def add(label):
        current = await execution.store.snapshot("board")
        await execution.kernel.commit(
            "board",
            BlackboardDelta(
                expected_revision=current.revision,
                idempotency_key=label,
                proposals=(
                    IntentProposal(
                        local_id="branch",
                        topic="branch",
                        objective=label,
                        root_intent_ref=root.ref,
                        parent_intent_ref=root.ref,
                        links=(Link(relation="contributes_to", target=root.ref),),
                        acceptance=(
                            root.acceptance[0].model_copy(
                                update={"id": label, "description": label}
                            ),
                        ),
                    ),
                ),
            ),
            context=CommitContext(source_id="host"),
        )
        return await execution.store.snapshot("board")

    first = summarize(await add("one"), (source,), execution.now)
    second = summarize(await add("another phrasing"), (source,), execution.now)
    assert first.directions.keys() == second.directions.keys()
    updated, decision = advance(initial(first, 3), second, allow_exploration=True)
    assert decision.kind == "no_progress" and updated.exploration_remaining == 3


async def test_conflict_resolution_requires_evidence_and_cannot_be_farmed(execution):
    board = await execution.store.snapshot("board")
    root = board.by_id()[board.root_intent_id]
    await execution.kernel.commit(
        "board",
        BlackboardDelta(
            expected_revision=board.revision,
            idempotency_key="seed",
            proposals=(await fact(execution, root, value=1),),
        ),
        context=CommitContext(source_id="host"),
    )
    board = await execution.store.snapshot("board")
    observed = next(item for item in board.objects if isinstance(item, Fact))
    state = initial(summarize(board, (), execution.now), 2)

    def phase(lifecycle, revision):
        return board.model_copy(
            update={
                "revision": revision,
                "objects": tuple(
                    observed.model_copy(update={"lifecycle": lifecycle})
                    if item.id == observed.id
                    else item
                    for item in board.objects
                ),
            }
        )

    disputed = summarize(phase("contested", 3), (), execution.now)
    state, _ = advance(state, disputed, allow_exploration=False)
    removed = summarize(phase("retracted", 4), (), execution.now)
    _, decision = advance(state, removed, allow_exploration=False)
    assert decision.kind == "no_progress" and not decision.resolved_conflicts
    restored = summarize(phase("valid", 4), (), execution.now)
    state, decision = advance(state, restored, allow_exploration=False)
    assert decision.kind == "goal_progress" and decision.resolved_conflicts
    state, _ = advance(
        state, disputed.model_copy(update={"board_revision": 5}), allow_exploration=False
    )
    state, decision = advance(
        state, restored.model_copy(update={"board_revision": 6}), allow_exploration=False
    )
    assert decision.kind == "no_progress"


async def test_external_time_progress_is_captured_without_refilling_exploration(execution):
    calls = []
    registry = chain(execution, calls)
    controller = BlackboardController(
        execution.kernel, registry, ledger=execution.ledger, limits=LIMITS
    )
    await controller.bootstrap({"board_id": "board"})
    await controller.assess({"board_id": "board"})
    before = await execution.ledger.run("board")
    board = await execution.store.snapshot("board")
    root = board.by_id()[board.root_intent_id]
    proposal = await fact(execution, root, value=1)
    content = FactContent.model_validate(proposal.model_dump(include=set(FactContent.model_fields)))
    content = content.model_copy(update={"valid_from": execution.now + timedelta(seconds=1)})
    receipt = await execution.kernel.verification.attest(
        "board", content, verifier_id="fixture", evidence={"value": 1}
    )
    await execution.kernel.commit(
        "board",
        BlackboardDelta(
            expected_revision=board.revision,
            idempotency_key="future",
            proposals=(
                FactProposal(
                    **content.model_dump(),
                    local_id="future",
                    verification_receipt_id=receipt.receipt_id,
                ),
            ),
        ),
        context=CommitContext(source_id="host"),
    )
    specs = tuple(s.spec for s in registry.sources())
    writer = await execution.kernel.writer("board")
    future = await execution.ledger.capture_progress("board", specs, writer, lambda: execution.now)
    assert not future.snapshot.evidence
    active = await execution.ledger.capture_progress(
        "board", specs, writer, lambda: execution.now + timedelta(seconds=2)
    )
    assert active.snapshot.evidence and active.last_decision.kind == "goal_progress"
    assert active.exploration_remaining == before.progress.exploration_remaining


async def test_sqlite_restart_keeps_exploration_balance_and_committed_decision(
    tmp_path, monkeypatch
):
    calls = []
    config = {"configurable": {"thread_id": "progress"}, "recursion_limit": 100}
    path = tmp_path / "board.sqlite"
    for reopening in (False, True):
        store = SQLiteBlackboardStore(path)
        await store.initialize()
        try:
            execution = await setup(store, create=not reopening)
            controller = BlackboardController(
                execution.kernel, chain(execution, calls), ledger=execution.ledger, limits=LIMITS
            )
            async with AsyncSqliteSaver.from_conn_string(
                str(tmp_path / "checkpoints.sqlite")
            ) as saver:
                runner = BlackboardRunner(controller, checkpointer=saver)
                if not reopening:
                    original = execution.kernel.commit

                    async def crash(*args, _original=original, **kwargs):
                        result = await _original(*args, **kwargs)
                        if kwargs.get("settlement"):
                            raise Crash()
                        return result

                    with monkeypatch.context() as patch:
                        patch.setattr(execution.kernel, "commit", crash)
                        with pytest.raises(Crash):
                            await runner.run("board", config)
                    state = await execution.ledger.run("board")
                    assert state.progress.exploration_remaining == 2
                    assert (await execution.ledger.records("board"))[
                        0
                    ].progress.kind == "exploration"
                else:
                    assert (await execution.ledger.run("board")).progress.exploration_remaining == 2
                    result = await runner.run("board", config)
                    assert result["status"] == "succeeded" and calls == [0, 1, 2]
                    assert (await execution.ledger.run("board")).progress.exploration_remaining == 1
                    assert (await execution.ledger.summary("board")).tokens == 15
        finally:
            await store.close()


async def test_v7_history_reads_but_v5_active_contract_cannot_gain_fresh_credit(tmp_path):
    path = tmp_path / "board.sqlite"
    store = SQLiteBlackboardStore(path)
    await store.initialize()
    execution = await setup(store)
    limits = LIMITS.model_copy(update={"max_explorations": 4})
    await execution.ledger.start_run("board", limits, now=execution.now)
    await execution.ledger.bind_contract("board", "blackboard-v5", "old")
    await store.close()
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE blackboard_schema SET version = 7")
        connection.execute(
            "UPDATE board_control SET payload = json_remove(payload, "
            "'$.progress', '$.progress_sources', '$.limits.max_explorations')"
        )
    store = SQLiteBlackboardStore(path)
    await store.initialize()
    try:
        execution = await setup(store, create=False)
        assert (await execution.ledger.run("board")).progress is None
        controller = BlackboardController(
            execution.kernel, KnowledgeSourceRegistry(), ledger=execution.ledger, limits=limits
        )
        with pytest.raises(BlackboardError, match="incompatible"):
            await BlackboardRunner(controller).run("board")
        assert (await execution.ledger.run("board")).progress is None
    finally:
        await store.close()


async def test_independent_branches_make_progress_before_joining_the_root(execution):
    board = await execution.store.snapshot("board")
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
                    objective=f"Verify {name}",
                    root_intent_ref=root.ref,
                    parent_intent_ref=root.ref,
                    links=(Link(relation="contributes_to", target=root.ref),),
                    acceptance=(
                        AcceptanceCriterion(
                            id=name,
                            description=name,
                            claim_key=name,
                            claim_type="tool_observation",
                        ),
                    ),
                )
                for name in ("left", "right")
            ),
        ),
        context=CommitContext(source_id="host"),
    )
    registry = KnowledgeSourceRegistry()

    def observe(name):
        async def execute(invocation, view):
            return KnowledgeOutput(
                proposals=(await fact(execution, view.intent, key=name, value=21),)
            )

        return execute

    for name in ("left", "right"):
        registry.register(
            RuleKnowledgeSource(spec(name, topics=(name,), emitted=("fact",)), observe(name))
        )

    async def join(invocation, view):
        return KnowledgeOutput(proposals=(await fact(execution, view.root_intent),))

    registry.register(
        RuleKnowledgeSource(
            spec(
                "join",
                emitted=("fact",),
                facts=(FactQuery(claim_key="left"), FactQuery(claim_key="right")),
            ),
            join,
        )
    )
    result = await run(execution, registry, LIMITS.model_copy(update={"max_concurrency": 2}))
    assert result["status"] == "succeeded"
    records = await execution.ledger.records("board")
    assert len(records) == 3 and all(r.progress.kind == "goal_progress" for r in records)
    assert len((await execution.ledger.run("board")).batches[0].items) == 2


@pytest.mark.parametrize("observed_newer", (False, True))
async def test_legacy_receipt_uses_its_historical_transition_once(execution, observed_newer):
    execution.ledger.bind(execution.store)
    record = await ready(execution)
    delta, context = record.proposal((await execution.ledger.run("board")).limits)
    receipt = await execution.kernel.commit("board", delta, context=context)
    await execution.kernel.commit(
        "board",
        BlackboardDelta(
            expected_revision=receipt.board_revision,
            idempotency_key="later",
        ),
        context=CommitContext(source_id="host"),
    )
    if observed_newer:
        await execution.ledger.capture_progress(
            "board",
            (),
            await execution.kernel.writer("board"),
            execution.kernel.clock,
        )
    settlement = execution.ledger.settlement(record.ref, execution.kernel.clock)
    assert (
        await execution.kernel.commit("board", delta, context=context, settlement=settlement)
        == receipt
    )
    settled = await execution.ledger.get(record.candidate.id)
    assert settled.progress.kind == ("no_progress" if observed_newer else "goal_progress")
    assert (settled.progress.before_revision, settled.progress.after_revision) == (1, 2)
    state = await execution.ledger.run("board")
    assert state.progress.snapshot.board_revision == (3 if observed_newer else 2)
    await execution.kernel.commit("board", delta, context=context, settlement=settlement)
    assert await execution.ledger.run("board") == state
    assert (await execution.store.snapshot("board")).revision == 3


async def test_restart_does_not_reset_no_progress_counter(tmp_path, monkeypatch):
    path = tmp_path / "board.sqlite"
    calls = []
    limits = LIMITS.model_copy(update={"max_no_progress": 2})
    for reopening in (False, True):
        store = SQLiteBlackboardStore(path)
        await store.initialize()
        try:
            execution = await setup(store, create=not reopening)
            registry = KnowledgeSourceRegistry()

            async def spam(invocation, view):
                calls.append(1)
                return KnowledgeOutput(proposals=(hint(view.intent, "noise", str(len(calls))),))

            registry.register(RuleKnowledgeSource(spec("spam"), spam))
            controller = BlackboardController(
                execution.kernel, registry, ledger=execution.ledger, limits=limits
            )
            runner = BlackboardRunner(controller)
            if not reopening:
                original = execution.kernel.commit

                async def crash(*args, _original=original, **kwargs):
                    await _original(*args, **kwargs)
                    raise Crash()

                with monkeypatch.context() as patch:
                    patch.setattr(execution.kernel, "commit", crash)
                    with pytest.raises(Crash):
                        await runner.run("board")
                assert (await execution.ledger.summary("board")).no_progress == 1
            else:
                result = await runner.run("board")
                assert result["stop_reason"] == "no_progress" and len(calls) == 2
                assert (await execution.ledger.summary("board")).no_progress == 2
                assert (await execution.ledger.run("board")).progress.exploration_remaining == 3
        finally:
            await store.close()
