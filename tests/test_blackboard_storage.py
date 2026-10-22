"""Blackboard persistence: stable history, atomic publication and restart behavior."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

import pytest

from work_harness.blackboard.kernel import BlackboardKernel
from work_harness.blackboard.models import (
    AcceptanceCriterion,
    Evidence,
    FactContent,
    FactProposal,
    HintProposal,
    IntentContent,
    IntentProposal,
    Link,
    digest,
)
from work_harness.blackboard.protocol import (
    BlackboardDelta,
    BlackboardError,
    BoardEvent,
    BoardSnapshot,
    CommitContext,
    CommitReceipt,
    ObjectChange,
    StaleSnapshotError,
)
from work_harness.blackboard.store import BlackboardStore, CommitRequest, Publication
from work_harness.blackboard.verification import VerificationRegistry, Verifier
from work_harness.storage.blackboard_sqlite import SQLiteBlackboardStore

SOURCE = CommitContext(source_id="test-source")


async def create_board(
    store: BlackboardStore, board_id: str = "board"
) -> tuple[BlackboardKernel, BoardSnapshot]:
    kernel = BlackboardKernel(store, VerificationRegistry())
    board = await kernel.create(
        board_id,
        IntentContent(
            topic="answer",
            objective="Read the answer",
            acceptance=(
                AcceptanceCriterion(
                    id="answer",
                    description="The file contains 42",
                    claim_key="answer",
                    claim_type="file_content",
                    operator="equals",
                    expected="42",
                    required_checks=("read",),
                ),
            ),
        ),
        source_id="user",
    )
    return kernel, board


def hint_delta(board: BoardSnapshot, key: str = "first", **fields) -> BlackboardDelta:
    root = board.by_id()[board.root_intent_id]
    return BlackboardDelta(
        expected_revision=board.revision,
        idempotency_key=key,
        proposals=(
            HintProposal(
                local_id="hint",
                topic="answer",
                suggestion="Read the source file",
                applies_when="The answer has not been verified",
                rationale="The input is available in a file",
                links=(Link(relation="contributes_to", target=root.ref),),
                **fields,
            ),
        ),
    )


async def test_history_and_attachment_bytes_are_stable(blackboard_store: BlackboardStore):
    store = blackboard_store
    kernel, initial = await create_board(store)
    body = "\n  核验材料\t\n"
    content_ref = await kernel.put_content("board", body, media_type="text/markdown")
    assert content_ref.content_digest == hashlib.sha256(body.encode()).hexdigest()
    assert content_ref.size_bytes == len(body.encode())
    assert (await store.content("board", content_ref)).body == body
    assert await kernel.put_content("board", body, media_type="text/markdown") == content_ref
    with pytest.raises(BlackboardError, match="different metadata"):
        await kernel.put_content("board", body, media_type="text/plain")

    other_kernel, _ = await create_board(store, "other")
    with pytest.raises(BlackboardError, match="missing"):
        await store.content("other", content_ref)
    assert await other_kernel.put_content("other", body, media_type="text/markdown") == content_ref

    receipt = await kernel.commit(
        "board", hint_delta(initial, attachments=(content_ref,)), context=SOURCE
    )
    active = await store.snapshot("board")
    hint = receipt.local_refs["hint"]
    await kernel.commit(
        "board",
        BlackboardDelta(
            expected_revision=2,
            idempotency_key="consumed",
            changes=(ObjectChange(target=hint, action="consume_hint", reason="Read completed"),),
        ),
        context=SOURCE,
    )
    assert (await store.snapshot("board")).by_id()[hint.object_id].status == "consumed"
    assert (await store.get_object("board", hint)).status == "active"
    assert await store.snapshot("board", revision=1) == initial
    assert await store.snapshot("board", revision=2) == active
    assert [event.sequence for event in await store.events("board", after=1)] == [2, 3]
    assert await store.events("board", after=3) == ()
    for bad_revision in (0, 4):
        with pytest.raises(BlackboardError, match="revision"):
            await store.snapshot("board", revision=bad_revision)


@pytest.mark.parametrize("invalid", ("overwrite", "missing_event", "cross_board", "remove_root"))
async def test_invalid_publication_cannot_diverge_from_history(
    blackboard_store: BlackboardStore, invalid: str
):
    store = blackboard_store
    kernel, initial = await create_board(store)
    root = initial.objects[0]
    updated = root.model_copy(update={"revision": 2, "status": "blocked"})
    if invalid == "overwrite":
        updated = updated.model_copy(update={"revision": 1})
    if invalid == "cross_board":
        updated = updated.model_copy(update={"board_id": "other"})
    snapshot = initial.model_copy(
        update={"revision": 2, "objects": () if invalid == "remove_root" else (updated,)}
    )
    event = BoardEvent(
        board_id="board",
        sequence=2,
        source_id="test-source",
        committed_at=root.created_at,
        changed_refs=() if invalid == "missing_event" else (updated.ref,),
    )
    receipt = CommitReceipt(
        board_id="board",
        board_revision=2,
        idempotency_key="invalid",
        request_digest=digest(invalid),
        committed_at=root.created_at,
        object_refs=(updated.ref,),
    )
    with pytest.raises(BlackboardError):
        await store.commit(
            CommitRequest(
                board_id="board",
                idempotency_key=receipt.idempotency_key,
                request_digest=receipt.request_digest,
                apply=lambda current: Publication(snapshot, event, receipt),
            ),
            writer=await kernel.writer("board"),
        )
    assert await store.snapshot("board") == initial
    assert await store.get_object("board", root.ref) == root
    assert len(await store.events("board")) == 1
    assert await store.receipt("board", "invalid") is None


async def test_sqlite_reopen_preserves_all_kinds_proofs_attachments_and_receipts(tmp_path: Path):
    path = tmp_path / "nested" / "board.sqlite"
    store = SQLiteBlackboardStore(path)
    await store.initialize()
    kernel, initial = await create_board(store)
    root = initial.objects[0]
    body = "\n42\n"
    attachment = await kernel.put_content("board", body, media_type="text/plain")

    def check_file(content, proof):
        if (
            content.value != proof.strip()
            or content.evidence[0].content_digest != hashlib.sha256(proof.encode()).hexdigest()
        ):
            raise ValueError("file does not support the claim")
        return ("read",)

    kernel.verification.register(Verifier("file", "1", "file-read", ("file_content",), check_file))
    fact = FactContent(
        topic="answer",
        statement="The file contains 42",
        claim_key="answer",
        claim_type="file_content",
        value="42",
        evidence=(
            Evidence(
                source="file",
                locator="input.txt",
                content_digest=attachment.content_digest,
                description="Exact input file content",
            ),
        ),
        attachments=(attachment,),
        links=(Link(relation="contributes_to", target=root.ref),),
    )
    verification = await kernel.verification.attest(
        "board", fact, verifier_id="file", evidence=body
    )
    delta = hint_delta(initial).model_copy(
        update={
            "proposals": (
                *hint_delta(initial).proposals,
                FactProposal(
                    **fact.model_dump(),
                    local_id="answer",
                    verification_receipt_id=verification.receipt_id,
                ),
                IntentProposal(
                    local_id="child",
                    topic="answer",
                    objective="Confirm the file content",
                    acceptance=root.acceptance,
                    root_intent_ref=root.ref,
                    parent_intent_ref=root.ref,
                    links=(Link(relation="contributes_to", target=root.ref),),
                ),
            ),
        }
    )
    receipt = await kernel.commit("board", delta, context=SOURCE)
    before = await store.snapshot("board")
    events = await store.events("board")
    await store.close()

    reopened = SQLiteBlackboardStore(path)
    await reopened.initialize()
    try:
        assert await reopened.snapshot("board") == before
        assert await reopened.snapshot("board", revision=1) == initial
        assert {item.kind for item in before.objects} == {"fact", "intent", "hint"}
        assert before.by_id()[before.root_intent_id].status == "satisfied"
        assert (
            await reopened.get_object("board", receipt.local_refs["answer"])
        ).verification == verification
        assert await reopened.get_object("board", root.ref) == root
        assert (await reopened.content("board", attachment)).body == body
        assert await reopened.events("board") == events
        # A receipt replay after restart needs neither the old kernel nor its verifier registry.
        recovered = BlackboardKernel(reopened, VerificationRegistry())
        assert await recovered.commit("board", delta, context=SOURCE) == receipt
        assert await reopened.events("board") == events
    finally:
        await reopened.close()


@pytest.mark.parametrize("same_request", (False, True))
async def test_sqlite_competing_connections_use_transactional_cas_and_idempotency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, same_request: bool
):
    path = tmp_path / "board.sqlite"
    first = SQLiteBlackboardStore(path)
    second = SQLiteBlackboardStore(path)
    await first.initialize()
    await second.initialize()
    kernel, initial = await create_board(first)
    other = BlackboardKernel(second, VerificationRegistry(), owner_id=kernel.owner_id)
    original_commit = SQLiteBlackboardStore.commit
    barrier = asyncio.Barrier(2)

    async def commit_together(self, request, *, writer):
        await barrier.wait()
        return await original_commit(self, request, writer=writer)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(SQLiteBlackboardStore, "commit", commit_together)
            results = await asyncio.gather(
                kernel.commit("board", hint_delta(initial), context=SOURCE),
                other.commit(
                    "board",
                    hint_delta(initial, "first" if same_request else "second"),
                    context=SOURCE,
                ),
                return_exceptions=True,
            )
        if same_request:
            assert isinstance(results[0], CommitReceipt)
            assert results[0] == results[1]
        else:
            assert sum(isinstance(result, StaleSnapshotError) for result in results) == 1
            assert sum(isinstance(result, CommitReceipt) for result in results) == 1
        assert len((await first.snapshot("board")).objects) == 2
        assert [event.sequence for event in await second.events("board")] == [1, 2]
    finally:
        await first.close()
        await second.close()


async def test_sqlite_failed_transaction_leaves_no_partial_publication(tmp_path: Path):
    path = tmp_path / "board.sqlite"
    store = SQLiteBlackboardStore(path)
    await store.initialize()
    kernel, initial = await create_board(store)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            """CREATE TRIGGER reject_receipt BEFORE INSERT ON board_commits
               BEGIN SELECT RAISE(ABORT, 'injected failure'); END"""
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        await kernel.commit("board", hint_delta(initial), context=SOURCE)
    await store.close()
    reopened = SQLiteBlackboardStore(path)
    await reopened.initialize()
    try:
        assert await reopened.snapshot("board") == initial
        assert len(await reopened.events("board")) == 1
        assert await reopened.receipt("board", "first") is None
        with closing(sqlite3.connect(path)) as connection, connection:
            assert (
                connection.execute("SELECT COUNT(*) FROM board_object_revisions").fetchone()[0] == 1
            )
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
            connection.execute("DROP TRIGGER reject_receipt")
        recovered = BlackboardKernel(reopened, VerificationRegistry())
        receipt = await recovered.commit("board", hint_delta(initial), context=SOURCE)
        assert receipt.board_revision == 2
    finally:
        await reopened.close()


async def test_sqlite_unknown_schema_is_not_overwritten(tmp_path: Path):
    path = tmp_path / "board.sqlite"
    store = SQLiteBlackboardStore(path)
    await store.initialize()
    kernel, initial = await create_board(store)
    await store.close()
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("UPDATE blackboard_schema SET version = 999")
    with pytest.raises(BlackboardError, match="unsupported blackboard schema"):
        await SQLiteBlackboardStore(path).initialize()
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT version FROM blackboard_schema").fetchone()[0] == 999
        assert connection.execute("SELECT revision FROM boards").fetchone()[0] == initial.revision


async def test_cancelled_storage_call_settles_before_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = tmp_path / "board.sqlite"
    store = SQLiteBlackboardStore(path)
    await store.initialize()
    kernel, _ = await create_board(store)
    entered, release = threading.Event(), threading.Event()
    original_connect = store._connect

    def pause():
        entered.set()
        if not release.wait(5):
            raise TimeoutError("test failed to release the database thread")
        return 0

    def connect():
        connection = original_connect()
        connection.create_function("pause_write", 0, pause)
        return connection

    monkeypatch.setattr(store, "_connect", connect)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            """CREATE TRIGGER pause_content BEFORE INSERT ON board_contents
               BEGIN SELECT pause_write(); END"""
        )
    operation = asyncio.create_task(kernel.put_content("board", "42", media_type="text/plain"))
    closing_task = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        operation.cancel()
        closing_task = asyncio.create_task(store.close())
        await asyncio.sleep(0)
        assert not closing_task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
        if closing_task:
            await closing_task
    reopened = SQLiteBlackboardStore(path)
    await reopened.initialize()
    try:
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("DROP TRIGGER pause_content")
        recovered = BlackboardKernel(reopened, VerificationRegistry())
        ref = await recovered.put_content("board", "42", media_type="text/plain")
        assert (await reopened.content("board", ref)).body == "42"
        assert (await reopened.snapshot("board")).revision == 1
    finally:
        await reopened.close()
