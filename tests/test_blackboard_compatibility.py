"""Engine migration preserves historical contracts and the original resume path."""

from __future__ import annotations

import asyncio
import sqlite3

import httpx
import pytest
from test_blackboard_postgres_runtime import distributed_database as distributed_database
from test_blackboard_postgres_runtime import distributed_managers as distributed_managers
from test_blackboard_postgres_runtime import wait_result
from test_blackboard_runtime import ROOT, draft_request
from test_blackboard_runtime import sqlite_paths as sqlite_paths

from work_harness.api.app import create_app
from work_harness.blackboard.protocol import BoardNotFoundError
from work_harness.domain.models import RunResult, TaskRequest
from work_harness.runtime import build_runtime
from work_harness.service import RunManager
from work_harness.storage.blackboard_sqlite import SQLiteBlackboardStore

pytestmark = pytest.mark.usefixtures("legacy_engine")

@pytest.mark.parametrize("versioned", (False, True))
async def test_legacy_history_and_human_resume_after_default_switch(
    sqlite_paths, tmp_path, monkeypatch, versioned
):
    monkeypatch.setenv("WORK_HARNESS_PROVIDER_STORE_PATH", str(tmp_path / "providers.sqlite"))
    request = TaskRequest(
        goal="Review a proposed production change",
        risk_level="high",
        idempotency_key="legacy-before-switch",
    )
    async with build_runtime(ROOT, checkpoint_backend="sqlite") as runtime:
        manager = RunManager(runtime)
        try:
            run_id = await manager.start(request)
            waiting = await manager.wait(run_id)
            assert waiting.status == "waiting_human" and waiting.artifacts and waiting.reviews
            execution = (await runtime.run_store.get(run_id)).execution
            # Pre-engine response JSON must still decode with its original meaning.
            historical = waiting.model_dump(mode="json", exclude={"engine", "pending_intents"})
            decoded = RunResult.model_validate(historical)
            assert decoded.engine == "legacy" and decoded.artifacts == waiting.artifacts
            assert decoded.reviews == waiting.reviews
        finally:
            await manager.close()
    if not versioned:
        with sqlite3.connect(tmp_path / "runs.sqlite") as connection:
            connection.execute("UPDATE run_index SET execution = NULL WHERE run_id = ?", (run_id,))
            connection.execute("DELETE FROM run_inputs WHERE run_id = ?", (run_id,))

    monkeypatch.setenv("WORK_HARNESS_WORKFLOW_ENGINE", "blackboard")
    app = create_app(ROOT)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        prior = (await client.get(f"/v1/runs/{run_id}")).json()
        assert prior["engine"] == "legacy" and prior["status"] == "waiting_human"
        assert prior["artifacts"] == historical["artifacts"]
        assert prior["reviews"] == historical["reviews"]
        assert (await client.get(f"/v1/runs/{run_id}/blackboard")).status_code == 409
        duplicate = await client.post("/v1/runs", json=request.model_dump(mode="json"))
        assert duplicate.status_code == 202 and duplicate.json()["run_id"] == run_id
        resumed = await client.post(f"/v1/runs/{run_id}/resume", json={"approved": True})
        assert resumed.status_code == 202, resumed.text

        async def completed(identifier):
            for _ in range(300):
                result = (await client.get(f"/v1/runs/{identifier}")).json()
                if result["status"] not in {"pending", "running"}:
                    return result
                await asyncio.sleep(0.01)
            raise AssertionError("run did not finish")

        final = await completed(run_id)
        assert final["engine"] == "legacy" and final["status"] == "succeeded"
        assert final["artifacts"] == historical["artifacts"]
        assert final["reviews"] == historical["reviews"]
        new = await client.post("/v1/runs", json=draft_request(engine=None).model_dump(mode="json"))
        assert new.status_code == 202, new.text
        board_id = new.json()["run_id"]
        assert (await completed(board_id))["engine"] == "blackboard"
        listed = (await client.get("/v1/runs")).json()
        assert {item["run_id"] for item in listed["items"]} == {run_id, board_id}
    # Default selection never creates a board or verification proof for old artifacts.
    store = SQLiteBlackboardStore(tmp_path / "blackboard.sqlite")
    await store.initialize()
    try:
        with pytest.raises(BoardNotFoundError):
            await store.snapshot(run_id)
        assert (await store.snapshot(board_id)).finalization.status == "succeeded"
    finally:
        await store.close()
    with sqlite3.connect(tmp_path / "runs.sqlite") as connection:
        stored = connection.execute(
            "SELECT execution FROM run_index WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
    assert stored is None if not versioned else execution.model_validate_json(stored) == execution


@pytest.mark.parametrize("versioned", (False, True))
async def test_postgres_worker_keeps_legacy_resume_after_default_switch(
    distributed_managers, versioned
):
    api, worker = distributed_managers
    await worker.initialize()
    request = TaskRequest(
        goal="Review a proposed deployment", risk_level="high", idempotency_key="original-legacy"
    )
    run_id = await api.start(request)
    waiting = await wait_result(api, run_id)
    assert waiting.engine == "legacy" and waiting.status == "waiting_human"
    for _ in range(200):
        if (await api.runtime.run_store.get(run_id)).status == "waiting_human":
            break
        await asyncio.sleep(0.01)
    if not versioned:
        async with api.runtime.run_store._pool.connection() as connection:
            await connection.execute(
                "UPDATE work_harness_run_index SET execution = NULL WHERE run_id = %s", (run_id,)
            )
    for manager in (api, worker):
        manager.runtime.config.runtime.workflow.engine = "blackboard"
    assert await api.start(request) == run_id
    await api.resume(run_id, approved=True)
    completed = await wait_result(api, run_id)
    assert completed.engine == "legacy" and completed.status == "succeeded"
    assert completed.artifacts == waiting.artifacts and completed.reviews == waiting.reviews
    board_id = await api.start(draft_request(engine=None))
    assert (await wait_result(api, board_id)).engine == "blackboard"
    store = await api.runtime.blackboard_store()
    with pytest.raises(BoardNotFoundError):
        await store.snapshot(run_id)
    assert (
        (await api.runtime.run_store.get(run_id)).execution is None
        if not versioned
        else (await api.runtime.run_store.get(run_id)).engine == "legacy"
    )


async def test_incompatible_legacy_checkpoint_is_readable_but_not_relabelled():
    async with build_runtime(ROOT, checkpoint_backend="memory") as runtime:
        manager = RunManager(runtime)
        try:
            run_id = await manager.start(
                TaskRequest(goal="Review a proposed migration", risk_level="high")
            )
            waiting = await manager.wait(run_id)
            await runtime.graph.aupdate_state(
                manager._graph_config(run_id), {"workflow_revision": "older-workflow"}
            )
            runtime.config.runtime.workflow.engine = "blackboard"
            assert (await manager.get(run_id)).artifacts == waiting.artifacts
            with pytest.raises(ValueError, match="workflow revision is incompatible"):
                await manager.resume(run_id, approved=True)
            current = await manager.get(run_id)
            assert current.engine == "legacy" and current.status == "waiting_human"
            assert current.reviews == waiting.reviews
            assert await runtime.existing_blackboard_store() is None
        finally:
            await manager.close()
