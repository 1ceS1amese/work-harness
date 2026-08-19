import asyncio
from pathlib import Path

import httpx
import pytest

from work_harness.api.app import create_app

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_secure_profile_requires_api_key(monkeypatch) -> None:
    monkeypatch.setenv("WORK_HARNESS_PROFILE", "secure")
    monkeypatch.delenv("WORK_HARNESS_API_KEY", raising=False)

    with pytest.raises(ValueError, match="WORK_HARNESS_API_KEY is not configured"):
        create_app(PROJECT_ROOT)


@pytest.mark.asyncio
async def test_api_creates_and_queries_run(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(
        "WORK_HARNESS_CHECKPOINT_PATH", str(tmp_path / "api-checkpoint.sqlite")
    )
    monkeypatch.setenv("WORK_HARNESS_RUN_STORE_PATH", str(tmp_path / "api-runs.sqlite"))
    monkeypatch.setenv(
        "WORK_HARNESS_PROVIDER_STORE_PATH", str(tmp_path / "api-providers.sqlite")
    )
    app = create_app(PROJECT_ROOT)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            liveness = await client.get("/health/live")
            assert liveness.status_code == 200
            assert liveness.json() == {"status": "ok"}

            readiness = await client.get("/health/ready")
            assert readiness.status_code == 200
            assert readiness.json()["status"] == "ok"

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


@pytest.mark.asyncio
async def test_production_starts_in_configuration_mode_without_provider_key(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("WORK_HARNESS_PROFILE", "production")
    monkeypatch.setenv("WORK_HARNESS_API_KEY", "deployment-key-for-readiness-test")
    monkeypatch.delenv("MODEL_BASE_URL", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.setenv(
        "WORK_HARNESS_CHECKPOINT_PATH", str(tmp_path / "production-checkpoint.sqlite")
    )
    monkeypatch.setenv(
        "WORK_HARNESS_RUN_STORE_PATH", str(tmp_path / "production-runs.sqlite")
    )
    monkeypatch.setenv(
        "WORK_HARNESS_PROVIDER_STORE_PATH", str(tmp_path / "production-providers.sqlite")
    )

    app = create_app(PROJECT_ROOT)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"X-API-Key": "deployment-key-for-readiness-test"},
        ) as client:
            readiness = await client.get("/health/ready")
            assert readiness.status_code == 503
            assert readiness.json()["status"] == "degraded"
            assert readiness.json()["providers"] == {"compatible": False}

            providers = await client.get("/v1/providers")
            assert providers.status_code == 200
            compatible = next(
                item for item in providers.json()["items"]
                if item["name"] == "compatible"
            )
            assert compatible["credential_available"] is False


@pytest.mark.asyncio
async def test_api_persists_provider_profile_for_next_startup(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv(
        "WORK_HARNESS_CHECKPOINT_PATH", str(tmp_path / "provider-checkpoint.sqlite")
    )
    monkeypatch.setenv("WORK_HARNESS_RUN_STORE_PATH", str(tmp_path / "provider-runs.sqlite"))
    monkeypatch.setenv(
        "WORK_HARNESS_PROVIDER_STORE_PATH", str(tmp_path / "provider-profiles.sqlite")
    )
    payload = {
        "config": {
            "adapter": "openai_compatible",
            "protocol_variant": "openai",
            "api_family": "responses",
            "enabled": True,
            "base_url": "https://custom-model.test/v1",
            "api_key_env": "CUSTOM_MODEL_API_KEY",
            "allowed_models": ["main-model", "worker-model"],
            "capabilities": ["chat", "structured_output", "tool_calling"],
        },
        "model_bindings": {"worker-fast": "worker-model"},
        "agent_bindings": {"main": "main-model", "worker-02": "worker-model"},
    }
    monkeypatch.setenv("CUSTOM_MODEL_API_KEY", "configured-for-mock-contract")

    first_app = create_app(PROJECT_ROOT)
    first_transport = httpx.ASGITransport(app=first_app)
    async with first_app.router.lifespan_context(first_app):
        async with httpx.AsyncClient(
            transport=first_transport, base_url="http://test"
        ) as client:
            saved = await client.put("/v1/providers/custom-model", json=payload)
            assert saved.status_code == 200
            assert saved.json()["provider"]["source"] == "persistent"
            assert saved.json()["provider"]["loaded"] is False
            assert saved.json()["restart_required"] is True
            assert "Bearer " not in saved.text

    second_app = create_app(PROJECT_ROOT)
    second_transport = httpx.ASGITransport(app=second_app)
    async with second_app.router.lifespan_context(second_app):
        async with httpx.AsyncClient(
            transport=second_transport, base_url="http://test"
        ) as client:
            providers = await client.get("/v1/providers")
            assert providers.status_code == 200
            custom = next(
                item
                for item in providers.json()["items"]
                if item["name"] == "custom-model"
            )
            assert custom["source"] == "persistent"
            assert custom["loaded"] is True
            assert custom["credential_available"] is True
            assert custom["model_bindings"] == {"worker-fast": "worker-model"}
            assert custom["agent_bindings"] == {
                "main": "main-model",
                "worker-02": "worker-model",
            }
            assert providers.json()["restart_required"] is False

            capabilities = (await client.get("/v1/capabilities")).json()
            agents = {item["id"]: item for item in capabilities["agents"]}
            assert agents["worker-01"]["model_route"] is None
            assert agents["worker-02"]["model_route"] == {
                "provider": "custom-model",
                "model": "worker-model",
            }
            assert all(
                "custom-model" in skill["available_providers"]
                for skill in capabilities["skills"]
            )
            compatibility = (await client.get("/v1/compatibility")).json()
            assert compatibility["agent_routes"]["planner"] == {
                "provider": "custom-model",
                "model": "main-model",
            }

            deleted = await client.delete("/v1/providers/custom-model")
            assert deleted.status_code == 204


@pytest.mark.asyncio
async def test_api_stages_disabled_provider_without_credential_value(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv(
        "WORK_HARNESS_CHECKPOINT_PATH", str(tmp_path / "staged-checkpoint.sqlite")
    )
    monkeypatch.setenv("WORK_HARNESS_RUN_STORE_PATH", str(tmp_path / "staged-runs.sqlite"))
    provider_store = tmp_path / "staged-providers.sqlite"
    monkeypatch.setenv("WORK_HARNESS_PROVIDER_STORE_PATH", str(provider_store))
    monkeypatch.delenv("MANUAL_MODEL_API_KEY", raising=False)
    payload = {
        "config": {
            "adapter": "openai_compatible",
            "protocol_variant": "deepseek",
            "api_family": "chat_completions",
            "enabled": False,
            "base_url": "https://models.example.test/v1",
            "api_key_env": "MANUAL_MODEL_API_KEY",
            "allowed_models": ["manual-model"],
            "capabilities": ["chat", "structured_output", "tool_calling"],
        },
        "model_bindings": {"worker-fast": "manual-model"},
        "agent_bindings": {"worker-03": "manual-model"},
    }

    app = create_app(PROJECT_ROOT)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            saved = await client.put("/v1/providers/manual-gateway", json=payload)
            assert saved.status_code == 200
            assert saved.json()["provider"]["credential_available"] is False
            assert saved.json()["restart_required"] is True

    restarted = create_app(PROJECT_ROOT)
    restarted_transport = httpx.ASGITransport(app=restarted)
    async with restarted.router.lifespan_context(restarted):
        async with httpx.AsyncClient(
            transport=restarted_transport, base_url="http://test"
        ) as client:
            providers = (await client.get("/v1/providers")).json()
            staged = next(
                item for item in providers["items"] if item["name"] == "manual-gateway"
            )
            assert staged["loaded"] is True
            assert staged["config"]["protocol_variant"] == "deepseek"
            assert staged["credential_available"] is False

    assert b"MANUAL_MODEL_API_KEY" in provider_store.read_bytes()
    assert b"sk-" not in provider_store.read_bytes()


@pytest.mark.asyncio
async def test_api_persists_encrypted_ui_managed_provider_key(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "WORK_HARNESS_CHECKPOINT_PATH", str(tmp_path / "ui-checkpoint.sqlite")
    )
    monkeypatch.setenv("WORK_HARNESS_RUN_STORE_PATH", str(tmp_path / "ui-runs.sqlite"))
    provider_store = tmp_path / "ui-providers.sqlite"
    monkeypatch.setenv("WORK_HARNESS_PROVIDER_STORE_PATH", str(provider_store))
    api_key = "ui-managed-provider-secret"
    payload = {
        "config": {
            "adapter": "openai_compatible",
            "protocol_variant": "openai",
            "api_family": "chat_completions",
            "enabled": True,
            "base_url": "https://models.example.test/v1",
            "credential_source": "stored",
            "allowed_models": ["ui-model"],
            "capabilities": ["chat", "structured_output", "tool_calling"],
        },
        "model_bindings": {"worker-fast": "ui-model"},
        "api_key": api_key,
    }

    first_app = create_app(PROJECT_ROOT)
    async with first_app.router.lifespan_context(first_app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=first_app),
            base_url="http://test",
        ) as client:
            saved = await client.put("/v1/providers/ui-provider", json=payload)
            assert saved.status_code == 200
            assert saved.json()["provider"]["credential_available"] is True
            assert api_key not in saved.text

    assert api_key.encode() not in provider_store.read_bytes()
    assert (tmp_path / "ui-providers.sqlite.key").is_file()

    second_app = create_app(PROJECT_ROOT)
    async with second_app.router.lifespan_context(second_app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=second_app),
            base_url="http://test",
        ) as client:
            providers = (await client.get("/v1/providers")).json()
            stored = next(
                item for item in providers["items"] if item["name"] == "ui-provider"
            )
            assert stored["loaded"] is True
            assert stored["credential_available"] is True
            assert stored["config"]["credential_source"] == "stored"


@pytest.mark.asyncio
async def test_secure_profile_protects_api_but_exposes_liveness(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("WORK_HARNESS_PROFILE", "secure")
    monkeypatch.setenv("WORK_HARNESS_API_KEY", "deployment-secret")
    monkeypatch.setenv(
        "WORK_HARNESS_CHECKPOINT_PATH", str(tmp_path / "auth-checkpoint.sqlite")
    )
    monkeypatch.setenv("WORK_HARNESS_RUN_STORE_PATH", str(tmp_path / "auth-runs.sqlite"))
    monkeypatch.setenv(
        "WORK_HARNESS_PROVIDER_STORE_PATH", str(tmp_path / "auth-providers.sqlite")
    )

    app = create_app(PROJECT_ROOT)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/health/live")).status_code == 200
            assert (await client.get("/health")).status_code == 401
            authenticated = await client.get(
                "/health", headers={"X-API-Key": "deployment-secret"}
            )
            assert authenticated.status_code == 200


@pytest.mark.asyncio
async def test_api_enforces_body_and_rate_limits(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(
        "WORK_HARNESS_CHECKPOINT_PATH", str(tmp_path / "guard-checkpoint.sqlite")
    )
    monkeypatch.setenv("WORK_HARNESS_RUN_STORE_PATH", str(tmp_path / "guard-runs.sqlite"))
    monkeypatch.setenv(
        "WORK_HARNESS_PROVIDER_STORE_PATH", str(tmp_path / "guard-providers.sqlite")
    )
    app = create_app(PROJECT_ROOT)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            too_large = await client.post(
                "/v1/runs",
                content=b"{}",
                headers={"Content-Length": str(1_048_577)},
            )
            assert too_large.status_code == 413

            responses = [await client.get("/v1/capabilities") for _ in range(61)]
            assert responses[-2].status_code == 200
            assert responses[-1].status_code == 429
            assert responses[-1].headers["retry-after"]
