"""Human inputs cross the same commit fence and survive lost wakeups or cancellation."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from functools import partial

import httpx
import pytest
from test_blackboard_runtime import ROOT
from test_blackboard_runtime import sqlite_paths as sqlite_paths
from test_blackboard_storage import create_board

from work_harness.api.app import create_app
from work_harness.blackboard.inputs import (
    AddInformation,
    AuthorizeOperation,
    ConfirmStatement,
    HumanInput,
    ReviseGoal,
    register_human_verifier,
    submit_input,
)
from work_harness.blackboard.models import AcceptanceCriterion, Fact, digest
from work_harness.blackboard.protocol import (
    BlackboardDelta,
    BlackboardError,
    BoardClosedError,
    CommitContext,
    GoalRevision,
    IdempotencyConflictError,
    InputPendingError,
    StaleWriterError,
)
from work_harness.domain.enums import RunStatus
from work_harness.domain.models import TaskRequest
from work_harness.llm.mock import MockProvider
from work_harness.runtime import build_runtime
from work_harness.service import RunManager
from work_harness.storage.blackboard_sqlite import SQLiteBlackboardStore


def criterion(key="release.approved"):
    return AcceptanceCriterion(
        id="approval",
        description="The user explicitly confirms the release decision",
        claim_type="user_statement",
        claim_key=key,
        operator="equals",
        expected=True,
        required_checks=("user_confirmed",),
    )


def human_request():
    return TaskRequest(
        goal="Confirm the release decision",
        engine="blackboard",
        blackboard={"acceptance": [criterion().model_dump()]},
    )


async def confirmation(store, run_id, *, key="approval", claim_key="release.approved"):
    board = await store.snapshot(run_id)
    return HumanInput(
        idempotency_key=key,
        expected_revision=board.revision,
        target=board.by_id()[board.root_intent_id].ref,
        command=ConfirmStatement(
            claim_key=claim_key, statement="I approve this release", value=True
        ),
    )


async def test_input_commit_and_wakeup_are_atomic_and_idempotent(blackboard_store):
    store = blackboard_store
    kernel, _ = await create_board(store)
    register_human_verifier(kernel)
    request = await confirmation(store, "board")
    writer = await kernel.writer("board")
    receipt = await submit_input(
        kernel, "board", request, writer=writer, max_objects=10, max_child_intents=10
    )
    assert (await store.control("board")).wakeup_revision == receipt.board_revision == 2
    assert await store.pending_reconciliations() == ("board",)
    assert (
        await submit_input(
            kernel, "board", request, writer=writer, max_objects=10, max_child_intents=10
        )
        == receipt
    )
    assert (await store.snapshot("board")).revision == 2
    with pytest.raises(IdempotencyConflictError):
        await submit_input(
            kernel,
            "board",
            request.model_copy(update={"command": AddInformation(text="changed")}),
            writer=writer,
            max_objects=10,
            max_child_intents=10,
        )
    with pytest.raises(InputPendingError):
        await kernel.finalize("board", status="failed", reason="stale finish", observed_revision=1)
    await store.acknowledge_wakeup("board", 1, writer=writer)
    assert (await store.control("board")).wakeup_revision == 2
    await store.acknowledge_wakeup("board", 2, writer=writer)
    assert await store.pending_reconciliations() == ()


async def test_rejected_input_does_not_publish_or_wake(blackboard_store):
    store = blackboard_store
    kernel, initial = await create_board(store)
    register_human_verifier(kernel)
    request = await confirmation(store, "board")
    with pytest.raises(BlackboardError, match="object limit"):
        await submit_input(
            kernel,
            "board",
            request,
            writer=await kernel.writer("board"),
            max_objects=1,
            max_child_intents=10,
        )
    assert await store.snapshot("board") == initial
    assert await store.receipt("board", request.storage_key) is None
    assert (await store.control("board")).wakeup_revision == 0
    assert len(await store.events("board")) == 1


async def test_goal_revision_requires_user_authority_and_exact_root(blackboard_store):
    store = blackboard_store
    kernel, board = await create_board(store)
    change = GoalRevision(
        target=board.objects[0].ref,
        objective="Confirm release",
        acceptance=(criterion(),),
        reason="Explicit user change",
    )
    delta = BlackboardDelta(expected_revision=1, idempotency_key="revise", goal_revision=change)
    with pytest.raises(BlackboardError, match="only user"):
        await kernel.commit("board", delta, context=CommitContext(source_id="agent"))
    assert await store.snapshot("board") == board
    await kernel.commit("board", delta, context=CommitContext(source_id="user", is_user=True))
    revised = await store.snapshot("board")
    assert revised.objects[0].objective == "Confirm release"
    assert (await store.snapshot("board", revision=1)).objects[0].objective == board.objects[
        0
    ].objective
    assert (await store.events("board"))[-1].goal_revision == change


async def test_released_input_writer_cannot_publish_or_revoke_a_new_owner(blackboard_store):
    store = blackboard_store
    kernel, _ = await create_board(store)
    old = await kernel.writer("board")
    await store.release_writer(old)
    with pytest.raises(StaleWriterError):
        await store.check_writer(old)
    current = await store.claim_writer("board", "controller")
    await store.release_writer(old)
    await store.check_writer(current)
    assert (await store.control("board")).generation == current.generation


async def test_input_with_full_run_queue_keeps_wakeup_and_releases_short_lease(monkeypatch):
    async with build_runtime(ROOT, checkpoint_backend="memory") as runtime:
        runtime.config.runtime.workflow.blackboard.agent_roles = []
        manager = RunManager(runtime)
        try:
            run_id = await manager.start(human_request())
            await manager.wait(run_id)
            store = await runtime.blackboard_store()
            request = await confirmation(store, run_id)
            with monkeypatch.context() as patch:
                patch.setattr(manager, "_at_capacity", lambda: True)
                accepted = await manager.submit_blackboard_input(run_id, request)
                assert accepted.wakeup == "pending"
                assert (
                    await store.control(run_id)
                ).wakeup_revision == accepted.receipt.board_revision
                assert (await store.control(run_id)).owner_id is None
                assert (await runtime.run_store.get(run_id)).lease_owner is None
            assert await manager.recover_incomplete() == [run_id]
            assert (await manager.wait(run_id)).status == "succeeded"
        finally:
            await manager.close()


@pytest.mark.parametrize("backend", ("memory", "sqlite"))
@pytest.mark.parametrize("explicit_check", (True, False))
async def test_waiting_interrupt_accepts_input_once_and_finishes(
    backend, explicit_check, sqlite_paths
):
    async with build_runtime(
        ROOT, checkpoint_backend=backend, provider_store_backend="memory"
    ) as runtime:
        runtime.config.runtime.workflow.blackboard.agent_roles = []
        manager = RunManager(runtime)
        try:
            request = human_request()
            if not explicit_check:
                request.blackboard = request.blackboard.model_copy(
                    update={
                        "acceptance": (criterion().model_copy(update={"required_checks": ()}),),
                    }
                )
            run_id = await manager.start(request)
            waiting = await manager.wait(run_id)
            assert waiting.status == "waiting_human" and waiting.pending_intents
            store = await runtime.blackboard_store()
            engine = await manager._check_execution_version(await runtime.run_store.get(run_id))
            checkpoint = await engine.runner.graph.aget_state(engine.graph_config(run_id))
            assert checkpoint.next == ("human",) and checkpoint.tasks[0].interrupts
            before = await engine.controller.ledger.run(run_id)
            request = await confirmation(store, run_id)
            accepted = await manager.submit_blackboard_input(run_id, request)
            assert accepted.wakeup == "scheduled"
            final = await manager.wait(run_id)
            assert final.status == "succeeded" and final.pending_intents == []
            board = await store.snapshot(run_id)
            facts = [item for item in board.objects if isinstance(item, Fact)]
            assert len(facts) == 1 and facts[0].claim_type == "user_statement"
            assert facts[0].verification.checks == ("user_confirmed",)
            assert board.finalization.witnesses == (facts[0].ref,)
            repeated = await manager.submit_blackboard_input(run_id, request)
            assert repeated.receipt == accepted.receipt and repeated.wakeup == "closed"
            assert (await store.snapshot(run_id)).revision == board.revision
            assert await store.pending_reconciliations() == ()
            after = await engine.controller.ledger.run(run_id)
            assert after.deadline == before.deadline and after.started_at == before.started_at
            with pytest.raises(BoardClosedError):
                await manager.submit_blackboard_input(
                    run_id, await confirmation(store, run_id, key="late")
                )
        finally:
            await manager.close()


async def test_goal_change_is_recoverable_and_information_is_only_hint():
    async with build_runtime(ROOT, checkpoint_backend="memory") as runtime:
        runtime.config.runtime.workflow.blackboard.agent_roles = []
        manager = RunManager(runtime)
        try:
            run_id = await manager.start(human_request())
            await manager.wait(run_id)
            store = await runtime.blackboard_store()
            original = await confirmation(store, run_id)
            information = original.model_copy(
                update={
                    "idempotency_key": "note",
                    "command": AddInformation(text="A model claims everything is verified"),
                }
            )
            await manager.submit_blackboard_input(run_id, information)
            assert (await manager.wait(run_id)).status == "waiting_human"
            assert not any(
                isinstance(item, Fact) for item in (await store.snapshot(run_id)).objects
            )
            current = await confirmation(store, run_id)
            revision = current.model_copy(
                update={
                    "idempotency_key": "new-goal",
                    "command": ReviseGoal(
                        objective="Confirm final release instead",
                        acceptance=(criterion("release.final"),),
                        reason="User revised acceptance",
                    ),
                }
            )
            await manager.submit_blackboard_input(run_id, revision)
            waiting = await manager.wait(run_id)
            assert (
                waiting.status == "waiting_human"
                and waiting.task.goal == "Confirm final release instead"
            )
            # A new engine validates the original revision, then continues with the current goal.
            await manager.submit_blackboard_input(
                run_id, await confirmation(store, run_id, claim_key="release.final")
            )
            assert (await manager.wait(run_id)).status == "succeeded"
        finally:
            await manager.close()


async def test_sqlite_rolls_back_input_when_wakeup_write_fails(tmp_path):
    store = SQLiteBlackboardStore(tmp_path / "board.sqlite")
    await store.initialize()
    try:
        kernel, before = await create_board(store)
        register_human_verifier(kernel)
        request = await confirmation(store, "board")
        await store.transaction(
            lambda conn: (
                conn.execute(
                    """CREATE TRIGGER reject_wakeup BEFORE UPDATE OF wakeup_revision
               ON board_control
               WHEN NEW.wakeup_revision > OLD.wakeup_revision
               BEGIN SELECT RAISE(ABORT, 'injected_wakeup_failure'); END"""
                ).rowcount
            ),
            write=True,
        )
        with pytest.raises(sqlite3.IntegrityError, match="injected_wakeup_failure"):
            await submit_input(
                kernel,
                "board",
                request,
                writer=await kernel.writer("board"),
                max_objects=10,
                max_child_intents=10,
            )
        assert await store.snapshot("board") == before
        assert (await store.control("board")).wakeup_revision == 0
        assert await store.receipt("board", request.storage_key) is None
        assert len(await store.events("board")) == 1
        await store.transaction(
            lambda conn: conn.execute("DROP TRIGGER reject_wakeup").rowcount, write=True
        )
        receipt = await submit_input(
            kernel,
            "board",
            request,
            writer=await kernel.writer("board"),
            max_objects=10,
            max_child_intents=10,
        )
        assert receipt.board_revision == 2
    finally:
        await store.close()


async def test_operation_consent_is_bound_and_does_not_certify_execution(blackboard_store):
    store = blackboard_store
    kernel, board = await create_board(store)
    register_human_verifier(kernel)
    command = AuthorizeOperation(
        tool_name="read_file",
        arguments={"path": "README.md"},
        approved=True,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    request = HumanInput(
        idempotency_key="consent", expected_revision=1, target=board.objects[0].ref, command=command
    )
    receipt = await submit_input(
        kernel,
        "board",
        request,
        writer=await kernel.writer("board"),
        max_objects=10,
        max_child_intents=10,
    )
    fact = await store.get_object("board", receipt.local_refs["input"])
    assert fact.claim_type == "user_statement" and fact.claim_key == "operation.authorization"
    assert fact.verification.checks == ("operation_authorized",)
    assert fact.scope["arguments_digest"] == digest(command.arguments)
    assert fact.scope["intent_revision"] == str(request.target.revision)
    assert fact.value["arguments"] == command.arguments and fact.valid_until == command.expires_at
    assert (await store.snapshot("board")).by_id()[board.root_intent_id].status == "open"
    # An expired request cannot become a new authorization. A committed retry remains readable.
    expired = request.model_copy(
        update={
            "idempotency_key": "expired",
            "expected_revision": 2,
            "command": command.model_copy(
                update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)}
            ),
        }
    )
    with pytest.raises(BlackboardError, match="future"):
        await submit_input(
            kernel,
            "board",
            expired,
            writer=await kernel.writer("board"),
            max_objects=10,
            max_child_intents=10,
        )


async def test_lost_input_wakeup_is_recovered_after_sqlite_restart(sqlite_paths, monkeypatch):
    async with build_runtime(
        ROOT, checkpoint_backend="sqlite", provider_store_backend="memory"
    ) as first:
        first.config.runtime.workflow.blackboard.agent_roles = []
        manager = RunManager(first)
        try:
            run_id = await manager.start(human_request())
            await manager.wait(run_id)
            store = await first.blackboard_store()
            request = await confirmation(store, run_id)

            async def lose_notification(_run_id):
                return "pending"

            monkeypatch.setattr(manager, "_resume_blackboard_locked", lose_notification)
            accepted = await manager.submit_blackboard_input(run_id, request)
            assert accepted.wakeup == "pending"
            # Reproduce the window where an earlier pause overwrote the index after submission.
            await first.run_store.update_status(run_id, RunStatus.WAITING_HUMAN)
            revision = (await store.snapshot(run_id)).revision
        finally:
            await manager.close()
    async with build_runtime(
        ROOT, checkpoint_backend="sqlite", provider_store_backend="memory"
    ) as second:
        second.config.runtime.workflow.blackboard.agent_roles = []
        manager = RunManager(second)
        try:
            assert await manager.recover_incomplete() == [run_id]
            assert (await manager.wait(run_id)).status == "succeeded"
            store = await second.blackboard_store()
            assert (await store.snapshot(run_id)).revision == revision + 1
            assert (
                await manager.submit_blackboard_input(run_id, request)
            ).receipt == accepted.receipt
        finally:
            await manager.close()


async def test_cancelled_waiting_run_is_reconciled_without_resuming(sqlite_paths, monkeypatch):
    async with build_runtime(
        ROOT, checkpoint_backend="sqlite", provider_store_backend="memory"
    ) as first:
        first.config.runtime.workflow.blackboard.agent_roles = []
        manager = RunManager(first)
        try:
            run_id = await manager.start(human_request())
            await manager.wait(run_id)
            update = first.run_store.update_status

            async def lose_index(run_id, status):
                if status == RunStatus.CANCELLED:
                    raise RuntimeError("lost_cancel_index_update")
                return await update(run_id, status)

            monkeypatch.setattr(first.run_store, "update_status", lose_index)
            with pytest.raises(RuntimeError, match="lost_cancel_index"):
                await manager.cancel(run_id)
            store = await first.blackboard_store()
            final = (await store.snapshot(run_id)).finalization
            assert final.status == "cancelled"
            assert (await first.run_store.get(run_id)).status == "waiting_human"
            assert (await store.control(run_id)).status_pending
        finally:
            await manager.close()
    async with build_runtime(
        ROOT, checkpoint_backend="sqlite", provider_store_backend="memory"
    ) as second:
        second.config.runtime.workflow.blackboard.agent_roles = []
        manager = RunManager(second)
        try:
            assert await manager.recover_incomplete() == []
            assert (await manager.get(run_id)).status == "cancelled"
            store = await second.blackboard_store()
            assert (await store.snapshot(run_id)).finalization == final
            assert await store.pending_reconciliations() == ()
        finally:
            await manager.close()


@pytest.mark.parametrize("cancel", (False, True))
async def test_active_controller_accepts_input_without_stealing_writer(monkeypatch, cancel):
    entered, release = asyncio.Event(), asyncio.Event()
    complete = MockProvider.complete

    async def delayed(provider, request, *, model):
        entered.set()
        await release.wait()
        return await complete(provider, request, model=model)

    monkeypatch.setattr(MockProvider, "complete", delayed)
    async with build_runtime(ROOT, checkpoint_backend="memory") as runtime:
        manager = RunManager(runtime)
        try:
            run_id = await manager.start(human_request())
            await asyncio.wait_for(entered.wait(), 3)
            engine = manager._blackboard_engines[run_id]
            store = await runtime.blackboard_store()
            writer = await engine.kernel.writer(run_id)
            accepted = await manager.submit_blackboard_input(
                run_id, await confirmation(store, run_id)
            )
            assert accepted.wakeup == "active"
            assert (await store.control(run_id)).generation == writer.generation
            if cancel:
                await manager.cancel(run_id)
                final = (await store.snapshot(run_id)).finalization
                assert final.status == "cancelled"
                with pytest.raises(BoardClosedError):
                    await engine.controller.submit_input(
                        run_id, await confirmation(store, run_id, key="late")
                    )
            release.set()
            result = await manager.wait(run_id)
            assert result.status == ("cancelled" if cancel else "succeeded")
            assert (
                len(
                    [
                        item
                        for item in (await store.snapshot(run_id)).objects
                        if isinstance(item, Fact)
                    ]
                )
                == 1
            )
            if not cancel:
                assert result.usage.input_tokens > 0
        finally:
            release.set()
            await manager.close()


async def test_api_typed_input_rejects_forged_facts_and_stale_revisions(monkeypatch):
    monkeypatch.setattr(
        "work_harness.api.app.build_runtime", partial(build_runtime, checkpoint_backend="memory")
    )
    app = create_app(ROOT)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            run_id = (
                await client.post("/v1/runs", json=human_request().model_dump(mode="json"))
            ).json()["run_id"]
            base = f"/v1/runs/{run_id}"
            await asyncio.wait_for(client.get(base + "/events"), 10)
            state = (await client.get(base)).json()
            assert state["status"] == "waiting_human" and state["pending_intents"]
            summary = (await client.get(base + "/blackboard")).json()
            request = HumanInput(
                idempotency_key="api-input",
                expected_revision=summary["revision"],
                target={
                    "object_id": summary["root_intent"]["id"],
                    "revision": summary["root_intent"]["revision"],
                },
                command=ConfirmStatement(
                    claim_key="release.approved", statement="I approve this release", value=True
                ),
            )
            forged = request.model_dump(mode="json")
            forged["command"]["verification"] = {"checks": ["externally_verified"]}
            assert (await client.post(base + "/inputs", json=forged)).status_code == 422
            stale = request.model_dump(mode="json")
            stale["expected_revision"] = 1
            assert (await client.post(base + "/inputs", json=stale)).status_code == 409
            accepted = await client.post(base + "/inputs", json=request.model_dump(mode="json"))
            assert accepted.status_code == 200, accepted.text
            await asyncio.wait_for(client.get(base + "/events"), 10)
            assert (await client.get(base)).json()["status"] == "succeeded"
            retry = await client.post(base + "/inputs", json=request.model_dump(mode="json"))
            assert (
                retry.status_code == 200 and retry.json()["receipt"] == accepted.json()["receipt"]
            )
            assert (await client.post(base + "/resume", json={"engine": "blackboard"})).json()[
                "status"
            ] == "closed"
            assert (await client.post(base + "/resume", json={"approved": True})).status_code == 409
