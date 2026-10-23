"""Clients discover admission policy and submit the exact template the user selected."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from test_backup_restore import project as project
from test_document_deliverables import document_worker as document_worker

from work_harness.api.app import create_app


@pytest.mark.parametrize(
    "default_engine,enabled", [("legacy", True), ("blackboard", True), ("blackboard", False)]
)
async def test_capabilities_follow_default_and_rollback_policy(
    project, monkeypatch, default_engine, enabled, document_worker
):
    monkeypatch.setenv("WORK_HARNESS_WORKFLOW_ENGINE", default_engine)
    monkeypatch.setenv("WORK_HARNESS_ACCEPT_NEW_BLACKBOARD_RUNS", str(enabled).lower())
    app = create_app(project)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        workflow = (await client.get("/v1/capabilities")).json()["workflow"]
        assert workflow["default_engine"] == default_engine
        assert workflow["accept_new_blackboard_runs"] is enabled
        template = workflow["blackboard_templates"][0]
        assert template["id"] == "analysis-report" and template["title"] == "分析报告"
        if default_engine == "blackboard":
            missing = await client.post("/v1/runs", json={"goal": "核验外部事实"})
            assert missing.status_code == 422
        accepted = await client.post(
            "/v1/runs",
            json={
                "goal": "起草项目说明",
                "engine": "blackboard",
                "blackboard": template["input"],
            },
        )
        if not enabled:
            assert accepted.status_code == 422 and "disabled" in accepted.text
            accepted = await client.post(
                "/v1/runs", json={"goal": "起草项目说明", "engine": "legacy"}
            )
        assert accepted.status_code == 202, accepted.text
        run_id = accepted.json()["run_id"]
        for _ in range(200):
            result = (await client.get(f"/v1/runs/{run_id}")).json()
            if result["status"] not in {"pending", "running"}:
                break
            await asyncio.sleep(0.01)
        assert result["status"] == "succeeded"
        assert result["engine"] == ("blackboard" if enabled else "legacy")
        if enabled:
            board = (await client.get(f"/v1/runs/{run_id}/blackboard")).json()
            assert board["root_intent"]["acceptance"] == template["input"]["acceptance"]
            assert "分析报告已交付" in result["output"]
