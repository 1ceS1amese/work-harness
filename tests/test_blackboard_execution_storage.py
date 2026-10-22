"""Durable execution records and accounting across attempts, failures and reopen."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from work_harness.blackboard.activation import make_view, match_rule
from work_harness.blackboard.controller import BLACKBOARD_WORKFLOW_REVISION, BlackboardController
from work_harness.blackboard.execution import (
    ActivationRecord,
    ActivationStore,
    BudgetExceeded,
    RunLimits,
)
from work_harness.blackboard.kernel import BlackboardKernel
from work_harness.blackboard.models import (
    AcceptanceCriterion,
    Evidence,
    FactContent,
    FactProposal,
    HintProposal,
    IntentContent,
    Link,
    digest,
)
from work_harness.blackboard.protocol import BlackboardError, VerificationError
from work_harness.blackboard.store import BlackboardStore
from work_harness.blackboard.verification import VerificationRegistry, Verifier
from work_harness.knowledge_sources.adapters.callback import (
    ObservationKnowledgeSource,
    VerifiedObservation,
)
from work_harness.knowledge_sources.base import (
    ActivationRule,
    KnowledgeOutput,
    KnowledgeSourceSpec,
    SourceLimits,
    UsageRecord,
)
from work_harness.knowledge_sources.registry import KnowledgeSourceRegistry
from work_harness.storage.blackboard_execution_sqlite import (
    SQLiteActivationStore,
    SQLiteVerificationReceiptStore,
)
from work_harness.storage.blackboard_factory import (
    build_activation_store,
    build_verification_receipt_store,
)
from work_harness.storage.blackboard_sqlite import SQLiteBlackboardStore
from work_harness.workflow.blackboard_graph import build_blackboard_graph

LIMITS = RunLimits(token_budget=500, cost_budget_usd=1, timeout_seconds=120, retry_delay_seconds=0)


def check(content, proof):
    if content.value != proof["value"] or content.evidence[0].content_digest != digest(proof):
        raise ValueError("observation does not prove the claim")
    return ("checked",)


@dataclass
class Execution:
    store: BlackboardStore
    ledger: ActivationStore
    kernel: BlackboardKernel
    now: datetime

    async def candidate(self, name="observe", *, tokens=100, cost=0.1, attempts=2):
        spec = KnowledgeSourceSpec(
            id=name,
            version="1",
            emitted_kinds=("fact", "hint"),
            activation_rule=ActivationRule(reason="Verify the answer"),
            adapter_type="observation",
            limits=SourceLimits(
                token_budget=tokens,
                cost_budget_usd=cost,
                max_attempts=attempts,
                timeout_seconds=30,
            ),
        )
        board = await self.store.snapshot("board")
        view = make_view(board, board.by_id()[board.root_intent_id], spec, self.now)
        candidate = match_rule(spec, view)
        assert candidate is not None
        return candidate, view, spec

    async def reserve(self, name="observe", *, limits=LIMITS, now=None) -> ActivationRecord:
        candidate, view, spec = await self.candidate(name)
        result = await self.ledger.reserve(candidate, view, spec, limits, now=now or self.now)
        assert result is not None
        return result

    def observation(self, record: ActivationRecord) -> VerifiedObservation:
        proof = {"value": 42}
        content = FactContent(
            topic="answer",
            statement="The verified answer is 42",
            claim_key="answer",
            claim_type="tool_observation",
            value=42,
            evidence=(
                Evidence(
                    source="tool",
                    locator="test:answer",
                    content_digest=digest(proof),
                    description="Trusted fixture observation",
                ),
            ),
            links=(Link(relation="contributes_to", target=record.view.intent.ref),),
        )
        return VerifiedObservation(
            local_id="answer", content=content, verifier_id="fixture", proof=proof
        )

    async def fact(self, record: ActivationRecord) -> FactProposal:
        observation = self.observation(record)
        receipt = await self.kernel.verification.attest(
            "board", observation.content, verifier_id="fixture", evidence=observation.proof
        )
        return FactProposal(
            **observation.content.model_dump(),
            local_id="answer",
            verification_receipt_id=receipt.receipt_id,
        )


async def setup(store: BlackboardStore, *, create=True) -> Execution:
    receipts = build_verification_receipt_store(store)
    ledger = build_activation_store(store)
    verification = VerificationRegistry(receipts)
    verification.register(
        Verifier("fixture", "1", "trusted-observation", ("tool_observation",), check)
    )
    kernel = BlackboardKernel(store, verification)
    if create:
        await kernel.create(
            "board",
            IntentContent(
                topic="answer",
                objective="Verify the answer",
                acceptance=(
                    AcceptanceCriterion(
                        id="answer",
                        description="The answer is 42",
                        claim_key="answer",
                        claim_type="tool_observation",
                        operator="equals",
                        expected=42,
                        required_checks=("checked",),
                    ),
                ),
            ),
            source_id="user",
        )
    return Execution(store, ledger, kernel, datetime.now(UTC))


@pytest.fixture
async def execution(blackboard_store):
    return await setup(blackboard_store)


async def test_activation_limit_is_atomic_and_duplicates_do_not_reserve_twice(execution):
    first, second = await execution.candidate("one"), await execution.candidate("two")
    limits = LIMITS.model_copy(update={"max_activations": 1})
    results = await asyncio.gather(
        execution.ledger.reserve(*first, limits, now=execution.now),
        execution.ledger.reserve(*second, limits, now=execution.now),
        return_exceptions=True,
    )
    assert sum(isinstance(result, BudgetExceeded) for result in results) == 1
    winner = next(result for result in results if isinstance(result, ActivationRecord))
    assert (
        await execution.ledger.reserve(
            winner.candidate, winner.view, winner.spec, limits, now=execution.now
        )
        is None
    )
    summary = await execution.ledger.summary("board")
    assert summary.attempts == 1 and summary.reserved_tokens == 0
    assert summary.reserved_cost_usd == 0


async def test_attempt_history_and_late_usage_remain_bound_to_the_original_attempt(execution):
    first = await execution.reserve()
    identity = first.candidate.id
    call = UsageRecord(call_id="call", input_tokens=10, cost_usd=0.01)
    await execution.ledger.record_usage(identity, 1, call)
    first_output = KnowledgeOutput(status="retryable_error", reason="temporary failure")
    await execution.ledger.save_output(identity, 1, first_output)
    await execution.ledger.finish(
        identity, attempt=1, status="retryable", reason="retry", progress=False, now=execution.now
    )
    second = await execution.reserve()
    assert second.attempt == 2
    await execution.ledger.record_usage(identity, 2, call)
    await execution.ledger.record_usage(identity, 1, call)
    await execution.ledger.record_usage(identity, 1, UsageRecord(call_id="late", input_tokens=5))
    with pytest.raises(BlackboardError, match="different outputs"):
        await execution.ledger.save_output(identity, 1, KnowledgeOutput(reason="different result"))
    await execution.ledger.save_output(identity, 1, first_output)
    await execution.ledger.finish(
        identity, attempt=1, status="completed", reason="late", progress=True
    )
    assert (await execution.ledger.get(identity)).attempt == 2
    assert (await execution.ledger.get(identity)).status == "running"
    history = await execution.ledger.attempts(identity)
    assert [(r.attempt, r.status) for r in history] == [(1, "retryable"), (2, "running")]
    assert history[0].output == first_output
    summary = await execution.ledger.summary("board")
    assert summary.tokens == 25 and summary.no_progress == 1 and summary.attempts == 2
    assert summary.reserved_tokens == 0
    with pytest.raises(BlackboardError, match="different content"):
        await execution.ledger.record_usage(
            identity, 1, call.model_copy(update={"input_tokens": 11})
        )
    assert (await execution.ledger.summary("board")).tokens == 25


async def test_large_usage_is_saved_without_rejection_and_survives_invalid_output(execution):
    record = await execution.reserve()
    identity = record.candidate.id
    call = UsageRecord(call_id="over", input_tokens=110, cost_usd=0.12)
    await execution.ledger.record_usage(identity, 1, call)
    await execution.ledger.save_output(
        identity, 1, KnowledgeOutput(status="failed", reason="invalid proposal")
    )
    await execution.ledger.finish(
        identity, attempt=1, status="rejected", reason="invalid proposal", progress=False
    )
    await execution.ledger.record_usage(identity, 1, call)
    summary = await execution.ledger.summary("board")
    assert summary.tokens == 110 and summary.cost_usd == 0.12 and summary.reserved_tokens == 0
    assert (await execution.store.snapshot("board")).revision == 1


async def test_uncertain_usage_stays_visible_without_blocking_new_attempts(execution):
    limits = LIMITS.model_copy(update={"token_budget": 1, "cost_budget_usd": 0})
    record = await execution.reserve(limits=limits)
    identity = record.candidate.id
    output = KnowledgeOutput(
        status="retryable_error", reason="provider timeout", usage_complete=False
    )
    await execution.ledger.save_output(identity, 1, output)
    await execution.ledger.finish(
        identity, attempt=1, status="retryable", reason="timeout", progress=False, now=execution.now
    )
    assert (await execution.reserve(limits=limits)).attempt == 2
    await execution.ledger.record_usage(
        identity, 1, UsageRecord(call_id="known-later", input_tokens=20, cached_tokens=5)
    )
    summary = await execution.ledger.summary("board")
    assert summary.tokens == 20 and summary.cached_tokens == 5
    assert summary.reserved_tokens == 0 and summary.unresolved_attempts == 1
    assert not summary.usage_complete
    await execution.ledger.complete_usage(identity, 1)
    assert (await execution.ledger.summary("board")).usage_complete
    assert (await execution.ledger.get(identity, 1)).output == output


async def test_limits_deadline_and_candidate_age_cannot_reset(execution):
    run = await execution.ledger.start_run("board", LIMITS, now=execution.now)
    first = await execution.reserve()
    ages = await execution.ledger.remember_candidates("board", (first.candidate.id,))
    later = execution.now + timedelta(hours=1)
    assert await execution.ledger.start_run(
        "board", LIMITS, now=later
    ) == await execution.ledger.run("board")
    assert (await execution.ledger.run("board")).deadline == run.deadline
    assert await execution.ledger.remember_candidates("board", (first.candidate.id, "new")) == {
        **ages,
        "new": 1,
    }
    with pytest.raises(BlackboardError, match="limits cannot change"):
        await execution.ledger.start_run(
            "board", LIMITS.model_copy(update={"token_budget": 1000}), now=later
        )
    candidate, view, spec = await execution.candidate("late")
    with pytest.raises(BudgetExceeded, match="deadline"):
        await execution.ledger.reserve(
            candidate, view, spec, LIMITS, now=later, run_deadline=later + timedelta(hours=1)
        )
    assert (await execution.ledger.summary("board")).attempts == 1


async def test_receipt_binding_and_immutability_are_identical_for_both_backends(execution):
    record = await execution.reserve()
    fact = await execution.fact(record)
    content = FactContent.model_validate(fact.model_dump(include=set(FactContent.model_fields)))
    receipts = execution.kernel.verification.receipts
    receipt = await receipts.get("board", fact.verification_receipt_id)
    assert receipt is not None
    await receipts.put("board", receipt)
    with pytest.raises(VerificationError, match="immutable"):
        await receipts.put("board", receipt.model_copy(update={"checks": ("invented",)}))
    with pytest.raises(VerificationError, match="does not cover"):
        await execution.kernel.verification.verify(
            "board", content.model_copy(update={"value": 43}), receipt.receipt_id
        )
    with pytest.raises(VerificationError, match="unknown verification"):
        await execution.kernel.verification.verify("other", content, receipt.receipt_id)


async def test_source_graph_can_use_the_durable_ledger_and_verification_store(execution):
    _, _, spec = await execution.candidate()

    async def observe(invocation, view):
        await invocation.record_usage(UsageRecord(call_id="observe", input_tokens=12))
        return (execution.observation(invocation.record),)

    registry = KnowledgeSourceRegistry()
    registry.register(ObservationKnowledgeSource(spec, observe))
    controller = BlackboardController(
        execution.kernel, registry, ledger=execution.ledger, limits=LIMITS
    )
    graph = build_blackboard_graph(controller, checkpointer=InMemorySaver())
    result = await graph.ainvoke(
        {"board_id": "board"}, config={"configurable": {"thread_id": "board"}}
    )
    assert result["status"] == "succeeded"
    assert (await execution.ledger.summary("board")).tokens == 12
    record = (await execution.ledger.records("board"))[0]
    assert record.status == "completed" and record.output.proposals[0].kind == "fact"


async def test_sqlite_pending_proposal_and_receipt_can_commit_after_reopen(tmp_path: Path):
    path = tmp_path / "board.sqlite"
    first = SQLiteBlackboardStore(path)
    await first.initialize()
    execution = await setup(first)
    record = await execution.reserve()
    identity = record.candidate.id
    run = await execution.ledger.run("board")
    attachment = await execution.kernel.put_content(
        "board", "\n候选内容\n", media_type="text/markdown"
    )
    fact = await execution.fact(record)
    hint = HintProposal(
        local_id="candidate",
        topic="answer",
        suggestion="Check the attached draft",
        applies_when="The draft has been generated",
        rationale="A source can continue verification",
        attachments=(attachment,),
        links=(Link(relation="contributes_to", target=record.view.intent.ref),),
    )
    output = KnowledgeOutput(proposals=(fact, hint))
    call = UsageRecord(call_id="model", input_tokens=15, output_tokens=5, cost_usd=0.02)
    await execution.ledger.record_usage(identity, 1, call)
    await execution.ledger.remember_candidates("board", (identity,))
    await execution.ledger.save_output(identity, 1, output)
    before = await execution.ledger.get(identity)
    await first.close()

    second = SQLiteBlackboardStore(path)
    await second.initialize()
    try:
        ledger = SQLiteActivationStore(second)
        assert await ledger.get(identity) == before
        assert (await ledger.run("board")).deadline == run.deadline
        assert (await ledger.run("board")).first_seen == {identity: 1}
        assert (await ledger.summary("board")).tokens == 20
        assert (await second.content("board", attachment)).body == "\n候选内容\n"
        await ledger.record_usage(identity, 1, call)
        # A persisted receipt remains usable without re-running its observation or verifier.
        verification = VerificationRegistry(SQLiteVerificationReceiptStore(second))
        kernel = BlackboardKernel(second, verification)
        controller = BlackboardController(
            kernel, KnowledgeSourceRegistry(), ledger=ledger, limits=LIMITS
        )
        await controller.commit(
            {
                "board_id": "board",
                "completed_activations": [{"activation_id": identity, "attempt": 1}],
            }
        )
        board = await second.snapshot("board")
        assert board.by_id()[board.root_intent_id].status == "satisfied"
        assert (await ledger.get(identity)).status == "completed"
        assert (await ledger.summary("board")).tokens == 20
        await controller.commit(
            {
                "board_id": "board",
                "completed_activations": [{"activation_id": identity, "attempt": 1}],
            }
        )
        assert await second.snapshot("board") == board
    finally:
        await second.close()


async def test_sqlite_reopen_keeps_retry_history_uncertain_reservations_and_deadlines(
    tmp_path: Path,
):
    path = tmp_path / "board.sqlite"
    first = SQLiteBlackboardStore(path)
    await first.initialize()
    execution = await setup(first)
    record = await execution.reserve()
    identity = record.candidate.id
    await execution.ledger.record_usage(
        identity, 1, UsageRecord(call_id="first", input_tokens=20, cached_tokens=5)
    )
    await execution.ledger.save_output(
        identity, 1, KnowledgeOutput(status="retryable_error", usage_complete=False)
    )
    await execution.ledger.finish(
        identity, attempt=1, status="retryable", reason="timeout", progress=False, now=execution.now
    )
    second_attempt = await execution.reserve()
    assert second_attempt.attempt == 2
    before = await execution.ledger.summary("board")
    history = await execution.ledger.attempts(identity)
    run = await execution.ledger.run("board")
    assert before.tokens == 20 and before.cached_tokens == 5
    assert before.unresolved_attempts == 1 and not before.usage_complete
    await first.close()
    reopened = SQLiteBlackboardStore(path)
    await reopened.initialize()
    try:
        ledger = SQLiteActivationStore(reopened)
        assert await ledger.attempts(identity) == history
        assert await ledger.summary("board") == before
        assert (
            await ledger.start_run("board", LIMITS, now=execution.now + timedelta(hours=1)) == run
        )
        await ledger.record_usage(identity, 1, UsageRecord(call_id="late", input_tokens=10))
        assert (await ledger.summary("board")).reserved_tokens == 0
        # A new controller without an old root checkpoint still uses the persisted deadline.
        kernel = BlackboardKernel(
            reopened, VerificationRegistry(), clock=lambda: execution.now + timedelta(hours=1)
        )
        controller = BlackboardController(
            kernel, KnowledgeSourceRegistry(), ledger=ledger, limits=LIMITS
        )
        assert (await controller.assess({"board_id": "board"}))["stop_reason"] == "timeout"
    finally:
        await reopened.close()


async def test_sqlite_separate_connections_cannot_overreserve(tmp_path: Path):
    path = tmp_path / "board.sqlite"
    first, second = SQLiteBlackboardStore(path), SQLiteBlackboardStore(path)
    await first.initialize()
    await second.initialize()
    try:
        execution = await setup(first)
        first_candidate = await execution.candidate("one")
        second_candidate = await execution.candidate("two")
        limits = LIMITS.model_copy(update={"max_activations": 1})
        results = await asyncio.gather(
            execution.ledger.reserve(*first_candidate, limits),
            SQLiteActivationStore(second).reserve(*second_candidate, limits),
            return_exceptions=True,
        )
        assert sum(isinstance(result, BudgetExceeded) for result in results) == 1
        assert sum(isinstance(result, ActivationRecord) for result in results) == 1
        assert (await SQLiteActivationStore(second).summary("board")).reserved_tokens == 0
    finally:
        await first.close()
        await second.close()


async def test_sqlite_failed_finish_rolls_back_status_and_progress_together(tmp_path: Path):
    path = tmp_path / "board.sqlite"
    store = SQLiteBlackboardStore(path)
    await store.initialize()
    try:
        execution = await setup(store)
        record = await execution.reserve()
        identity = record.candidate.id
        await execution.ledger.save_output(identity, 1, KnowledgeOutput(status="no_progress"))
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute(
                "CREATE TRIGGER reject_finish BEFORE UPDATE ON board_activation_attempts "
                "BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
            )
        with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
            await execution.ledger.finish(
                identity, attempt=1, status="no_progress", reason="no result", progress=False
            )
        assert (await execution.ledger.get(identity)).status == "result_ready"
        assert (await execution.ledger.summary("board")).no_progress == 0
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("DROP TRIGGER reject_finish")
        await execution.ledger.finish(
            identity, attempt=1, status="no_progress", reason="no result", progress=False
        )
        assert (await execution.ledger.summary("board")).no_progress == 1
    finally:
        await store.close()


async def test_sqlite_v1_upgrade_preserves_existing_knowledge(tmp_path: Path):
    path = tmp_path / "board.sqlite"
    store = SQLiteBlackboardStore(path)
    await store.initialize()
    await setup(store)
    initial = await store.snapshot("board")
    await store.close()
    # Version 1 had the same knowledge tables and none of the execution tables.
    with closing(sqlite3.connect(path)) as connection, connection:
        for table in (
            "board_usage",
            "board_activation_attempts",
            "board_activations",
            "board_control",
            "board_verification_receipts",
        ):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("UPDATE blackboard_schema SET version = 1")
    reopened = SQLiteBlackboardStore(path)
    await reopened.initialize()
    try:
        assert await reopened.snapshot("board") == initial
        execution = await setup(reopened, create=False)
        assert (await execution.reserve()).attempt == 1
        with closing(sqlite3.connect(path)) as connection:
            assert connection.execute("SELECT version FROM blackboard_schema").fetchone()[0] == 9
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        await reopened.close()


async def test_v4_view_history_remains_readable_without_resuming_the_old_execution_contract(
    tmp_path,
):
    path = tmp_path / "board.sqlite"
    first = SQLiteBlackboardStore(path)
    await first.initialize()
    execution = await setup(first)
    old = await execution.reserve()
    await execution.ledger.bind_contract("board", "blackboard-v2", digest([]))
    await first.close()
    with closing(sqlite3.connect(path)) as connection, connection:
        row = connection.execute("SELECT payload FROM board_activation_attempts").fetchone()
        payload = json.loads(row[0])
        payload["view"].pop("evaluated_at")
        connection.execute(
            "UPDATE board_activation_attempts SET payload = ?", (json.dumps(payload),)
        )
        connection.execute("UPDATE blackboard_schema SET version = 4")
    reopened = SQLiteBlackboardStore(path)
    await reopened.initialize()
    try:
        ledger = SQLiteActivationStore(reopened)
        recovered = await ledger.get(old.candidate.id)
        assert recovered.view.evaluated_at is None
        assert recovered.view.model_dump(exclude={"evaluated_at"}) == old.view.model_dump(
            exclude={"evaluated_at"}
        )
        assert recovered.candidate == old.candidate and recovered.deadline == old.deadline
        with pytest.raises(BlackboardError, match="incompatible"):
            await ledger.bind_contract("board", BLACKBOARD_WORKFLOW_REVISION, digest([]))
        assert (await ledger.run("board")).workflow_revision == "blackboard-v2"
        with closing(sqlite3.connect(path)) as connection:
            assert connection.execute("SELECT version FROM blackboard_schema").fetchone()[0] == 9
    finally:
        await reopened.close()
