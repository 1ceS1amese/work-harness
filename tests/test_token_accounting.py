"""Usage is observed independently of execution and per-response output limits."""

from pathlib import Path

import httpx
import pytest
from test_blackboard_execution_storage import LIMITS, setup
from test_openai_compatible import model_request, provider_config

from work_harness.blackboard.protocol import BlackboardError
from work_harness.config.loader import _profiled, load_app_config
from work_harness.domain.models import TaskRequest, Usage
from work_harness.knowledge_sources.base import KnowledgeOutput, UsageRecord
from work_harness.knowledge_sources.catalog import draft_task_input
from work_harness.llm.base import ModelGatewayError, TransientModelError
from work_harness.llm.gateway import ModelGateway
from work_harness.llm.mock import MockProvider
from work_harness.llm.openai_compatible import OpenAICompatibleProvider
from work_harness.runtime import build_runtime
from work_harness.service import RunManager
from work_harness.storage.settings import EditableRuntimeSettings

ROOT = Path(__file__).resolve().parents[1]


async def test_partial_receipt_is_reconciled_once_without_double_counting(blackboard_store):
    execution = await setup(blackboard_store)
    record = await execution.reserve(
        limits=LIMITS.model_copy(update={"token_budget": 1, "cost_budget_usd": 0})
    )
    identity = record.candidate.id
    partial = UsageRecord(call_id="one", input_tokens=100, usage_complete=False)
    await execution.ledger.record_usage(identity, 1, partial)
    await execution.ledger.save_output(identity, 1, KnowledgeOutput())
    summary = await execution.ledger.summary("board")
    assert not summary.usage_complete and summary.unknown_calls == 1
    assert summary.tokens == 100 and summary.calls == 1
    complete = UsageRecord(
        call_id="one",
        input_tokens=100,
        output_tokens=20,
        cached_tokens=80,
        cost_usd=10,
    )
    await execution.ledger.record_usage(identity, 1, complete)
    await execution.ledger.record_usage(identity, 1, complete)
    await execution.ledger.complete_usage(identity, 1)
    summary = await execution.ledger.summary("board")
    assert summary.usage_complete and summary.unknown_calls == 0
    assert summary.calls == 1 and summary.tokens == 120  # Cache is part of input, not extra.
    assert (summary.input_tokens, summary.output_tokens, summary.cached_tokens) == (100, 20, 80)
    assert summary.cost_usd == 10 and summary.reserved_tokens == summary.reserved_cost_usd == 0
    with pytest.raises(BlackboardError, match="different content"):
        await execution.ledger.record_usage(
            identity, 1, complete.model_copy(update={"input_tokens": 101})
        )


@pytest.mark.parametrize("responses", (False, True))
@pytest.mark.parametrize("counts", (None, {"input": 100}, {"input": 100, "output": 5}))
async def test_provider_marks_missing_usage_and_keeps_available_counts(
    monkeypatch, responses, counts
):
    monkeypatch.setenv("TEST_MODEL_BASE_URL", "https://models.example.test/v1")
    monkeypatch.setenv("TEST_MODEL_API_KEY", "test-only")
    usage = {}
    if counts:
        for key, value in counts.items():
            field = (
                f"{key}_tokens"
                if responses
                else {"input": "prompt_tokens", "output": "completion_tokens"}[key]
            )
            usage[field] = value
    data = (
        {
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": '{"ok":true}'}]}
            ]
        }
        if responses
        else {"choices": [{"message": {"content": '{"ok":true}'}}]}
    )
    if counts is not None:
        data["usage"] = usage
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=data))
    ) as client:
        provider = OpenAICompatibleProvider(
            "compatible",
            provider_config(api_family="responses" if responses else "chat_completions"),
            client=client,
        )
        result = await provider.complete(model_request(), model="test-model")
    assert result.usage.input_tokens == (100 if counts else 0)
    assert result.usage.output_tokens == (counts or {}).get("output", 0)
    assert result.usage.usage_complete == (counts is not None and "output" in counts)


@pytest.mark.parametrize("complete", (False, True))
@pytest.mark.parametrize("engine", ("blackboard", "legacy"))
async def test_run_usage_includes_cache_and_completeness_without_allowance(
    monkeypatch, engine, complete
):
    original = MockProvider.complete

    async def respond(self, request, *, model):
        response = await original(self, request, model=model)
        response.usage = Usage(
            input_tokens=100_000,
            output_tokens=10,
            cached_tokens=90_000,
            usage_complete=complete,
            estimated_cost_usd=100,
        )
        return response

    monkeypatch.setattr(MockProvider, "complete", respond)
    async with build_runtime(ROOT, checkpoint_backend="memory") as runtime:
        manager = RunManager(runtime)
        try:
            identity = await manager.start(
                TaskRequest(
                    goal="Write a draft",
                    engine=engine,
                    blackboard=draft_task_input() if engine == "blackboard" else None,
                )
            )
            result = await manager.wait(identity)
        finally:
            await manager.close()
    assert result.status == "succeeded"
    assert result.usage.input_tokens >= 100_000
    assert result.usage.output_tokens >= 10
    assert result.usage.cached_tokens == result.usage.input_tokens * 9 // 10
    assert result.usage.usage_complete == complete


def test_old_settings_drop_allowances_and_request_alias_is_readable():
    settings = EditableRuntimeSettings.from_runtime(load_app_config(ROOT).runtime)
    old = {**settings.model_dump(), "token_budget": 1, "cost_budget_usd": 0}
    assert EditableRuntimeSettings.model_validate(old) == settings
    assert "token_budget" not in settings.model_dump()
    raw = model_request().model_dump()
    raw["token_budget"] = raw.pop("max_output_tokens")
    request = type(model_request()).model_validate(raw)
    assert request.max_output_tokens == 4000
    assert "token_budget" not in request.model_dump()


def test_old_profile_output_limit_overrides_new_base_field(tmp_path):
    (tmp_path / "agents.yaml").write_text("agents:\n  worker-01:\n    max_output_tokens: 1000\n")
    profile = tmp_path / "profiles" / "local"
    profile.mkdir(parents=True)
    (profile / "agents.yaml").write_text("agents:\n  worker-01:\n    token_budget: 2000\n")
    assert _profiled(tmp_path, "agents", "local")["agents"]["worker-01"] == {
        "max_output_tokens": 2000
    }


async def test_successful_retry_preserves_unknown_prior_usage():
    config = load_app_config(ROOT)

    class RetryProvider(MockProvider):
        calls = 0

        async def complete(self, request, *, model):
            self.calls += 1
            if self.calls == 1:
                raise TransientModelError("response lost")
            return await super().complete(request, model=model)

    provider = RetryProvider()
    gateway = ModelGateway(config.models, {"mock": provider}, config.runtime)
    result = await gateway.complete(model_request())
    assert provider.calls == 2
    assert result.usage.input_tokens > 0 and not result.usage.usage_complete


@pytest.mark.parametrize("engine", ("blackboard", "legacy"))
async def test_failed_model_call_is_not_reported_as_complete_zero_usage(monkeypatch, engine):
    async def fail(self, request, *, model):
        raise ModelGatewayError("unavailable")

    monkeypatch.setattr(MockProvider, "complete", fail)
    async with build_runtime(ROOT, checkpoint_backend="memory") as runtime:
        manager = RunManager(runtime)
        try:
            identity = await manager.start(
                TaskRequest(
                    goal="Write a draft",
                    engine=engine,
                    blackboard=draft_task_input() if engine == "blackboard" else None,
                )
            )
            result = await manager.wait(identity)
        finally:
            await manager.close()
    assert result.status == "failed"
    assert result.usage.input_tokens == result.usage.output_tokens == 0
    assert not result.usage.usage_complete
