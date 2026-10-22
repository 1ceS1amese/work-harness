from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from work_harness.config.models import ModelCandidateConfig
from work_harness.domain.models import ModelMessage, ModelRequest, ModelResponse, Usage
from work_harness.evaluation_comparison import (
    ComparisonGateway,
    ComparisonPricing,
    ComparisonSpec,
    reassess_comparison,
    run_comparison,
    score_output,
)
from work_harness.knowledge_sources.catalog import DELIVERY_PREFIX
from work_harness.llm.base import ModelGatewayError
from work_harness.llm.mock import MockProvider
from work_harness.runtime import build_runtime

ROOT = Path(__file__).resolve().parents[1]


def spec() -> ComparisonSpec:
    return ComparisonSpec(
        scope="contract tests only",
        budget={"token_budget": 50_000},
        provider="mock",
        model="test-model",
        cases=[
            {
                "id": "one",
                "goal": "Return a JSON object with count equal to 3.",
                "expected": {"count": 3},
            }
        ],
    )


def pin(runtime):
    for logical in runtime.config.models.logical_models.values():
        logical.candidates = [ModelCandidateConfig(provider="mock", model="test-model")]


@pytest.mark.parametrize(
    "text",
    [
        '{"count": 3} unrelated false claim',
        'Wrong answer. Here is the expected text: {"count": 3}',
        '{"count": 9, "count": 3}',
        '{"count": 3, "invented": true}',
        '{"count": true}',
    ],
)
def test_quality_rejects_substrings_duplicate_fields_and_type_confusion(text):
    assert not all(score_output(text, {"count": 3}).values())


def test_quality_accepts_delivery_envelope_but_still_checks_answer():
    assert all(score_output(DELIVERY_PREFIX + '```json\n{"count": 3}\n```', {"count": 3}).values())
    assert not all(score_output(DELIVERY_PREFIX + '{"count": 4}', {"count": 3}).values())


@pytest.mark.parametrize("exclude_cost", (False, True))
async def test_both_real_graphs_are_measured_without_mock_quality_claim(
    tmp_path, monkeypatch, exclude_cost
):
    output = tmp_path / "comparison.json"
    original_dispatch = MockProvider._dispatch

    def dispatch(self, operation, payload, run_id):
        # The acceptance criteria and budget must be frozen before the first generation.
        frozen = json.loads(output.read_text())
        assert frozen["manifest"]["cases"][0]["expected"] == {"count": 3}
        assert frozen["frozen_at"]
        result = original_dispatch(self, operation, payload, run_id)
        if operation == "worker":
            result["content"] = '{"count": 3}'
        if operation == "synthesize":
            result["output"] = '{"count": 3}'
        return result

    monkeypatch.setattr(MockProvider, "_dispatch", dispatch)
    monkeypatch.setenv("MODEL_API_KEY", "secret-must-not-be-persisted")
    async with build_runtime(ROOT, checkpoint_backend="memory") as runtime:
        pin(runtime)
        policy = runtime.config.runtime.model_dump()
        graph, gateway = runtime.graph, runtime.model_gateway
        configuration = spec()
        if exclude_cost:
            configuration.thresholds.max_cost_ratio = None
            configuration.budget.cost_budget_usd = None
        report = await run_comparison(runtime, configuration, output, contract_only=True)
        assert runtime.graph is graph and runtime.model_gateway is gateway
        assert runtime.config.runtime.model_dump() == policy
        assert report["status"] == "completed"
        assert report["evaluation_scope"] == "comparison_contract_test"
        assert not report["quality_claim"] and not report["eligible_for_default_switch"]
        assert report["gates"]["quality"] is True
        assert report["gates"]["cost"] is None
        assert not report["comparison_gates_passed"]
        if exclude_cost:
            assert report["excluded_gates"] == ["cost", "monetary_budget"]
            assert report["gates"]["budgets"] is True
            assert "cost" not in report["required_gates"]
            assert report["gates"]["usage_known"] is True
        assert all(row["passed"] for row in report["cases"])
        assert all(row["model_calls"] for row in report["cases"])
        assert report["results"]["blackboard"]["activation_count"] == 2
        assert report["results"]["legacy"]["ineffective_activations"] is None
        for result in report["results"].values():
            assert result["input_tokens"] > 0
            assert result["estimated_cost_usd"] is None
        saved = output.read_text()
        assert "secret-must-not-be-persisted" not in saved
        with pytest.raises(FileExistsError):
            await run_comparison(runtime, spec(), output, contract_only=True)
        assert output.read_text() == saved
        assert runtime.config.runtime.model_dump() == policy


async def test_real_comparison_rejects_mock_and_fallback_routes(tmp_path):
    async with build_runtime(ROOT, checkpoint_backend="memory") as runtime:
        with pytest.raises(ValueError, match="never Mock"):
            await run_comparison(runtime, spec(), tmp_path / "real.json")
        with pytest.raises(ValueError, match="same single provider/model"):
            await run_comparison(runtime, spec(), tmp_path / "mixed.json", contract_only=True)
    assert not list(tmp_path.iterdir())


async def test_parse_failure_does_not_erase_returned_model_usage(tmp_path, monkeypatch):
    original = MockProvider._dispatch

    def invalid_plan(self, operation, payload, run_id):
        if operation == "plan":
            return {"not_a_plan": True}
        return original(self, operation, payload, run_id)

    monkeypatch.setattr(MockProvider, "_dispatch", invalid_plan)
    async with build_runtime(ROOT, checkpoint_backend="memory") as runtime:
        pin(runtime)
        report = await run_comparison(
            runtime, spec(), tmp_path / "invalid.json", contract_only=True
        )
    legacy = next(row for row in report["cases"] if row["engine"] == "legacy")
    assert legacy["status"] == "failed" and not legacy["passed"]
    assert len(legacy["model_calls"]) == 1
    assert legacy["model_calls"][0]["status"] == "ok"
    assert report["results"]["legacy"]["input_tokens"] == 64
    assert report["results"]["legacy"]["output_tokens"] == 128
    assert report["results"]["legacy"]["estimated_cost_usd"] is None


async def test_missing_usage_is_not_reported_as_free_generation():
    class Gateway:
        async def complete(self, request):
            return ModelResponse(provider="mock", model="test-model", content="OK")

    meter = ComparisonGateway(Gateway(), spec())
    request = ModelRequest(
        run_id="one",
        trace_id="first",
        node_name="worker",
        agent_role="worker-01",
        logical_model="worker-fast",
        messages=[ModelMessage(role="user", content="hello")],
    )
    await meter.complete(request)
    assert meter.calls["one"][0]["status"] == "usage_unknown"
    assert meter.calls["one"][0]["estimated_cost_usd"] is None
    with pytest.raises(ModelGatewayError, match="usage unresolved"):
        await meter.complete(request)


async def test_cancelled_comparison_preserves_calls_before_case_completion(tmp_path, monkeypatch):
    output = tmp_path / "cancelled.json"
    entered = asyncio.Event()
    original = MockProvider.complete

    async def wait_in_worker(self, request, *, model):
        if request.node_name == "plan":
            response = await original(self, request, model=model)
            response.structured_output["subtasks"] = response.structured_output["subtasks"][:1]
            response.content = json.dumps(response.structured_output)
            return response
        if request.node_name == "worker":
            saved = json.loads(output.read_text())
            legacy = next(row for row in saved["cases"] if row["engine"] == "legacy")
            assert legacy["status"] == "running"
            assert legacy["model_calls"][0]["status"] == "ok"
            assert legacy["model_calls"][0]["input_tokens"] == 64
            assert legacy["model_calls"][-1]["status"] == "pending"
            entered.set()
            await asyncio.Event().wait()
        return await original(self, request, model=model)

    monkeypatch.setattr(MockProvider, "complete", wait_in_worker)
    async with build_runtime(ROOT, checkpoint_backend="memory") as runtime:
        pin(runtime)
        task = asyncio.create_task(run_comparison(runtime, spec(), output, contract_only=True))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        # No running call or graph may outlive the evaluation context.
        assert runtime.config.runtime.model_retry_attempts == 2
    saved = json.loads(output.read_text())
    legacy = next(row for row in saved["cases"] if row["engine"] == "legacy")
    assert saved["status"] == "interrupted" and legacy["status"] == "interrupted"
    assert legacy["model_calls"][-1]["status"] == "usage_unknown"
    summary = saved["results"]["legacy"]
    assert summary["input_tokens"] is None
    assert summary["observed_input_tokens"] == 64
    assert summary["unresolved_calls"] == 1
    assert not saved["quality_claim"] and not saved["comparison_gates_passed"]


async def test_failed_preflight_keeps_quality_and_cost_unknown(tmp_path, monkeypatch):
    async with build_runtime(ROOT, checkpoint_backend="memory") as runtime:
        pin(runtime)
        runtime.config.models.providers["mock"].adapter = "openai_compatible"

        async def unavailable(request):
            raise ModelGatewayError("secret-must-not-be-persisted")

        monkeypatch.setattr(runtime.model_gateway, "complete", unavailable)
        output = tmp_path / "unavailable.json"
        report = await run_comparison(runtime, spec(), output)
        assert report["status"] == "preflight_failed"
        assert all(row["status"] == "not_run" for row in report["cases"])
        assert not report["quality_claim"]
        assert report["preflight_calls"][0]["status"] == "usage_unknown"
        for result in report["results"].values():
            assert result["quality_pass_rate"] is None
            assert result["input_tokens"] is None
            assert result["estimated_cost_usd"] is None
        assert "secret-must-not-be-persisted" not in output.read_text()


async def test_concurrent_calls_cannot_bypass_shared_budget():
    class Gateway:
        count = 0

        async def complete(self, request):
            self.count += 1
            assert request.max_output_tokens == 128
            return ModelResponse(
                provider="mock", model="test-model", usage=Usage(input_tokens=20, output_tokens=10)
            )

    configuration = spec()
    configuration.budget.max_model_calls = 1
    configuration.budget.max_output_tokens_per_call = 128
    gateway = Gateway()
    meter = ComparisonGateway(gateway, configuration)
    request = ModelRequest(
        run_id="one",
        trace_id="first",
        node_name="worker",
        agent_role="worker-01",
        logical_model="worker-fast",
        messages=[ModelMessage(role="user", content="hello")],
    )
    results = await asyncio.gather(
        meter.complete(request), meter.complete(request), return_exceptions=True
    )
    assert gateway.count == 1
    assert sum(isinstance(result, ModelGatewayError) for result in results) == 1
    assert len(meter.calls["one"]) == 1
    assert meter.calls["one"][0]["estimated_cost_usd"] is None


def test_pricing_counts_cached_input_separately():
    pricing = ComparisonPricing(
        source="test fixture, not a provider quote",
        input_usd_per_million=2,
        cached_input_usd_per_million=1,
        output_usd_per_million=8,
    )
    assert (
        pricing.estimate(Usage(input_tokens=1000, cached_tokens=400, output_tokens=100)) == 0.0024
    )


@pytest.fixture
def frozen_comparison():
    """Synthetic observations only; this fixture is never a project quality report."""
    configuration = spec()
    configuration.provider = "compatible"
    manifest = configuration.model_dump(mode="json")
    report = {
        "manifest": manifest,
        "manifest_sha256": hashlib.sha256(
            json.dumps(manifest, sort_keys=True).encode()
        ).hexdigest(),
        "status": "completed",
        "evaluation_scope": "real_provider_engine_comparison",
        "gates": {"cost": None},
        "comparison_gates_passed": False,
        "cases": [
            {
                "case_id": "one",
                "engine": engine,
                "status": "succeeded",
                "output": '{"count": 3}',
                "errors": [],
                "latency_ms": 100,
                "passed": False,
                "budget_passed": False,
                "model_calls": [
                    {
                        "status": "ok",
                        "provider": "compatible",
                        "model": "test-model",
                        "input_tokens": 100,
                        "output_tokens": 10,
                        "latency_ms": 50,
                        "estimated_cost_usd": None,
                    }
                ],
                "activations": [{"status": "completed"}] if engine == "blackboard" else [],
            }
            for engine in ("legacy", "blackboard")
        ],
    }
    configuration.thresholds.max_cost_ratio = None
    configuration.budget.cost_budget_usd = None
    return report, configuration


def test_cost_exclusion_reuses_observations_without_rewriting_source(tmp_path, frozen_comparison):
    original, policy = frozen_comparison
    source, output = tmp_path / "source.json", tmp_path / "reassessed.json"
    raw = json.dumps(original).encode()
    source.write_bytes(raw)
    report = reassess_comparison(source, policy, output)
    assert report["comparison_gates_passed"]
    assert report["gates"]["cost"] is None
    assert report["excluded_gates"] == ["cost", "monetary_budget"]
    assert not report["eligible_for_default_switch"]  # Deployment checks are separate.
    assert all(r["estimated_cost_usd"] is None for r in report["results"].values())
    assert report["source_report"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert source.read_bytes() == raw
    with pytest.raises(FileExistsError):
        reassess_comparison(source, policy, output)
    with pytest.raises(FileExistsError):
        reassess_comparison(source, policy, source)
    assert source.read_bytes() == raw


def test_token_exclusion_keeps_known_usage_above_old_allowance(tmp_path, frozen_comparison):
    original, policy = frozen_comparison
    original["cases"][1]["model_calls"][0]["input_tokens"] = 100_000
    policy.budget.token_budget = None
    source = tmp_path / "source.json"
    raw = json.dumps(original)
    source.write_text(raw)
    report = reassess_comparison(source, policy, tmp_path / "result.json")
    assert report["comparison_gates_passed"]
    assert "token_budget" in report["excluded_gates"]
    assert report["results"]["blackboard"]["input_tokens"] == 100_000
    assert source.read_text() == raw


async def test_comparison_observes_usage_without_reducing_next_output_limit():
    configuration = spec()
    configuration.budget.token_budget = configuration.budget.cost_budget_usd = None
    observed = []

    class Gateway:
        async def complete(self, request):
            observed.append(request.max_output_tokens)
            return ModelResponse(
                provider="mock",
                model="test-model",
                usage=Usage(input_tokens=100_000, output_tokens=10),
            )

    gateway = ComparisonGateway(Gateway(), configuration)
    for number in range(2):
        await gateway.complete(
            ModelRequest(
                run_id="run",
                trace_id=str(number),
                node_name="worker",
                agent_role="worker-01",
                logical_model="test",
                messages=[ModelMessage(role="user", content="work")],
            )
        )
    assert observed == [4000, 4000]
    assert sum(c["input_tokens"] for c in gateway.calls["run"]) == 200_000


@pytest.mark.parametrize(
    "failure",
    [
        "quality",
        "usage_known",
        "route",
        "latency",
        "effective_activations",
        "tokens",
        "output_tokens",
        "call_timeout",
        "mock",
    ],
)
def test_cost_exclusion_keeps_other_gates(tmp_path, frozen_comparison, failure):
    original, policy = frozen_comparison
    row = original["cases"][1]
    if failure == "quality":
        row["output"] = '{"count": 4}'
        row["passed"] = True  # Re-score raw output instead of trusting the old verdict.
    elif failure == "usage_known":
        row["model_calls"] = []
    elif failure == "route":
        row["model_calls"][0]["model"] = "other-model"
    elif failure == "latency":
        row["latency_ms"] = 200
    elif failure == "effective_activations":
        row["activations"][0]["status"] = "failed"
    elif failure == "tokens":
        row["model_calls"][0]["input_tokens"] = 50_001
    elif failure == "output_tokens":
        row["model_calls"][0]["output_tokens"] = 4097
    elif failure == "call_timeout":
        row["model_calls"][0]["latency_ms"] = 60_001
    elif failure == "mock":
        original["evaluation_scope"] = "comparison_contract_test"
    source = tmp_path / "source.json"
    source.write_text(json.dumps(original))
    result = reassess_comparison(source, policy, tmp_path / "result.json")
    assert not result["comparison_gates_passed"]
    assert not result["eligible_for_default_switch"]
    if failure == "mock":
        assert not result["quality_claim"]


@pytest.mark.parametrize(
    "change", ["answer", "threshold", "route", "duplicate", "digest", "incomplete"]
)
def test_reassessment_rejects_changed_experiment(tmp_path, frozen_comparison, change):
    original, policy = frozen_comparison
    if change == "answer":
        policy.cases[0].expected = {"count": 4}
    elif change == "threshold":
        policy.thresholds.min_quality_pass_rate = 0.1
    elif change == "route":
        policy.model = "other-model"
    elif change == "duplicate":
        original["cases"][1] = original["cases"][0]
    elif change == "digest":
        original["manifest_sha256"] = "incorrect"
    elif change == "incomplete":
        original["status"] = "interrupted"
    source, output = tmp_path / "source.json", tmp_path / "result.json"
    source.write_text(json.dumps(original))
    with pytest.raises(ValueError):
        reassess_comparison(source, policy, output)
    assert not output.exists()


async def test_reassessment_cli_never_initializes_runtime(tmp_path, frozen_comparison, monkeypatch):
    from work_harness import cli

    original, policy = frozen_comparison
    source, manifest, output = (
        tmp_path / name for name in ("source.json", "manifest.json", "out.json")
    )
    source.write_text(json.dumps(original))
    manifest.write_text(policy.model_dump_json())

    def unexpected_runtime(*args, **kwargs):
        raise AssertionError("offline reassessment must not open a provider or store")

    monkeypatch.setattr(cli, "build_runtime", unexpected_runtime)
    args = cli.build_parser().parse_args(
        [
            "eval",
            "--reassess-comparison",
            str(source),
            "--cases",
            str(manifest),
            "--output",
            str(output),
        ]
    )
    assert await cli._run_eval(args) == 0
    assert output.exists()
