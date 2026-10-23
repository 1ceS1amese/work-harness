import asyncio
import threading
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest

from work_harness.api.app import create_app
from work_harness.config.loader import load_app_config
from work_harness.domain.models import ModelMessage, ModelRequest, ModelResponse
from work_harness.llm.base import ModelGatewayError, TransientModelError
from work_harness.llm.gateway import ModelGateway
from work_harness.llm.http_provider import HTTPModelProvider
from work_harness.llm.mock import MockProvider
from work_harness.storage import RunRecord, SQLiteRunStore
from work_harness.storage.provider_sqlite import SQLiteProviderProfileStore
from work_harness.storage.settings import ApplicationSettings, ApplicationSettingsStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def model_request(**overrides):
    values = dict(
        run_id="r",
        trace_id="t",
        node_name="worker",
        agent_role="worker-01",
        logical_model="worker-fast",
        timeout_seconds=1,
        messages=[ModelMessage(role="user", content="test")],
    )
    values.update(overrides)
    return ModelRequest(**values)


async def test_failed_settings_write_does_not_publish_uncommitted_value(tmp_path, monkeypatch):
    store = ApplicationSettingsStore(tmp_path / "settings.json")
    await store.initialize()
    original = await store.save(ApplicationSettings(prompts={"worker": "original"}))

    def fail(_):
        raise OSError("disk unavailable")

    monkeypatch.setattr(store, "_write_sync", fail)
    with pytest.raises(OSError):
        await store.save(ApplicationSettings(prompts={"worker": "uncommitted"}))
    assert await store.get() == original
    reopened = ApplicationSettingsStore(store.path)
    await reopened.initialize()
    assert await reopened.get() == original


@pytest.mark.parametrize("failure", ["fsync", "replace"])
async def test_settings_file_failure_preserves_prior_file_and_removes_temp(
    tmp_path,
    monkeypatch,
    failure,
):
    store = ApplicationSettingsStore(tmp_path / "settings.json")
    await store.initialize()
    original = await store.save(ApplicationSettings(prompts={"worker": "original"}))

    def fail(*_):
        raise OSError("injected write failure")

    monkeypatch.setattr(f"work_harness.storage.settings.os.{failure}", fail)
    with pytest.raises(OSError):
        await store.save(ApplicationSettings(prompts={"worker": "uncommitted"}))
    reopened = ApplicationSettingsStore(store.path)
    await reopened.initialize()
    assert await store.get() == await reopened.get() == original
    assert not await asyncio.to_thread(lambda: list(tmp_path.glob(".settings.json.*")))


async def test_cancelled_settings_writer_finishes_before_next_update(tmp_path, monkeypatch):
    store = ApplicationSettingsStore(tmp_path / "settings.json")
    await store.initialize()
    original = await store.get()
    started, release = threading.Event(), threading.Event()
    write = store._write_sync

    def blocked(value):
        if value.prompts == {"planner": "first"}:
            started.set()
            assert release.wait(3)
        write(value)

    monkeypatch.setattr(store, "_write_sync", blocked)
    first = asyncio.create_task(store.save(ApplicationSettings(prompts={"planner": "first"})))
    second = None
    try:
        assert await asyncio.to_thread(started.wait, 2)
        assert await store.get() == original
        first.cancel()
        await asyncio.sleep(0.01)
        first.cancel()  # Repeated cancellation must not drop the write lock.
        second = asyncio.create_task(
            store.update(
                lambda current: ApplicationSettings(
                    prompts={**current.prompts, "worker": "second"},
                )
            )
        )
        await asyncio.sleep(0.01)
        assert not first.done()
        assert not second.done()
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
    assert first.cancelled()
    assert (await store.get()).prompts == {"planner": "first", "worker": "second"}
    reopened = ApplicationSettingsStore(store.path)
    await reopened.initialize()
    assert await reopened.get() == await store.get()


async def test_concurrent_provider_bindings_cannot_persist_invalid_combination(
    tmp_path, monkeypatch
):
    for name in ("CHECKPOINT", "RUN_STORE", "PROVIDER_STORE", "BLACKBOARD"):
        monkeypatch.setenv(f"WORK_HARNESS_{name}_PATH", str(tmp_path / f"{name}.sqlite"))
    read = SQLiteProviderProfileStore.list

    async def yielding_read(store):
        snapshot = await read(store)
        await asyncio.sleep(0.02)
        return snapshot

    monkeypatch.setattr(SQLiteProviderProfileStore, "list", yielding_read)
    app = create_app(PROJECT_ROOT)
    body = {
        "config": {
            "adapter": "openai_compatible",
            "base_url": "https://test.example/v1",
            "credential_source": "stored",
            "allowed_models": ["test"],
            "capabilities": ["chat", "structured_output", "tool_calling"],
        },
        "agent_bindings": {"worker-01": "test"},
        "api_key": "test-only-key",
    }
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client,
    ):
        responses = await asyncio.gather(
            *(client.put(f"/v1/providers/channel-{index}", json=body) for index in range(2))
        )
        assert sorted(response.status_code for response in responses) == [200, 409]
        state = await client.get("/v1/providers")
        assert state.status_code == 200
        assert (
            len([item for item in state.json()["items"] if item["name"].startswith("channel-")])
            == 1
        )


async def test_model_deadline_covers_all_attempts_and_releases_provider_slot():
    class SlowProvider(MockProvider):
        calls = 0
        active = 0
        slow = True

        async def complete(self, request, *, model):
            self.calls += 1
            self.active += 1
            try:
                if self.slow:
                    await asyncio.sleep(10)
                return ModelResponse(provider=self.name, model=model, content="ready")
            finally:
                self.active -= 1

    config = load_app_config(PROJECT_ROOT)
    config.models.providers["mock"].max_concurrency = 1
    provider = SlowProvider()
    gateway = ModelGateway(config.models, {"mock": provider}, config.runtime)
    request = ModelRequest(
        run_id="r",
        trace_id="t",
        node_name="worker",
        agent_role="worker-01",
        logical_model="worker-fast",
        timeout_seconds=0.03,
        messages=[ModelMessage(role="user", content="test")],
    )
    with pytest.raises(ModelGatewayError):
        await gateway.complete(request)
    assert provider.calls == 1
    assert provider.active == 0
    provider.slow = False
    assert (await gateway.complete(request)).content == "ready"


async def test_cancelled_sqlite_write_keeps_lock_until_commit(tmp_path, monkeypatch):
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    await store.initialize()
    entered, release = threading.Event(), threading.Event()
    reserve = store._reserve_sync

    def blocked(*args):
        entered.set()
        assert release.wait(3)
        return reserve(*args)

    monkeypatch.setattr(store, "_reserve_sync", blocked)
    first = asyncio.create_task(store.reserve(RunRecord(run_id="pending-write")))
    second = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        first.cancel()
        second = asyncio.create_task(store.update_status("pending-write", "succeeded"))
        await asyncio.sleep(0.01)
        assert not second.done()
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
        await store.close()
    assert first.cancelled()
    assert (await store.get("pending-write")).status == "succeeded"


async def test_settings_read_parsing_runs_off_the_event_loop(tmp_path, monkeypatch):
    for name in ("CHECKPOINT", "RUN_STORE", "PROVIDER_STORE", "BLACKBOARD"):
        monkeypatch.setenv(f"WORK_HARNESS_{name}_PATH", str(tmp_path / f"{name}.sqlite"))
    threads = []
    reading = False

    def tracked_loader(*args):
        if reading:
            threads.append(threading.get_ident())
        return load_app_config(*args)

    monkeypatch.setattr("work_harness.config.loader.load_app_config", tracked_loader)
    app = create_app(PROJECT_ROOT)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client,
    ):
        reading = True
        assert (await client.get("/v1/providers")).status_code == 200
        assert (await client.get("/v1/settings")).status_code == 200
        assert len(threads) == 2
        assert threading.get_ident() not in threads


async def test_queued_cancellation_does_not_leak_slots_or_cancel_active_call():
    entered, release = asyncio.Event(), asyncio.Event()

    class Waiting(MockProvider):
        calls = 0

        async def complete(self, request, *, model):
            self.calls += 1
            entered.set()
            await release.wait()
            return ModelResponse(provider=self.name, model=model)

    config = load_app_config(PROJECT_ROOT)
    config.models.providers["mock"].max_concurrency = 1
    provider = Waiting()
    gateway = ModelGateway(config.models, {"mock": provider}, config.runtime)
    active = asyncio.create_task(gateway.complete(model_request()))
    await entered.wait()
    try:
        with pytest.raises(ModelGatewayError, match="deadline exceeded"):
            await gateway.complete(model_request(timeout_seconds=0.02))
        queued = asyncio.create_task(gateway.complete(model_request()))
        await asyncio.sleep(0)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        assert not active.done()
        assert provider.calls == 1
    finally:
        release.set()
        await active
    await gateway.complete(model_request())
    assert provider.calls == 2


async def test_long_retry_after_uses_fallback_without_violating_deadline():
    class Limited(MockProvider):
        calls = 0

        async def complete(self, request, *, model):
            self.calls += 1
            raise TransientModelError("limited", retry_after_seconds=60)

    class Ready(MockProvider):
        async def complete(self, request, *, model):
            return ModelResponse(provider=self.name, model=model)

    config = load_app_config(PROJECT_ROOT)
    primary = Limited()
    gateway = ModelGateway(
        config.models,
        {
            "mock": primary,
            "mock-secondary": Ready("mock-secondary"),
        },
        config.runtime,
    )
    response = await gateway.complete(model_request(timeout_seconds=0.5))
    assert primary.calls == 1
    assert response.provider == "mock-secondary"
    assert response.fallback_from.startswith("mock/")
    assert not response.usage.usage_complete


async def test_retry_after_is_observed_before_retrying_same_provider(monkeypatch):
    class Limited(MockProvider):
        calls = 0

        async def complete(self, request, *, model):
            self.calls += 1
            if self.calls == 1:
                raise TransientModelError("limited", retry_after_seconds=0.25)
            return ModelResponse(provider=self.name, model=model)

    waits = []

    async def sleep(delay):
        waits.append(delay)

    monkeypatch.setattr("work_harness.llm.gateway.asyncio.sleep", sleep)
    config = load_app_config(PROJECT_ROOT)
    provider = Limited()
    result = await ModelGateway(config.models, {"mock": provider}, config.runtime).complete(
        model_request(),
    )
    assert waits == [0.25]
    assert result.retries == 1


async def test_hung_healthcheck_cannot_hang_readiness():
    class Hanging(MockProvider):
        async def healthcheck(self):
            await asyncio.Event().wait()

    config = load_app_config(PROJECT_ROOT)
    config.models.providers["mock"].timeout_seconds = 0.01
    gateway = ModelGateway(
        config.models,
        {
            "mock": Hanging(),
            "mock-secondary": MockProvider("mock-secondary"),
        },
        config.runtime,
    )
    assert await asyncio.wait_for(gateway.health(), 0.5) == {"mock": False, "mock-secondary": True}


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("2", 2),
        ("0.5", 0.5),
        ("-1", 0),
        ("nan", None),
        ("inf", None),
        ("not-a-date", None),
        (None, None),
    ],
)
def test_retry_after_header_parsing(header, expected):
    assert HTTPModelProvider._retry_after(header) == expected


def test_retry_after_http_date():
    assert TransientModelError().retry_after_seconds is None
    future = format_datetime(datetime.now(UTC) + timedelta(seconds=60))
    assert 58 <= HTTPModelProvider._retry_after(future) <= 60
    with pytest.raises(TransientModelError) as error:
        HTTPModelProvider._raise_for_status(httpx.Response(429, headers={"Retry-After": "2"}))
    assert error.value.retry_after_seconds == 2
