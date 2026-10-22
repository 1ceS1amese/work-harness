"""Atomic read-set and predicate checks with independent knowledge publication."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from pydantic import ValidationError
from test_blackboard_kernel import Rig
from test_blackboard_kernel import rig as rig

from work_harness.blackboard.activation import activation_preconditions, make_view, match_rule
from work_harness.blackboard.kernel import BlackboardKernel
from work_harness.blackboard.models import Fact, Hint, Intent, IntentContent, Link
from work_harness.blackboard.protocol import (
    BlackboardDelta,
    BlackboardError,
    CommitContext,
    CommitReceipt,
    FactQuery,
    FactQueryPrecondition,
    HintQueryPrecondition,
    IdempotencyConflictError,
    IntentPrecondition,
    ObjectChange,
    ReadSelector,
    StaleSnapshotError,
)
from work_harness.knowledge_sources.base import ActivationRule, KnowledgeSourceSpec
from work_harness.storage.blackboard_sqlite import SQLiteBlackboardStore

SOURCE = CommitContext(source_id="source")


def activation_delta(
    rig: Rig,
    key: str,
    *proposals,
    intent=None,
    rule=None,
    topics=("latency",),
    kinds=("fact", "hint", "intent"),
):
    spec = KnowledgeSourceSpec(
        id="source",
        version="1",
        adapter_type="rule",
        emitted_kinds=("fact", "hint", "intent"),
        input_topics=topics,
        accepted_kinds=kinds,
        activation_rule=rule or ActivationRule(reason="Inspect the current branch"),
    )
    intent = intent or rig.board.by_id()[rig.board.root_intent_id]
    view = make_view(rig.board, intent, spec, rig.now[0])
    candidate = match_rule(spec, view)
    assert candidate is not None
    return BlackboardDelta(
        expected_revision=view.board_revision,
        idempotency_key=key,
        proposals=proposals,
        read_set=candidate.input_refs,
        concurrency="inputs",
        preconditions=activation_preconditions(spec, view),
    )


async def publish(rig: Rig, delta: BlackboardDelta, *, context=SOURCE):
    return await rig.kernel.commit(rig.board.board_id, delta, context=context)


async def test_independent_branches_publish_from_one_snapshot_without_overwrite(rig, monkeypatch):
    branches = await rig.submit(
        rig.intent("left", objective="Inspect left"), rig.intent("right", objective="Inspect right")
    )
    deltas = [
        activation_delta(
            rig,
            key,
            rig.hint(key, links=(Link(relation="contributes_to", target=branches[key].ref),)),
            intent=branches[key],
            topics=(),
        )
        for key in ("left", "right")
    ]
    before = rig.board
    original_commit = rig.store.commit
    barrier = asyncio.Barrier(2)

    async def together(request, *, writer):
        await barrier.wait()
        return await original_commit(request, writer=writer)

    monkeypatch.setattr(rig.store, "commit", together)
    receipts = await asyncio.gather(*(publish(rig, delta) for delta in deltas))
    assert sorted(r.board_revision for r in receipts) == [before.revision + 1, before.revision + 2]
    current = await rig.store.snapshot(before.board_id)
    assert len(current.objects) == len(before.objects) + 2
    assert all(ref.object_id in current.by_id() for r in receipts for ref in r.local_refs.values())
    assert await rig.store.snapshot(before.board_id, revision=before.revision) == before


async def test_unrelated_topic_does_not_invalidate_a_source_view(rig):
    delta = activation_delta(rig, "waiting", rig.hint("result"))
    unrelated = (await rig.submit(rig.hint("other", topic="other-topic")))["other"]
    receipt = await publish(rig, delta)
    current = await rig.store.snapshot(rig.board.board_id)
    assert unrelated.id in current.by_id()
    assert receipt.local_refs["result"].object_id in current.by_id()
    # Replaying the original request does not reevaluate now-changed read conditions.
    assert await publish(rig, delta) == receipt
    with pytest.raises(IdempotencyConflictError):
        await publish(rig, delta.model_copy(update={"preconditions": ()}))


async def test_changed_read_object_rejects_the_whole_delta(rig):
    fact = (await rig.submit(await rig.fact("input", claim_key="input")))["input"]
    delta = activation_delta(rig, "old-fact", rig.hint("one"), rig.intent("two"))
    await rig.submit(
        changes=(
            ObjectChange(target=fact.ref, action="retract_fact", reason="Evidence withdrawn"),
        ),
        context=CommitContext(source_id="verifier", can_retract_facts=True),
    )
    before = rig.board
    with pytest.raises(StaleSnapshotError, match="read set"):
        await publish(rig, delta)
    assert await rig.store.snapshot(before.board_id) == before
    assert await rig.store.receipt(before.board_id, "old-fact") is None


@pytest.mark.parametrize("change", ("fact", "hint", "goal", "expiry", "unrelated"))
async def test_conditions_are_evaluated_after_preparation_in_the_write_transaction(
    rig, monkeypatch, change
):
    if change == "expiry":
        await rig.submit(
            await rig.fact(
                "input", claim_key="input", valid_until=rig.now[0] + timedelta(seconds=5)
            )
        )
    rule = ActivationRule(
        reason="Explore missing evidence",
        absent_facts=(FactQuery(claim_key="missing"),),
        absent_hint_topics=("latency",),
    )
    delta = activation_delta(rig, "waiting", rig.hint("result"), rule=rule)
    prepared, release = asyncio.Event(), asyncio.Event()
    original_commit = rig.store.commit

    async def delayed(request, *, writer):
        if request.idempotency_key == "waiting":
            prepared.set()
            await release.wait()
        return await original_commit(request, writer=writer)

    monkeypatch.setattr(rig.store, "commit", delayed)
    operation = asyncio.create_task(publish(rig, delta))
    try:
        await asyncio.wait_for(prepared.wait(), 5)
        if change == "fact":
            await rig.submit(await rig.fact("new", claim_key="missing"))
        elif change == "hint":
            await rig.submit(rig.hint("new"))
        elif change == "goal":
            await rig.submit(await rig.fact())
        elif change == "expiry":
            rig.now[0] += timedelta(seconds=6)
        else:
            await rig.submit(rig.hint("other", topic="other-topic"))
        before = await rig.store.snapshot(rig.board.board_id)
    finally:
        release.set()
    if change == "unrelated":
        assert isinstance(await asyncio.wait_for(operation, 5), CommitReceipt)
    else:
        with pytest.raises(StaleSnapshotError):
            await asyncio.wait_for(operation, 5)
        assert await rig.store.snapshot(before.board_id) == before
        assert await rig.store.receipt(before.board_id, "waiting") is None


@pytest.mark.parametrize("shared", (False, True))
async def test_query_visibility_includes_root_evidence_but_not_other_branches(rig, shared):
    branches = await rig.submit(
        rig.intent("left", objective="Left"), rig.intent("right", objective="Right")
    )
    delta = activation_delta(
        rig,
        "left-read",
        rig.hint("result", links=(Link(relation="contributes_to", target=branches["left"].ref),)),
        intent=branches["left"],
        rule=ActivationRule(
            reason="Find missing evidence", absent_facts=(FactQuery(claim_key="missing"),)
        ),
    )
    await rig.submit(
        await rig.fact(
            "new",
            claim_key="missing",
            links=(
                Link(
                    relation="contributes_to", target=rig.root if shared else branches["right"].ref
                ),
            ),
        )
    )
    if shared:
        with pytest.raises(StaleSnapshotError, match="Fact query"):
            await publish(rig, delta)
    else:
        assert isinstance(await publish(rig, delta), CommitReceipt)


async def test_typed_fact_query_respects_scope_and_claim_type(rig):
    condition = FactQueryPrecondition(
        selector=ReadSelector(intent_id=rig.root.object_id),
        query=FactQuery(claim_key="input", claim_type="tool_observation", scope={"env": "prod"}),
        exists=False,
    )
    delta = BlackboardDelta(
        expected_revision=rig.board.revision,
        idempotency_key="scoped",
        concurrency="inputs",
        read_set=(rig.root,),
        preconditions=(condition,),
        proposals=(rig.hint(),),
    )
    await rig.submit(await rig.fact("other-env", claim_key="input", scope={"env": "dev"}))
    await rig.submit(
        await rig.fact(
            "other-type", claim_key="input", claim_type="user_statement", scope={"env": "prod"}
        )
    )
    assert isinstance(await publish(rig, delta), CommitReceipt)
    await rig.submit(await rig.fact("matches", claim_key="input", scope={"env": "prod"}))
    with pytest.raises(StaleSnapshotError, match="Fact query"):
        await publish(rig, delta.model_copy(update={"idempotency_key": "now-present"}))


@pytest.mark.parametrize("kind", ("fact", "hint"))
async def test_positive_query_expires_without_a_revision_change(rig, kind):
    expires = rig.now[0] + timedelta(seconds=5)
    if kind == "fact":
        await rig.submit(await rig.fact("input", claim_key="input", valid_until=expires))
        condition = FactQueryPrecondition(
            selector=ReadSelector(intent_id=rig.root.object_id),
            query=FactQuery(claim_key="input"),
            exists=True,
        )
    else:
        await rig.submit(rig.hint("input", expires_at=expires))
        condition = HintQueryPrecondition(
            selector=ReadSelector(intent_id=rig.root.object_id),
            topic="latency",
            exists=True,
        )
    delta = BlackboardDelta(
        expected_revision=rig.board.revision,
        idempotency_key="positive",
        concurrency="inputs",
        read_set=(rig.root,),
        preconditions=(condition,),
        proposals=(rig.hint("result"),),
    )
    rig.now[0] = expires
    with pytest.raises(StaleSnapshotError, match="query precondition"):
        await publish(rig, delta)
    assert await rig.store.snapshot(rig.board.board_id) == rig.board


async def test_target_is_rechecked_even_when_goal_becomes_true_only_with_time(rig):
    starts = rig.now[0] + timedelta(seconds=5)
    await rig.submit(await rig.fact(valid_from=starts))
    delta = BlackboardDelta(
        expected_revision=rig.board.revision,
        idempotency_key="goal-time",
        concurrency="inputs",
        read_set=(rig.root,),
        preconditions=(IntentPrecondition(intent_id=rig.root.object_id),),
        proposals=(rig.hint(),),
    )
    rig.now[0] = starts
    with pytest.raises(StaleSnapshotError, match="no longer open"):
        await publish(rig, delta)


async def test_independent_observations_reconcile_conflicts_against_the_latest_board(rig):
    deltas = [
        activation_delta(
            rig, key, await rig.fact(key, claim_key="temperature", value=value), kinds=("intent",)
        )
        for key, value in (("one", 10), ("two", 20))
    ]
    receipts = await asyncio.gather(*(publish(rig, delta) for delta in deltas))
    board = await rig.store.snapshot(rig.board.board_id)
    facts = [item for item in board.objects if isinstance(item, Fact)]
    assert len(facts) == 2 and all(item.lifecycle == "contested" for item in facts)
    assert sum(isinstance(item, Intent) and bool(item.blockers) for item in board.objects) == 1
    assert sorted(r.board_revision for r in receipts) == [2, 3]


async def test_object_limit_is_checked_after_rebasing_not_on_the_old_snapshot(rig):
    first = BlackboardDelta(
        expected_revision=rig.board.revision,
        idempotency_key="one",
        concurrency="inputs",
        read_set=(rig.root,),
        proposals=(rig.hint("one", topic="one"),),
    )
    second = first.model_copy(
        update={"idempotency_key": "two", "proposals": (rig.hint("two", topic="two"),)}
    )
    context = SOURCE.model_copy(update={"max_objects": 2})
    results = await asyncio.gather(
        publish(rig, first, context=context),
        publish(rig, second, context=context),
        return_exceptions=True,
    )
    assert sum(isinstance(result, CommitReceipt) for result in results) == 1
    error = next(result for result in results if isinstance(result, Exception))
    assert isinstance(error, BlackboardError) and "object limit" in str(error)
    assert len((await rig.store.snapshot(rig.board.board_id)).objects) == 2


def test_query_contract_does_not_accept_expressions_or_unconditional_input_mode():
    with pytest.raises(ValidationError):
        BlackboardDelta(expected_revision=1, idempotency_key="bad", concurrency="inputs")
    for predicate in (
        {"kind": "python", "expression": "True"},
        {"kind": "fact_query", "sql": "SELECT 1"},
    ):
        with pytest.raises(ValidationError):
            BlackboardDelta(expected_revision=1, idempotency_key="bad", preconditions=(predicate,))


async def test_absence_query_cannot_use_an_intent_from_another_board(rig):
    other = await rig.kernel.create(
        "other",
        IntentContent(
            topic="other",
            objective="Another run",
            acceptance=rig.board.objects[0].acceptance,
        ),
        source_id="user",
    )
    delta = BlackboardDelta(
        expected_revision=rig.board.revision,
        idempotency_key="foreign-query",
        concurrency="inputs",
        read_set=(rig.root,),
        proposals=(rig.hint(),),
        preconditions=(
            FactQueryPrecondition(
                selector=ReadSelector(intent_id=other.root_intent_id),
                query=FactQuery(claim_key="missing"),
                exists=False,
            ),
        ),
    )
    with pytest.raises(StaleSnapshotError, match="on this board"):
        await publish(rig, delta)
    assert await rig.store.receipt(rig.board.board_id, "foreign-query") is None


@pytest.mark.parametrize("blackboard_store", ("sqlite",), indirect=True)
async def test_sqlite_two_connections_merge_independent_inputs(rig, monkeypatch):
    second = SQLiteBlackboardStore(rig.store.path)
    await second.initialize()
    try:
        other = BlackboardKernel(
            second, rig.registry, owner_id=rig.kernel.owner_id, clock=rig.kernel.clock
        )
        original = SQLiteBlackboardStore.commit
        barrier = asyncio.Barrier(2)

        async def together(self, request, *, writer):
            await barrier.wait()
            return await original(self, request, writer=writer)

        monkeypatch.setattr(SQLiteBlackboardStore, "commit", together)
        deltas = [
            activation_delta(rig, key, rig.hint(key, topic=key), kinds=("intent",))
            for key in ("one", "two")
        ]
        receipts = await asyncio.gather(
            publish(rig, deltas[0]), other.commit(rig.board.board_id, deltas[1], context=SOURCE)
        )
        assert sorted(r.board_revision for r in receipts) == [2, 3]
        assert (
            sum(
                isinstance(item, Hint)
                for item in (await second.snapshot(rig.board.board_id)).objects
            )
            == 2
        )
    finally:
        await second.close()
