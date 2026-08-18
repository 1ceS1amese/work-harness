import asyncio
from pathlib import Path

import httpx
import pytest

from work_harness.api.app import create_app

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_api_creates_and_queries_run(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(
        "WORK_HARNESS_CHECKPOINT_PATH", str(tmp_path / "api-checkpoint.sqlite")
    )
    monkeypatch.setenv("WORK_HARNESS_RUN_STORE_PATH", str(tmp_path / "api-runs.sqlite"))
    app = create_app(PROJECT_ROOT)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            capabilities = await client.get("/v1/capabilities")
            assert capabilities.status_code == 200
            assignable_agents = {
                item["id"] for item in capabilities.json()["agents"] if item["assignable"]
            }
            assert assignable_agents == {
                "worker-01",
                "worker-02",
                "worker-03",
                "worker-04",
            }
            assert {item["id"] for item in capabilities.json()["functions"]} == {
                "analysis",
                "research",
                "execution",
                "risk",
            }
            assert capabilities.json()["provider_contract"]["compatible"] is True
            assert {item["provider"] for item in capabilities.json()["providers"]} == {
                "mock",
                "mock-secondary",
            }
            assert {item["id"] for item in capabilities.json()["skills"]} == {
                "repository-analysis",
                "risk-audit",
            }

            compatibility = await client.get("/v1/compatibility")
            assert compatibility.status_code == 200
            assert compatibility.json()["providers"]["compatible"] is True

            invalid_skill = await client.post(
                "/v1/runs",
                json={
                    "goal": "Check risks",
                    "skills": [{"skill_id": "risk-audit", "provider": "compatible"}],
                },
            )
            assert invalid_skill.status_code == 422

            response = await client.post("/v1/runs", json={"goal": "Create a checklist"})
            assert response.status_code == 202
            run_id = response.json()["run_id"]

            result = None
            for _ in range(30):
                result = await client.get(f"/v1/runs/{run_id}")
                assert result.status_code == 200
                if result.json()["status"] == "succeeded":
                    break
                await asyncio.sleep(0.01)

            assert result is not None
            assert result.json()["status"] == "succeeded"
            assert len(result.json()["artifacts"]) == 2

            events = await client.get(f"/v1/runs/{run_id}/events")
            assert events.status_code == 200
            assert events.headers["content-type"].startswith("text/event-stream")
            assert "event: run" in events.text
            assert '"status":"succeeded"' in events.text

            run_index = await client.get("/v1/runs")
            assert run_index.status_code == 200
            assert run_index.json()["items"][0]["run_id"] == run_id
            assert "idempotency_key_hash" not in run_index.json()["items"][0]

            idempotent_payload = {
                "goal": "Create a persistent checklist",
                "idempotency_key": "api-idempotency-test",
            }
            first = await client.post("/v1/runs", json=idempotent_payload)
            duplicate = await client.post("/v1/runs", json=idempotent_payload)
            conflict = await client.post(
                "/v1/runs",
                json={
                    "goal": "A different task",
                    "idempotency_key": "api-idempotency-test",
                },
            )
            assert first.status_code == 202
            assert duplicate.json()["run_id"] == first.json()["run_id"]
            assert conflict.status_code == 409
