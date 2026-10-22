"""Writer fencing, terminal snapshots and actual process ownership."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from work_harness.blackboard.controller import BlackboardController
from work_harness.blackboard.execution import RunLimits
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
from work_harness.blackboard.protocol import (
    BlackboardDelta,
    BlackboardProcessLockError,
    BoardClosedError,
    BoardSnapshot,
    CommitContext,
    GoalNotSatisfiedError,
    StaleWriterError,
    WriterToken,
)
from work_harness.blackboard.store import BlackboardStore
from work_harness.blackboard.verification import VerificationRegistry, Verifier
from work_harness.knowledge_sources.adapters.callback import RuleKnowledgeSource
from work_harness.knowledge_sources.base import (
    ActivationRule,
    KnowledgeOutput,
    KnowledgeSourceSpec,
    SourceLimits,
    UsageRecord,
)
from work_harness.knowledge_sources.registry import KnowledgeSourceRegistry
from work_harness.storage.blackboard_execution_sqlite import SQLiteActivationStore
from work_harness.storage.blackboard_factory import build_activation_store
from work_harness.storage.blackboard_sqlite import SQLiteBlackboardStore
from work_harness.workflow.blackboard_graph import build_blackboard_graph

SOURCE = CommitContext(source_id="test-source")


@dataclass
class Board:
    store: BlackboardStore
    kernel: BlackboardKernel
    initial: BoardSnapshot
    now: list[datetime]

    def hint(self, revision=1, key="hint"):
        return BlackboardDelta(
            expected_revision=revision,
            idempotency_key=key,
            proposals=(
                HintProposal(
                    local_id="hint",
                    topic="answer",
                    suggestion="Check the input",
                    applies_when="Evidence is missing",
                    rationale="A source can inspect the input",
                    links=(Link(relation="contributes_to", target=self.initial.objects[0].ref),),
                ),
            ),
        )

    async def prove(self, *, expires=None):
        content = FactContent(
            topic="answer",
            statement="The answer is 42",
            claim_type="tool_observation",
            claim_key="answer",
            value=42,
            evidence=(
                Evidence(
                    source="tool",
                    locator="test:answer",
                    content_digest=digest(42),
                    description="Trusted test observation",
                ),
            ),
            links=(Link(relation="contributes_to", target=self.initial.objects[0].ref),),
            valid_until=expires,
        )
        proof = await self.kernel.verification.attest(
            "board", content, verifier_id="fixture", evidence=42
        )
        snapshot = await self.store.snapshot("board")
        return await self.kernel.commit(
            "board",
            BlackboardDelta(
                expected_revision=snapshot.revision,
                idempotency_key="proof",
                proposals=(
                    FactProposal(
                        **content.model_dump(),
                        local_id="answer",
                        verification_receipt_id=proof.receipt_id,
                    ),
                ),
            ),
            context=SOURCE,
        )


async def setup(store: BlackboardStore) -> Board:
    def check(content, proof):
        if content.value != proof or content.evidence[0].content_digest != digest(proof):
            raise ValueError("unsupported claim")
        return ("checked",)

    verification = VerificationRegistry()
    verification.register(Verifier("fixture", "1", "observation", ("tool_observation",), check))
    now = [datetime.now(UTC)]
    kernel = BlackboardKernel(store, verification, clock=lambda: now[0])
    initial = await kernel.create(
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
    return Board(store, kernel, initial, now)


@pytest.fixture
async def board(blackboard_store):
    return await setup(blackboard_store)


async def test_takeover_fences_old_writer_before_idempotent_replay(board):
    first_token = await board.kernel.writer("board")
    delta = board.hint()
    original_receipt = await board.kernel.commit("board", delta, context=SOURCE)
    other = BlackboardKernel(board.store, board.kernel.verification)
    with pytest.raises(StaleWriterError, match="different writer"):
        await other.writer("board")
    new_token = await other.writer("board", takeover=True)
    assert new_token.generation == first_token.generation + 1
    with pytest.raises(StaleWriterError):
        await board.kernel.commit("board", delta, context=SOURCE)
    with pytest.raises(StaleWriterError):
        await board.kernel.put_content("board", "late draft", media_type="text/plain")
    assert await other.commit("board", delta, context=SOURCE) == original_receipt
    assert len(await board.store.events("board")) == 2


@pytest.mark.parametrize("interruption", ("takeover", "cancel", "seal"))
async def test_write_token_is_checked_again_inside_publication(board, monkeypatch, interruption):
    original_commit = board.store.commit
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_commit(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original_commit(*args, **kwargs)

    monkeypatch.setattr(board.store, "commit", delayed_commit)
    operation = asyncio.create_task(board.kernel.commit("board", board.hint(), context=SOURCE))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if interruption == "takeover":
            other = BlackboardKernel(board.store, board.kernel.verification)
            await other.writer("board", takeover=True)
        elif interruption == "cancel":
            await board.kernel.cancel("board")
        else:
            await board.kernel.finalize("board", status="failed", reason="stopped")
    finally:
        release.set()
    with pytest.raises(StaleWriterError if interruption == "takeover" else BoardClosedError):
        await operation
    assert (await board.store.snapshot("board")).objects == board.initial.objects
    assert await board.store.receipt("board", "hint") is None


async def test_success_is_verified_and_sealed_with_its_exact_snapshot(board):
    with pytest.raises(GoalNotSatisfiedError):
        await board.kernel.finalize("board", status="succeeded", reason="unsupported success")
    assert await board.store.snapshot("board") == board.initial
    receipt = await board.prove()
    before = await board.store.snapshot("board")
    final = await board.kernel.finalize("board", status="succeeded", reason="goal_satisfied")
    snapshot = await board.store.snapshot("board")
    assert final.finalized_at_revision == before.revision + 1 == snapshot.revision
    assert final.output == "The answer is 42"
    assert final.witnesses == (receipt.local_refs["answer"],)
    assert snapshot.finalization == final
    assert snapshot.objects == before.objects
    assert await board.store.snapshot("board", revision=before.revision) == before
    assert (await board.store.events("board"))[-1].finalization == final
    with pytest.raises(BoardClosedError):
        await board.kernel.commit("board", board.hint(snapshot.revision), context=SOURCE)
    with pytest.raises(BoardClosedError):
        await board.kernel.put_content("board", "late", media_type="text/plain")
    assert await board.store.receipt("board", "proof") == receipt
    assert await board.kernel.finalize("board", status="succeeded", reason="replay") == final
    assert await board.store.snapshot("board") == snapshot


async def test_expiry_between_assessment_and_sealing_cannot_claim_success(board):
    await board.prove(expires=board.now[0] + timedelta(seconds=1))
    controller = BlackboardController(board.kernel, KnowledgeSourceRegistry())
    assessed = await controller.assess({"board_id": "board"})
    assert assessed["status"] == "succeeded"
    board.now[0] += timedelta(seconds=2)
    assert await controller.finish({"board_id": "board", **assessed}) == {
        "route": "assess",
        "status": "running",
    }
    assert (await board.store.control("board")).finalization is None
    next_state = await controller.assess({"board_id": "board"})
    assert next_state["status"] != "succeeded"


async def test_cancel_preempts_owner_and_cannot_be_reopened(board):
    original_token = await board.kernel.writer("board")
    other = BlackboardKernel(board.store, board.kernel.verification)
    final = await other.cancel("board")
    assert final.status == "cancelled"
    control = await board.store.control("board")
    assert control.cancelled and control.owner_id is None
    assert control.generation > original_token.generation
    with pytest.raises(BoardClosedError):
        await board.kernel.commit("board", board.hint(), context=SOURCE)
    with pytest.raises(BoardClosedError):
        await board.store.claim_writer("board", "new-writer", takeover=True)
    assert await other.cancel("board", reason="duplicate") == final
    assert len(await board.store.events("board")) == 2
    assert (await board.store.snapshot("board")).objects == board.initial.objects


async def test_late_source_result_is_rejected_but_actual_usage_is_kept(board):
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(invocation, view):
        entered.set()
        await release.wait()
        await invocation.record_usage(UsageRecord(call_id="late-call", input_tokens=12))
        return KnowledgeOutput(proposals=board.hint().proposals)

    registry = KnowledgeSourceRegistry()
    registry.register(
        RuleKnowledgeSource(
            KnowledgeSourceSpec(
                id="slow",
                version="1",
                emitted_kinds=("hint",),
                adapter_type="rule",
                activation_rule=ActivationRule(reason="Inspect input"),
                limits=SourceLimits(token_budget=100),
            ),
            slow,
        )
    )
    ledger = build_activation_store(board.store)
    controller = BlackboardController(board.kernel, registry, ledger=ledger)
    graph = build_blackboard_graph(controller, checkpointer=InMemorySaver())
    running = asyncio.create_task(
        graph.ainvoke({"board_id": "board"}, config={"configurable": {"thread_id": "board"}})
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        final = await board.kernel.cancel("board")
    finally:
        release.set()
    result = await asyncio.wait_for(running, 5)
    assert (
        result["status"] == "cancelled"
        and result["finalized_at_revision"] == final.finalized_at_revision
    )
    assert (await ledger.summary("board")).tokens == 12
    assert (await ledger.records("board"))[0].status == "rejected"
    assert (await board.store.snapshot("board")).objects == board.initial.objects
    assert (await board.store.control("board")).finalization == final


async def test_competing_terminal_requests_publish_only_one_final_event(board):
    first, second = await asyncio.gather(
        board.kernel.cancel("board"),
        board.kernel.finalize("board", status="failed", reason="no_path"),
    )
    assert first == second
    assert len(await board.store.events("board")) == 2
    assert (await board.store.snapshot("board")).finalization == first


async def test_sqlite_terminal_snapshot_survives_reopen_and_later_expiry(tmp_path: Path):
    path = tmp_path / "board.sqlite"
    store = SQLiteBlackboardStore(path)
    await store.initialize()
    board = await setup(store)
    await board.prove(expires=board.now[0] + timedelta(seconds=1))
    final = await board.kernel.finalize("board", status="succeeded", reason="goal_satisfied")
    await store.close()
    reopened = SQLiteBlackboardStore(path)
    await reopened.initialize()
    try:
        kernel = BlackboardKernel(
            reopened, VerificationRegistry(), clock=lambda: board.now[0] + timedelta(days=1)
        )
        controller = BlackboardController(kernel, KnowledgeSourceRegistry())
        state = await controller.assess({"board_id": "board"})
        result = await controller.finish({"board_id": "board", **state})
        assert result["status"] == "succeeded" and result["final_output"] == final.output
        assert result["finalized_at_revision"] == final.finalized_at_revision
        with pytest.raises(BoardClosedError):
            await kernel.writer("board", takeover=True)
        assert (await reopened.snapshot("board")).finalization == final
    finally:
        await reopened.close()


async def test_sqlite_failed_seal_rolls_back_barrier_event_and_snapshot(tmp_path: Path):
    path = tmp_path / "board.sqlite"
    store = SQLiteBlackboardStore(path)
    await store.initialize()
    try:
        board = await setup(store)
        token = await board.kernel.writer("board")
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute(
                "CREATE TRIGGER reject_seal BEFORE UPDATE ON board_control "
                "WHEN NEW.finalization IS NOT NULL "
                "BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
            )
        with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
            await board.kernel.cancel("board")
        assert await store.snapshot("board") == board.initial
        assert len(await store.events("board")) == 1
        assert (await store.control("board")).finalization is None
        await store.check_writer(token)
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("DROP TRIGGER reject_seal")
        assert (await board.kernel.cancel("board")).status == "cancelled"
    finally:
        await store.close()


async def child_probe(path: Path) -> str:
    code = """
import asyncio, sys
from pathlib import Path
from work_harness.blackboard.protocol import BlackboardProcessLockError
from work_harness.storage.blackboard_sqlite import SQLiteBlackboardStore
async def main():
    store = SQLiteBlackboardStore(Path(sys.argv[1]))
    try:
        await store.initialize()
    except BlackboardProcessLockError:
        print('locked', flush=True)
    else:
        print('acquired', flush=True)
        await store.close()
asyncio.run(main())
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        code,
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 15)
        assert process.returncode == 0, stderr.decode()
        return stdout.decode().strip()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def test_sqlite_process_lock_is_shared_locally_and_blocks_other_processes(tmp_path: Path):
    path = tmp_path / "board.sqlite"
    first, second = SQLiteBlackboardStore(path), SQLiteBlackboardStore(path)
    await first.initialize()
    await second.initialize()
    try:
        assert await child_probe(path) == "locked"
        await first.close()
        assert await child_probe(path) == "locked"
    finally:
        await first.close()
        await second.close()
    assert await child_probe(path) == "acquired"
    assert await asyncio.to_thread(Path(str(path) + ".lock").exists)


async def test_process_death_releases_lock_and_new_session_fences_old_token(tmp_path: Path):
    path = tmp_path / "board.sqlite"
    code = """
import asyncio, sys
from pathlib import Path
from work_harness.blackboard.kernel import BlackboardKernel
from work_harness.blackboard.models import IntentContent, AcceptanceCriterion
from work_harness.blackboard.verification import VerificationRegistry
from work_harness.storage.blackboard_sqlite import SQLiteBlackboardStore
async def main():
    store = SQLiteBlackboardStore(Path(sys.argv[1]))
    await store.initialize()
    kernel = BlackboardKernel(store, VerificationRegistry())
    goal = IntentContent(topic='answer', objective='Find the answer', acceptance=(
        AcceptanceCriterion(id='answer', description='Verified answer',
                            claim_key='answer', claim_type='tool_observation'),
    ))
    await kernel.create('board', goal, source_id='user')
    print((await kernel.writer('board')).model_dump_json(), flush=True)
    await asyncio.to_thread(sys.stdin.readline)
asyncio.run(main())
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        code,
        str(path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        line = await asyncio.wait_for(process.stdout.readline(), 15)
        token = WriterToken.model_validate(json.loads(line))
        with pytest.raises(BlackboardProcessLockError):
            await SQLiteBlackboardStore(path).initialize()
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()
    reopened = SQLiteBlackboardStore(path)
    await reopened.initialize()
    try:
        kernel = BlackboardKernel(reopened, VerificationRegistry())
        replacement = await kernel.writer("board")
        assert replacement.generation == token.generation + 1
        assert replacement.session_id != token.session_id
        with pytest.raises(StaleWriterError):
            await reopened.check_writer(token)
        assert (await reopened.snapshot("board")).revision == 1
    finally:
        await reopened.close()


async def test_v2_upgrade_retains_execution_limits_and_creates_writer_control(tmp_path: Path):
    path = tmp_path / "board.sqlite"
    first = SQLiteBlackboardStore(path)
    await first.initialize()
    await setup(first)
    execution = await SQLiteActivationStore(first).start_run("board", RunLimits(token_budget=123))
    await first.close()
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("ALTER TABLE board_control RENAME TO board_control_current")
        connection.execute(
            "CREATE TABLE board_control "
            "(board_id TEXT PRIMARY KEY REFERENCES boards(board_id), payload TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO board_control SELECT board_id, payload FROM board_control_current"
        )
        connection.execute("DROP TABLE board_control_current")
        connection.execute("UPDATE blackboard_schema SET version = 2")
    reopened = SQLiteBlackboardStore(path)
    await reopened.initialize()
    try:
        assert await SQLiteActivationStore(reopened).run("board") == execution
        kernel = BlackboardKernel(reopened, VerificationRegistry())
        assert (await kernel.writer("board")).generation == 1
        with closing(sqlite3.connect(path)) as connection:
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        await reopened.close()
