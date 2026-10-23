import httpx
import pytest

from work_harness.config.endpoints import normalize_model_url
from work_harness.config.models import ProviderConfig
from work_harness.llm.discovery import discover_models
from work_harness.llm.http_provider import HTTPModelProvider


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("gateway.example", "https://gateway.example"),
        ("gateway.example/proxy/v1/", "https://gateway.example/proxy/v1"),
        (
            "gateway.example/v1?callback=https://app.example",
            "https://gateway.example/v1?callback=https://app.example",
        ),
        ("//gateway.example/v1", "https://gateway.example/v1"),
        ("HTTP://gateway.example/v1", "http://gateway.example/v1"),
        ("https://127.0.0.1/v1", "https://127.0.0.1/v1"),
        ("localhost:11434", "http://localhost:11434"),
        ("localhost.:11434", "http://localhost.:11434"),
        ("127.0.0.1:8000/v1", "http://127.0.0.1:8000/v1"),
        ("192.168.1.8:8000", "http://192.168.1.8:8000"),
        ("10.0.0.8/proxy/v1", "http://10.0.0.8/proxy/v1"),
        ("[::1]:11434/v1", "http://[::1]:11434/v1"),
        ("ollama:11434/v1", "http://ollama:11434/v1"),
        ("gateway.local/v1", "http://gateway.local/v1"),
        ("gateway.internal/v1", "http://gateway.internal/v1"),
        ("gateway.example:80/v1", "http://gateway.example:80/v1"),
        ("localhost:443/v1", "https://localhost:443/v1"),
        ("  gateway.example/v1/?version=1/  ", "https://gateway.example/v1?version=1/"),
    ],
)
def test_normalize_addresses_without_probing_or_overriding_explicit_transport(address, expected):
    assert normalize_model_url(address) == expected
    assert normalize_model_url(expected) == expected
    config = ProviderConfig(adapter="openai_compatible", enabled=False, base_url=address)
    assert config.base_url == expected


@pytest.mark.parametrize(
    "address",
    [
        "ftp://gateway.example",
        "file:///etc/passwd",
        "https://",
        "/v1",
        "https://x:99999",
        "https://x:0",
        "https://x:bad",
        "http://[bad]/",
        "gateway.example/a\nb",
        "https://x\\evil",
    ],
)
def test_invalid_transport_or_address_is_rejected(address):
    with pytest.raises(ValueError):
        normalize_model_url(address)


@pytest.mark.parametrize(
    "address",
    ["user:password@gateway.example/v1", "gateway.example/v1?api_key=secret"],
)
def test_missing_scheme_does_not_bypass_credential_validation(address):
    with pytest.raises(ValueError, match="credential"):
        ProviderConfig(adapter="openai_compatible", enabled=False, base_url=address)


@pytest.mark.parametrize(
    ("address", "family", "expected"),
    [
        ("127.0.0.1:8080", "responses", "http://127.0.0.1:8080/v1/models"),
        (
            "http://gateway.example/proxy/v1/responses",
            "responses",
            "http://gateway.example/proxy/v1/models",
        ),
        (
            "gateway.example/v1/chat/completions",
            "chat_completions",
            "https://gateway.example/v1/models",
        ),
        (
            "[::1]:8000/proxy/v1/messages?version=1",
            "messages",
            "http://[::1]:8000/proxy/v1/models?version=1",
        ),
    ],
)
async def test_discovery_uses_automatic_address_policy_for_all_protocols(address, family, expected):
    config = ProviderConfig(
        adapter="anthropic" if family == "messages" else "openai_compatible",
        api_family=family,
        enabled=False,
        base_url=address,
        credential_source="stored",
    )
    config.set_runtime_api_key("test-only-key")
    seen = []

    def handler(request):
        seen.append(str(request.url))
        assert request.headers["x-api-key" if family == "messages" else "authorization"]
        return httpx.Response(200, json={"data": [{"id": "available-model"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await discover_models(config, client=client)
    assert seen == [expected]
    assert result.items[0].id == "available-model"


async def test_environment_override_uses_same_normalization_and_preserves_explicit_restrictions(
    monkeypatch,
):
    monkeypatch.setenv("AUTO_MODEL_URL", "localhost:8000/proxy/v1/messages?version=2")
    config = ProviderConfig(
        adapter="anthropic",
        api_family="messages",
        enabled=False,
        base_url="https://fallback.example",
        base_url_env="AUTO_MODEL_URL",
    )
    async with httpx.AsyncClient() as client:
        provider = HTTPModelProvider("test", config, client=client)
        assert provider._endpoint("messages") == "http://localhost:8000/proxy/v1/messages?version=2"
        config.allow_insecure_http = False
        with pytest.raises(ValueError, match="scheme"):
            HTTPModelProvider("test", config, client=client)
        config.allow_insecure_http = None
        config.allow_private_network = False
        with pytest.raises(ValueError, match="localhost"):
            HTTPModelProvider("test", config, client=client)
