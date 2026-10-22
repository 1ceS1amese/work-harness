"""Exercise shipped defaults without the legacy compatibility test override."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from test_backup_restore import project as project
from test_blackboard_inputs import confirmation, human_request
from test_blackboard_postgres_runtime import distributed_database as distributed_database
from test_blackboard_postgres_runtime import distributed_managers as distributed_managers
from test_blackboard_postgres_runtime import wait_result

from work_harness import cli
from work_harness.api.app import create_app
from work_harness.config.loader import load_app_config
from work_harness.config.models import WorkflowConfig
from work_harness.domain.models import TaskRequest
from work_harness.knowledge_sources.catalog import draft_task_input

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("profile", ["local", "setup", "production", "distributed"])
def test_shipped_profiles_default_to_blackboard(monkeypatch, profile):
    monkeypatch.delenv("WORK_HARNESS_WORKFLOW_ENGINE", raising=False)
    monkeypatch.setenv("WORK_HARNESS_PROFILE", profile)
    config = load_app_config(ROOT)
    assert config.runtime.workflow.engine == WorkflowConfig().engine == "blackboard"
    assert config.runtime.workflow.accept_new_blackboard_runs


def test_cli_uses_shipped_default_and_keeps_explicit_legacy(project, capsys):
    arguments = ["--project-root", str(project), "run", "--goal", "起草项目说明"]
    with pytest.raises(ValueError, match="require --blackboard-spec"):
        cli.main(arguments)
    assert not (project / "var").exists()
    assert cli.main([*arguments, "--acceptance-template", "saved-draft"]) == 0
    board = json.loads(capsys.readouterr().out)
    assert board["engine"] == "blackboard" and board["status"] == "succeeded"
    assert cli.main([*arguments, "--engine", "legacy"]) == 0
    legacy = json.loads(capsys.readouterr().out)
    assert legacy["engine"] == "legacy" and legacy["status"] == "succeeded"


async def test_sqlite_api_default_keeps_acceptance_and_explicit_legacy(project):
    app = create_app(project)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        workflow = (await client.get("/v1/capabilities")).json()["workflow"]
        assert workflow["default_engine"] == "blackboard"
        missing = await client.post("/v1/runs", json={"goal": "生成草稿"})
        assert missing.status_code == 422
        template = workflow["blackboard_templates"][0]["input"]
        body = {"goal": "生成草稿", "blackboard": template, "idempotency_key": "default-board"}
        board = await client.post("/v1/runs", json=body)
        legacy = await client.post("/v1/runs", json={"goal": "生成草稿", "engine": "legacy"})
        assert board.status_code == legacy.status_code == 202
        for accepted, engine in ((board, "blackboard"), (legacy, "legacy")):
            identifier = accepted.json()["run_id"]
            for _ in range(300):
                result = (await client.get(f"/v1/runs/{identifier}")).json()
                if result["status"] not in {"pending", "running"}:
                    break
                await asyncio.sleep(0.01)
            assert result["status"] == "succeeded" and result["engine"] == engine
        identifier = board.json()["run_id"]
        snapshot = (await client.get(f"/v1/runs/{identifier}/blackboard")).json()
        assert snapshot["root_intent"]["acceptance"] == template["acceptance"]
        assert (await client.post("/v1/runs", json=body)).json()["run_id"] == identifier


async def test_postgres_default_and_rollback_preserve_waiting_blackboard(distributed_managers):
    api, worker = distributed_managers
    await worker.initialize()
    assert api.runtime.config.runtime.workflow.engine == "blackboard"
    with pytest.raises(ValueError, match="acceptance"):
        await api.start(TaskRequest(goal="需要明确验收"))
    board_id = await api.start(TaskRequest(goal="生成草稿", blackboard=draft_task_input()))
    assert (await wait_result(api, board_id)).engine == "blackboard"
    legacy_id = await api.start(TaskRequest(goal="生成草稿", engine="legacy"))
    legacy = await wait_result(api, legacy_id)
    assert legacy.engine == "legacy" and legacy.status == "succeeded"
    request = human_request().model_copy(update={"engine": None, "idempotency_key": "waiting"})
    waiting_id = await api.start(request)
    waiting = await wait_result(api, waiting_id)
    assert waiting.status == "waiting_human"
    for _ in range(200):
        if (await api.runtime.run_store.get(waiting_id)).status == "waiting_human":
            break
        await asyncio.sleep(0.025)
    execution = (await api.runtime.run_store.get(waiting_id)).execution
    for manager in (api, worker):
        manager.runtime.config.runtime.workflow.engine = "legacy"
        manager.runtime.config.runtime.workflow.accept_new_blackboard_runs = False
    assert await api.start(request) == waiting_id
    with pytest.raises(ValueError, match="disabled"):
        await api.start(
            TaskRequest(goal="新的草稿", engine="blackboard", blackboard=draft_task_input())
        )
    fallback_id = await api.start(TaskRequest(goal="回退后生成草稿"))
    assert (await wait_result(api, fallback_id)).engine == "legacy"
    store = await api.runtime.blackboard_store()
    await api.submit_blackboard_input(waiting_id, await confirmation(store, waiting_id))
    resumed = await wait_result(api, waiting_id)
    assert resumed.status == "succeeded" and resumed.engine == "blackboard"
    assert resumed.usage == waiting.usage
    assert (await api.runtime.run_store.get(waiting_id)).execution == execution
