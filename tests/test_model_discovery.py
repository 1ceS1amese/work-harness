import json
from pathlib import Path

import httpx
import pytest

from work_harness.api.app import create_app
from work_harness.config.models import ProviderConfig
from work_harness.llm.base import InvalidModelRequestError, TransientModelError
from work_harness.llm.discovery import ModelDiscoveryResult, UpstreamModel, discover_models

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def config(**overrides):
    values = dict(
        adapter="openai_compatible",
        credential_source="stored",
        enabled=False,
        base_url="https://gateway.example",
    )
    values.update(overrides)
    result = ProviderConfig(**values)
    result.set_runtime_api_key("catalog-secret")
    return result


@pytest.mark.parametrize(
    ("family", "path", "header"),
    [
        ("responses", "https://gateway.example", "authorization"),
        ("chat_completions", "https://gateway.example/v1/chat/completions", "authorization"),
        ("messages", "https://gateway.example/v1/messages", "x-api-key"),
    ],
)
async def test_catalog_protocol_headers_root_paths_and_pagination(family, path, header):
    seen = []

    def handler(req):
        seen.append(req)
        assert req.url.path == "/v1/models"
        assert "catalog-secret" in req.headers[header]
        assert req.method == "GET"
        if family == "messages":
            assert req.headers["anthropic-version"] == "2023-06-01"
        if len(seen) == 1:
            return httpx.Response(
                200,
                json={
                    "data": [{"id": "a", "display_name": "Alpha"}],
                    "has_more": True,
                    "last_id": "a",
                },
            )
        assert req.url.params["after_id"] == "a"
        return httpx.Response(200, json={"data": [{"id": "a"}, {"id": "b"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await discover_models(
            config(
                api_family=family,
                base_url=path,
                adapter="anthropic" if family == "messages" else "openai_compatible",
            ),
            client=client,
        )
    assert [item.id for item in result.items] == ["a", "b"]
    assert not result.truncated


async def test_http_private_auto_mode_and_explicit_restrictions():
    with pytest.raises(ValueError, match="valid port"):
        await discover_models(config(base_url="https://gateway.example:not-a-port"))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(404),
        )
    ) as client:
        with pytest.raises(InvalidModelRequestError, match="手动添加模型"):
            await discover_models(config(), client=client)
    connection = config(base_url="http://127.0.0.1:8001/proxy/v1?api-version=1")
    connection.allow_insecure_http = False
    with pytest.raises(ValueError, match="scheme"):
        await discover_models(connection)
    connection.allow_insecure_http = None
    connection.allow_private_network = False
    with pytest.raises(ValueError, match="private"):
        await discover_models(connection)
    connection.allow_private_network = None

    def handler(req):
        assert req.url.path == "/proxy/v1/models"
        assert req.url.params["api-version"] == "1"
        return httpx.Response(200, json={"data": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert (await discover_models(connection, client=client)).items == []


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>login</html>"),
        httpx.Response(200, json={"data": [{"id": 42}]}),
        httpx.Response(200, json={"data": [], "has_more": True}),
        httpx.Response(200, json={"data": [{"id": "a"}], "has_more": True, "last_id": "a"}),
        httpx.Response(200, content=b" " * 2_097_153),
        httpx.Response(302, headers={"location": "http://127.0.0.1/private"}),
    ],
)
async def test_catalog_rejects_malformed_oversized_and_redirect_responses(response):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
        with pytest.raises(InvalidModelRequestError):
            await discover_models(config(), client=client)


async def test_catalog_transport_failure():
    def handler(_):
        raise httpx.ReadTimeout("upstream-secret")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(TransientModelError, match="获取模型超时") as error:
            await discover_models(config(), client=client)
        assert "upstream-secret" not in str(error.value)


@pytest.mark.usefixtures("legacy_engine")
async def test_discovery_api_reuses_encrypted_key_without_saving_or_echoing_it(
    tmp_path, monkeypatch
):
    for name, filename in [
        ("CHECKPOINT", "checkpoints"),
        ("RUN_STORE", "runs"),
        ("PROVIDER_STORE", "providers"),
    ]:
        monkeypatch.setenv(f"WORK_HARNESS_{name}_PATH", str(tmp_path / f"{filename}.sqlite"))
    seen = []

    async def discover(connection):
        seen.append(connection.runtime_api_key())
        return ModelDiscoveryResult(items=[UpstreamModel(id="claude-test", name="Claude")])

    monkeypatch.setattr("work_harness.api.app.discover_models", discover)
    app = create_app(PROJECT_ROOT)
    payload = {
        "config": config(
            adapter="anthropic", api_family="messages", allowed_models=["claude-test"]
        ).model_dump(mode="json"),
        "api_key": "stored-test-key",
    }
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client,
    ):
        assert (await client.put("/v1/providers/claude-test", json=payload)).status_code == 200
        before = (await client.get("/v1/providers")).json()
        discovery = {
            "config": {**payload["config"], "allowed_models": []},
            "provider_name": "claude-test",
        }
        for update, expected in [
            ({}, "stored-test-key"),
            ({"api_key": "new-unsaved-key"}, "new-unsaved-key"),
            ({"clear_api_key": True}, ""),
        ]:
            result = await client.post(
                "/v1/providers/models/discover", json={**discovery, **update}
            )
            assert result.status_code == 200
            assert seen[-1] == expected
            assert "key" not in result.text
        monkeypatch.setenv("CATALOG_KEY", "env-test-key")
        discovery["config"].update(credential_source="environment", api_key_env="CATALOG_KEY")
        assert (
            await client.post("/v1/providers/models/discover", json=discovery)
        ).status_code == 200
        assert seen[-1] == "env-test-key"
        assert (await client.get("/v1/providers")).json() == before
        # Invalid config must not echo a SecretStr's raw input or the containing body.
        bad = {**discovery, "api_key": "do-not-echo", "clear_api_key": True}
        response = await client.post("/v1/providers/models/discover", json=bad)
        assert response.status_code == 422
        assert "do-not-echo" not in response.text
        assert "input" not in json.dumps(response.json()["detail"])
