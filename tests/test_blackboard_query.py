"""Public reads stay on committed snapshots across changes, gaps and restarts."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from functools import partial

import httpx
import pytest
from langgraph.checkpoint.base import empty_checkpoint
from test_blackboard_runtime import ROOT, draft_request
from test_blackboard_storage import SOURCE, create_board, hint_delta

from work_harness.api.app import create_app
from work_harness.api.blackboard import stream_blackboard
from work_harness.blackboard.execution import MemoryActivationStore
from work_harness.blackboard.models import IntentProposal, Link
from work_harness.blackboard.protocol import BlackboardDelta, HistoryUnavailableError, ObjectChange
from work_harness.blackboard.query import (
    BlackboardQuery,
    ObjectFilter,
    QueryConflictError,
    QueryNotFoundError,
    ReadCursor,
    event_cursor,
)
from work_harness.runtime import build_runtime
from work_harness.service import RunManager
from work_harness.storage.base import RunExecutionVersion, RunRecord
from work_harness.storage.blackboard_execution_sqlite import SQLiteActivationStore
from work_harness.storage.blackboard_memory import MemoryBlackboardStore
from work_harness.storage.blackboard_postgres import PostgresBlackboardStore
from work_harness.storage.blackboard_sqlite import SQLiteBlackboardStore


async def consume(kernel, store, ref, key="consume"):
    board = await store.snapshot("board")
    await kernel.commit(
        "board",
        BlackboardDelta(
            expected_revision=board.revision,
            idempotency_key=key,
            changes=(ObjectChange(target=ref, action="consume_hint", reason="Read completed"),),
        ),
        context=SOURCE,
    )


@pytest.mark.parametrize("backend", ("memory", "sqlite"))
async def test_object_activation_provenance_is_version_bound_and_survives_reopen(
    backend, tmp_path, monkeypatch
):
    for name, filename in (
        ("WORK_HARNESS_CHECKPOINT_PATH", "checkpoints.sqlite"),
        ("WORK_HARNESS_RUN_STORE_PATH", "runs.sqlite"),
        ("WORK_HARNESS_BLACKBOARD_PATH", "board.sqlite"),
    ):
        monkeypatch.setenv(name, str(tmp_path / filename))
    async with build_runtime(
        ROOT, checkpoint_backend=backend, provider_store_backend="memory"
    ) as runtime:
        manager = RunManager(runtime)
        try:
            run_id = await manager.start(draft_request())
            assert (await manager.wait(run_id)).status == "succeeded"
            store = await runtime.blackboard_store()
            ledger = (
                SQLiteActivationStore(store)
                if isinstance(store, SQLiteBlackboardStore)
                else MemoryActivationStore()
            )
            ledger.bind(store)
            query = BlackboardQuery(store, run_id, ledger=ledger)
            snapshot = await query.summary()
            initial = await query.object(snapshot.root_intent.id, object_revision=1)
            assert initial.activation is None
            fact = next(item for item in (await query.objects()).items if item.kind == "fact")
            detail = await query.object(fact.id)
            assert detail.activation is not None
            assert detail.activation.activation_id == detail.item.event.activation_id
            assert detail.activation.source_id == fact.created_by
            assert detail.activation.reason
            for ref in detail.activation.input_refs:
                original = await query.object(
                    ref.object_id,
                    object_revision=ref.revision,
                    at_revision=detail.item.event.sequence,
                )
                assert original.item.event.sequence < detail.item.event.sequence
            with pytest.raises(QueryNotFoundError):
                await query.object(fact.id, at_revision=1)
            expected = detail.activation
        finally:
            await manager.close()
    if backend == "sqlite":
        reopened = SQLiteBlackboardStore(tmp_path / "board.sqlite")
        await reopened.initialize()
        try:
            query = BlackboardQuery(reopened, run_id, ledger=SQLiteActivationStore(reopened))
            assert (await query.object(fact.id)).activation == expected
        finally:
            await reopened.close()


async def remove_event(store, sequence):
    if isinstance(store, MemoryBlackboardStore):
        store._events["board"] = [e for e in store._events["board"] if e.sequence != sequence]
    elif isinstance(store, PostgresBlackboardStore):

        async def corrupt(conn):
            # Remove referencing receipt first to simulate history retention without
            # disabling PostgreSQL foreign-key protection for any other session.
            await conn.execute(
                "DELETE FROM work_harness_board_commits "
                "WHERE board_id = %s AND board_revision = %s",
                ("board", sequence),
            )
            await conn.execute(
                "DELETE FROM work_harness_board_events WHERE board_id = %s AND sequence = %s",
                ("board", sequence),
            )

        await store.transaction(corrupt, board_id="board", write=True)
    else:
        # Simulate lost/compacted history, deliberately bypassing normal FK protection.
        def corrupt():
            with closing(sqlite3.connect(store.path)) as conn, conn:
                conn.execute(
                    "DELETE FROM board_events WHERE board_id = ? AND sequence = ?",
                    ("board", sequence),
                )

        await asyncio.to_thread(corrupt)


async def test_object_pages_pin_snapshot_and_filters_during_commits(blackboard_store):
    store = blackboard_store
    kernel, initial = await create_board(store)
    first = await kernel.commit("board", hint_delta(initial), context=SOURCE)
    board = await store.snapshot("board")
    await kernel.commit("board", hint_delta(board, "second", tags=("second",)), context=SOURCE)
    query = BlackboardQuery(store, "board")
    page = await query.objects(filters=ObjectFilter(kind="hint", status="active"), limit=1)
    assert page.total == 2 and page.next_cursor
    await consume(kernel, store, first.local_refs["hint"])
    board = await store.snapshot("board")
    await kernel.commit("board", hint_delta(board, "third", tags=("third",)), context=SOURCE)
    rest = await query.objects(cursor=page.next_cursor, limit=1)
    assert rest.revision == page.revision == 3
    assert rest.next_cursor is None
    assert len({item.id for item in page.items + rest.items}) == 2
    assert all(item.status == "active" for item in page.items + rest.items)
    assert (await query.objects(filters=ObjectFilter(status="active"))).revision == 5
    for kwargs in (
        {"filters": ObjectFilter(kind="intent")},
        {"at_revision": 5},
    ):
        with pytest.raises(ValueError):
            await query.objects(cursor=page.next_cursor, **kwargs)
    summary = await query.summary()
    assert summary.counts == {"fact": 0, "intent": 1, "hint": 3}
    assert (await query.objects(cursor=summary.objects_cursor)).revision == summary.revision
    assert (await query.events(after=summary.event_cursor)).items == ()


async def test_object_history_and_detail_bound_board_and_object_versions(blackboard_store):
    store = blackboard_store
    kernel, initial = await create_board(store)
    receipt = await kernel.commit("board", hint_delta(initial), context=SOURCE)
    ref = receipt.local_refs["hint"]
    await consume(kernel, store, ref)
    query = BlackboardQuery(store, "board")
    page = await query.history(ref.object_id, limit=1)
    assert page.total == 2 and page.items[0].event.sequence == 2
    await kernel.commit("board", hint_delta(await store.snapshot("board"), "later"), context=SOURCE)
    last = await query.history(ref.object_id, cursor=page.next_cursor)
    assert last.revision == page.revision == 3
    assert last.items[0].object.status == "consumed"
    assert last.items[0].event.sequence == 3 and last.next_cursor is None
    old = await query.object(ref.object_id, object_revision=1)
    assert old.item.object.status == "active"
    assert old.item.event.sequence == 2
    assert (await query.object(ref.object_id, at_revision=2)).item == old.item
    with pytest.raises(QueryNotFoundError):
        await query.object(ref.object_id, object_revision=2, at_revision=2)
    with pytest.raises(QueryNotFoundError):
        await query.object(ref.object_id, at_revision=1)
    with pytest.raises(ValueError):
        await query.history(initial.root_intent_id, cursor=page.next_cursor)


async def test_intent_filter_includes_descendants_and_their_contributions(blackboard_store):
    store = blackboard_store
    kernel, initial = await create_board(store)
    root = initial.objects[0]
    child = await kernel.commit(
        "board",
        BlackboardDelta(
            expected_revision=1,
            idempotency_key="child",
            proposals=(
                IntentProposal(
                    local_id="child",
                    topic="branch",
                    objective="Read one input",
                    acceptance=root.acceptance,
                    root_intent_ref=root.ref,
                    parent_intent_ref=root.ref,
                    links=(Link(relation="contributes_to", target=root.ref),),
                ),
            ),
        ),
        context=SOURCE,
    )
    child_ref = child.local_refs["child"]
    from work_harness.blackboard.models import HintProposal

    await kernel.commit(
        "board",
        BlackboardDelta(
            expected_revision=2,
            idempotency_key="nested",
            proposals=(
                HintProposal(
                    local_id="nested",
                    topic="branch",
                    suggestion="Read child input",
                    applies_when="Input missing",
                    rationale="Inspect the file",
                    links=(Link(relation="contributes_to", target=child_ref),),
                ),
            ),
        ),
        context=SOURCE,
    )
    query = BlackboardQuery(store, "board")
    assert (await query.objects(filters=ObjectFilter(intent_id=root.id))).total == 3
    assert (await query.objects(filters=ObjectFilter(intent_id=child_ref.object_id))).total == 2
    assert (await query.objects(filters=ObjectFilter(topic="branch", kind="intent"))).total == 1
    with pytest.raises(QueryNotFoundError):
        await query.objects(filters=ObjectFilter(intent_id="missing"))


async def test_only_committed_attachment_is_public_and_bytes_survive(blackboard_store):
    store = blackboard_store
    kernel, initial = await create_board(store)
    hidden = await kernel.put_content("board", "PRIVATE_UNCOMMITTED", media_type="text/plain")
    body = "\n  public attachment\t\n"
    public = await kernel.put_content("board", body, media_type="text/plain")
    await kernel.commit("board", hint_delta(initial, attachments=(public,)), context=SOURCE)
    query = BlackboardQuery(store, "board")
    assert (await query.content(public.content_digest)).content.body == body
    with pytest.raises(QueryNotFoundError):
        await query.content(hidden.content_digest)
    with pytest.raises(QueryNotFoundError):
        await query.content(public.content_digest, at_revision=1)
    await create_board(store, "other")
    with pytest.raises(QueryNotFoundError):
        await BlackboardQuery(store, "other").content(public.content_digest)


async def test_bounded_event_replay_and_explicit_gap_errors(blackboard_store):
    store = blackboard_store
    kernel, initial = await create_board(store)
    receipt = await kernel.commit("board", hint_delta(initial), context=SOURCE)
    await consume(kernel, store, receipt.local_refs["hint"])
    query = BlackboardQuery(store, "board")
    first = await query.events(limit=1)
    assert first.has_more and first.revision == 3
    rest = await query.events(after=first.next_cursor)
    assert [item.sequence for item in rest.items] == [2, 3]
    assert not rest.has_more
    with pytest.raises(QueryConflictError):
        await query.events(after=event_cursor("board", 4))
    with pytest.raises(ValueError):
        await query.events(after=event_cursor("other", 1))
    await remove_event(store, 2)
    with pytest.raises(HistoryUnavailableError):
        await query.events(after=first.next_cursor)
    with pytest.raises(HistoryUnavailableError):
        await query.history(receipt.local_refs["hint"].object_id)
    # A latest snapshot is still obtainable, and its cursor skips unavailable history explicitly.
    summary = await query.summary()
    assert (await query.events(after=summary.event_cursor)).items == ()


@pytest.mark.parametrize("bad", ("", "!", "x" * 16385, "e30", "77-9"))
def test_invalid_cursors_are_rejected(bad):
    with pytest.raises(ValueError):
        ReadCursor.decode(bad, board_id="board", kind="events")


async def test_sqlite_read_cursors_survive_reopen(tmp_path):
    path = tmp_path / "board.sqlite"
    store = SQLiteBlackboardStore(path)
    await store.initialize()
    try:
        kernel, initial = await create_board(store)
        await kernel.commit("board", hint_delta(initial), context=SOURCE)
        query = BlackboardQuery(store, "board")
        objects = await query.objects(limit=1)
        events = await query.events(limit=1)
    finally:
        await store.close()
    store = SQLiteBlackboardStore(path)
    await store.initialize()
    try:
        query = BlackboardQuery(store, "board")
        assert (await query.objects(cursor=objects.next_cursor)).revision == 2
        assert [item.sequence for item in (await query.events(after=events.next_cursor)).items] == [
            2
        ]
    finally:
        await store.close()


def frames(text):
    return [
        dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        for block in text.strip().split("\n\n")
        if block and not block.startswith(":")
    ]


async def test_api_queries_and_sse_resume_hide_private_state(monkeypatch):
    monkeypatch.setattr(
        "work_harness.api.app.build_runtime", partial(build_runtime, checkpoint_backend="memory")
    )
    managers = []

    def capture_manager(runtime):
        manager = RunManager(runtime)
        managers.append(manager)
        return manager

    monkeypatch.setattr("work_harness.api.app.RunManager", capture_manager)
    app = create_app(ROOT)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            accepted = await client.post("/v1/runs", json=draft_request().model_dump(mode="json"))
            assert accepted.status_code == 202, accepted.text
            run_id = accepted.json()["run_id"]
            base = f"/v1/runs/{run_id}"
            response = await asyncio.wait_for(client.get(base + "/events"), timeout=10)
            assert response.status_code == 200
            items = frames(response.text)
            assert all(item["event"] == "blackboard" for item in items)
            events = [json.loads(item["data"]) for item in items]
            assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
            assert events[-1]["event_type"] == "finalized"
            runtime = managers[0].runtime
            private = "PRIVATE_AGENT_MESSAGE_SENTINEL"
            checkpoint = empty_checkpoint()
            checkpoint["channel_values"] = {"messages": [{"content": private}]}
            checkpoint["channel_versions"] = {"messages": 1}
            await runtime.checkpointer.aput(
                {"configurable": {"thread_id": run_id, "checkpoint_ns": "private-agent"}},
                checkpoint,
                {"source": "update", "step": 1, "parents": {}},
                {"messages": 1},
            )
            store = await runtime.blackboard_store()
            ledger = store._activation_data.boards[run_id]
            for key, record in ledger.attempts.items():
                if record.output:
                    ledger.attempts[key] = record.model_copy(
                        update={
                            "output": record.output.model_copy(update={"reason": private}),
                        }
                    )
            replay = await client.get(base + "/events", headers={"Last-Event-ID": items[1]["id"]})
            assert frames(replay.text) == items[2:]
            empty = await client.get(base + "/events", headers={"Last-Event-ID": items[-1]["id"]})
            assert empty.text == ""
            summary = await client.get(base + "/blackboard")
            assert summary.status_code == 200, summary.text
            snapshot = summary.json()
            assert snapshot["goal"]["satisfied"]
            assert snapshot["counts"]["fact"] == 1
            objects = await client.get(
                base + "/blackboard/objects", params={"cursor": snapshot["objects_cursor"]}
            )
            assert objects.status_code == 200, objects.text
            root_id = snapshot["root_intent"]["id"]
            detail = await client.get(base + "/blackboard/objects/" + root_id)
            history = await client.get(base + "/blackboard/objects/" + root_id + "/revisions")
            assert detail.status_code == history.status_code == 200
            assert history.json()["total"] >= 2
            fact_id = next(item["id"] for item in objects.json()["items"] if item["kind"] == "fact")
            fact_detail = await client.get(base + "/blackboard/objects/" + fact_id)
            assert fact_detail.status_code == 200
            assert fact_detail.json()["activation"]["reason"]
            assert private not in fact_detail.text
            assert set(fact_detail.json()["activation"]) == {
                "activation_id",
                "source_id",
                "source_version",
                "reason",
                "intent_ref",
                "input_refs",
            }
            delta = await client.get(
                base + "/blackboard/events", params={"after": snapshot["event_cursor"]}
            )
            assert delta.status_code == 200 and delta.json()["items"] == []
            for payload in (summary.text, objects.text, detail.text, history.text, replay.text):
                assert '"messages"' not in payload and '"checkpoint"' not in payload
                assert private not in payload
            attachment = next(
                item["attachments"][0] for item in objects.json()["items"] if item["attachments"]
            )
            content = await client.get(base + "/blackboard/content/" + attachment["content_digest"])
            assert content.status_code == 200 and private not in content.text
            index = (await client.get("/v1/runs")).json()["items"][0]
            assert index["execution"]["engine"] == index["engine"] == "blackboard"
            for route in ("/blackboard", "/blackboard/objects", "/blackboard/events"):
                assert (await client.get("/v1/runs/missing" + route)).status_code == 404
            legacy = await client.post(
                "/v1/runs", json={"goal": "Write a brief", "engine": "legacy"}
            )
            legacy_id = legacy.json()["run_id"]
            assert (await client.get(f"/v1/runs/{legacy_id}/blackboard")).status_code == 409
            for token, status in (
                ("bad", 422),
                (event_cursor("other", 1), 422),
                (event_cursor(run_id, 999), 409),
            ):
                bad = await client.get(base + "/events", headers={"Last-Event-ID": token})
                assert bad.status_code == status, bad.text
            conflict = await client.get(
                base + "/events",
                params={"after": items[0]["id"]},
                headers={"Last-Event-ID": items[1]["id"]},
            )
            assert conflict.status_code == 422
            for params in (
                {"topic": " "},
                {"kind": "messages"},
                {"limit": 201},
                {"at_revision": 0},
            ):
                assert (
                    await client.get(base + "/blackboard/objects", params=params)
                ).status_code == 422
            assert (await client.get("/v1/runs/missing/events")).status_code == 404
            runtime.config.api.auth_enabled = True
            monkeypatch.setenv(runtime.config.api.api_key_env, "test-blackboard-key")
            for route in ("/blackboard", "/blackboard/objects", "/blackboard/events", "/events"):
                assert (await client.get(base + route)).status_code == 401
            assert (
                await client.get(base + "/blackboard", headers={"X-API-Key": "test-blackboard-key"})
            ).status_code == 200


async def test_sse_and_object_cursor_survive_api_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "work_harness.api.app.build_runtime",
        partial(
            build_runtime,
            checkpoint_backend="sqlite",
            provider_store_backend="memory",
        ),
    )
    for name, filename in (
        ("WORK_HARNESS_CHECKPOINT_PATH", "checkpoints.sqlite"),
        ("WORK_HARNESS_RUN_STORE_PATH", "runs.sqlite"),
        ("WORK_HARNESS_BLACKBOARD_PATH", "blackboard.sqlite"),
    ):
        monkeypatch.setenv(name, str(tmp_path / filename))
    app = create_app(ROOT)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            accepted = await client.post("/v1/runs", json=draft_request().model_dump(mode="json"))
            base = "/v1/runs/" + accepted.json()["run_id"]
            response = await asyncio.wait_for(client.get(base + "/events"), timeout=10)
            committed = frames(response.text)
            first = (await client.get(base + "/blackboard/objects", params={"limit": 1})).json()
    # A new runtime and new SQLite connections must interpret both cursors identically.
    app = create_app(ROOT)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            replay = await client.get(
                base + "/events", headers={"Last-Event-ID": committed[1]["id"]}
            )
            assert frames(replay.text) == committed[2:]
            rest = await client.get(
                base + "/blackboard/objects", params={"cursor": first["next_cursor"]}
            )
            assert rest.status_code == 200, rest.text
            assert rest.json()["revision"] == first["revision"]
            assert len(rest.json()["items"]) + 1 == first["total"]


class ConnectedRequest:
    async def is_disconnected(self):
        return False


async def test_stream_drains_all_pages_even_when_finalized_index_lags():
    async with build_runtime(ROOT, checkpoint_backend="memory") as runtime:
        manager = RunManager(runtime)
        try:
            await runtime.run_store.reserve(
                RunRecord(
                    run_id="board",
                    execution=RunExecutionVersion(
                        engine="blackboard",
                        workflow_revision="blackboard-v6",
                        schema_version=1,
                        configuration_fingerprint="0" * 64,
                    ),
                )
            )
            store = await runtime.blackboard_store()
            kernel, initial = await create_board(store)
            for number in range(101):
                board = await store.snapshot("board")
                await kernel.commit("board", hint_delta(board, str(number)), context=SOURCE)
            final = await kernel.cancel("board")
            assert (await runtime.run_store.get("board")).status == "pending"
            response = await stream_blackboard(manager, "board", ConnectedRequest())
            items = frames("".join([item async for item in response.body_iterator]))
            assert len(items) == final.finalized_at_revision == 103
            assert [json.loads(item["data"])["sequence"] for item in items] == list(range(1, 104))
            assert json.loads(items[-1]["data"])["event_type"] == "finalized"
            summary = await BlackboardQuery(store, "board").summary()
            assert summary.evaluated_at == final.finalized_at
            historical = await BlackboardQuery(store, "board").summary(at_revision=1)
            assert historical.evaluated_at == initial.objects[0].created_at
        finally:
            await manager.close()


async def test_stream_polls_intermediate_commits_and_reports_later_gap():
    async with build_runtime(ROOT, checkpoint_backend="memory") as runtime:
        manager = RunManager(runtime)
        try:
            await runtime.run_store.reserve(
                RunRecord(
                    run_id="board",
                    execution=RunExecutionVersion(
                        engine="blackboard",
                        workflow_revision="blackboard-v6",
                        schema_version=1,
                        configuration_fingerprint="0" * 64,
                    ),
                )
            )
            store = await runtime.blackboard_store()
            # Open the stream before initialization; this is a legitimate admission race.
            response = await stream_blackboard(manager, "board", ConnectedRequest())
            stream = response.body_iterator
            first = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)
            kernel, initial = await create_board(store)
            assert json.loads(frames(await asyncio.wait_for(first, 2))[0]["data"])["sequence"] == 1
            receipt = await kernel.commit("board", hint_delta(initial), context=SOURCE)
            # No manager._publish call: durable polling still observes the accepted commit.
            second = await asyncio.wait_for(anext(stream), 2)
            assert json.loads(frames(second)[0]["data"])["sequence"] == 2
            await consume(kernel, store, receipt.local_refs["hint"])
            await kernel.commit(
                "board", hint_delta(await store.snapshot("board"), "later"), context=SOURCE
            )
            await remove_event(store, 3)
            gap = await asyncio.wait_for(anext(stream), 2)
            assert frames(gap)[0]["event"] == "snapshot_required" and "id" not in frames(gap)[0]
            with pytest.raises(StopAsyncIteration):
                await anext(stream)
            from fastapi import HTTPException

            with pytest.raises(HTTPException) as missing:
                await stream_blackboard(
                    manager, "board", ConnectedRequest(), after=event_cursor("board", 2)
                )
            assert missing.value.status_code == 410
        finally:
            await manager.close()
