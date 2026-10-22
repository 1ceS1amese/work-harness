"""Paired, bounded engine evaluation; freeze inputs before any model request."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from work_harness.domain.models import ModelMessage, ModelRequest, TaskRequest
from work_harness.evaluation import save_report
from work_harness.knowledge_sources.catalog import DELIVERY_PREFIX
from work_harness.llm.base import ModelGatewayError
from work_harness.observability.trace import TraceRecorder
from work_harness.runtime import HarnessRuntime
from work_harness.service import RunManager
from work_harness.storage.blackboard_factory import build_activation_store
from work_harness.workflow.graph import build_root_graph


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ComparisonCase(Contract):
    id: str = Field(min_length=1)
    goal: str = Field(min_length=1, max_length=19_000)
    expected: dict[str, Any]
    task_type: str = "general"


class ComparisonBudget(Contract):
    token_budget: int | None = Field(default=None, ge=1)
    max_model_calls: int = Field(default=16, ge=1, le=100)
    max_output_tokens_per_call: int = Field(default=4096, ge=1)
    run_timeout_seconds: float = Field(default=300, gt=0, le=1800)
    call_timeout_seconds: float = Field(default=60, gt=0, le=180)
    cost_budget_usd: float | None = Field(default=5, gt=0)


class ComparisonPricing(Contract):
    """User-supplied channel rates; these are estimates, never invoices."""

    source: str = Field(min_length=1)
    input_usd_per_million: float = Field(ge=0)
    cached_input_usd_per_million: float = Field(ge=0)
    output_usd_per_million: float = Field(ge=0)

    def estimate(self, usage) -> float:
        cached = min(usage.cached_tokens, usage.input_tokens)
        return (
            (usage.input_tokens - cached) * self.input_usd_per_million
            + cached * self.cached_input_usd_per_million
            + usage.output_tokens * self.output_usd_per_million
        ) / 1_000_000


class ComparisonThresholds(Contract):
    min_quality_pass_rate: float = Field(default=0.8, ge=0, le=1)
    max_cost_ratio: float | None = Field(default=1.25, gt=0)
    max_latency_ratio: float = Field(default=1.5, gt=0)
    max_ineffective_activation_rate: float = Field(default=0.25, ge=0, le=1)


class ComparisonSpec(Contract):
    version: Literal["blackboard-comparison-001"] = "blackboard-comparison-001"
    scope: str = Field(min_length=1)
    provider: str
    model: str
    budget: ComparisonBudget = Field(default_factory=ComparisonBudget)
    pricing: ComparisonPricing | None = None
    thresholds: ComparisonThresholds = Field(default_factory=ComparisonThresholds)
    cases: list[ComparisonCase] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_cases(self):
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("comparison case IDs must be unique")
        return self


def score_output(output: str, expected: dict[str, Any]) -> dict[str, bool]:
    """Judge the entire deliverable, not a matching substring or the engine's verdict."""
    text = output.removeprefix(DELIVERY_PREFIX).strip()
    if text.startswith("```json\n") and text.endswith("\n```"):
        text = text[len("```json\n") : -len("\n```")]

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result

    try:
        value = json.loads(text, object_pairs_hook=unique_object)
        # Canonical JSON distinguishes booleans from numbers (unlike Python equality).
        equal = json.dumps(value, sort_keys=True, allow_nan=False) == json.dumps(
            expected, sort_keys=True, allow_nan=False
        )
    except (ValueError, TypeError):
        return {"json_document": False, "expected_answer": False}
    return {"json_document": isinstance(value, dict), "expected_answer": equal}


class ComparisonGateway:
    """Measure both graphs at the same boundary, including failures lost during parsing."""

    def __init__(self, gateway, spec: ComparisonSpec):
        self.gateway = gateway
        self.spec = spec
        self.calls: dict[str, list[dict[str, Any]]] = {}
        self._lock = asyncio.Lock()
        self.on_change: Callable[[], None] | None = None

    async def complete(self, request: ModelRequest):
        # Serial model concurrency is fixed for both engines, including reviewer branches.
        async with self._lock:
            calls = self.calls.setdefault(request.run_id, [])
            used = sum(c.get("input_tokens", 0) + c.get("output_tokens", 0) for c in calls)
            cost = sum(c.get("estimated_cost_usd") or 0 for c in calls)
            budget = self.spec.budget
            if (
                len(calls) >= budget.max_model_calls
                or (budget.token_budget is not None and used >= budget.token_budget)
                or (
                    self.spec.pricing is not None
                    and budget.cost_budget_usd is not None
                    and cost >= budget.cost_budget_usd
                )
                or any(c["status"] != "ok" for c in calls)
            ):
                raise ModelGatewayError("comparison budget exhausted or usage unresolved")
            request = request.model_copy(
                update={
                    "max_output_tokens": min(
                        request.max_output_tokens,
                        budget.max_output_tokens_per_call,
                        budget.token_budget - used
                        if budget.token_budget is not None
                        else budget.max_output_tokens_per_call,
                    ),
                    "timeout_seconds": min(request.timeout_seconds, budget.call_timeout_seconds),
                }
            )
            row = {"node": request.node_name, "trace_id": request.trace_id, "status": "pending"}
            calls.append(row)
            if self.on_change is not None:
                self.on_change()
            started = time.perf_counter()
            try:
                response = await self.gateway.complete(request)
            except BaseException as exc:
                # Never persist remote error text or pretend a failed request consumed zero.
                row.update(status="usage_unknown", error_type=type(exc).__name__)
                raise
            else:
                row.update(
                    status="ok",
                    provider=response.provider,
                    model=response.model,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    cached_tokens=response.usage.cached_tokens,
                    provider_request_id=response.provider_request_id,
                    content=response.content,
                    structured_output=response.structured_output,
                    tool_calls=response.tool_calls,
                    estimated_cost_usd=(
                        self.spec.pricing.estimate(response.usage) if self.spec.pricing else None
                    ),
                )
                if (
                    not response.usage.usage_complete
                    or response.usage.input_tokens == response.usage.output_tokens == 0
                ):
                    row["status"] = "usage_unknown"
                    row["estimated_cost_usd"] = None
                if (response.provider, response.model) != (self.spec.provider, self.spec.model):
                    row["status"] = "unexpected_route"
                    raise ModelGatewayError("comparison response used an unexpected model route")
                if self.spec.pricing is not None and row["status"] == "ok":
                    response = response.model_copy(
                        update={
                            "usage": response.usage.model_copy(
                                update={"estimated_cost_usd": row["estimated_cost_usd"]}
                            )
                        }
                    )
                return response
            finally:
                row["latency_ms"] = round((time.perf_counter() - started) * 1000)
                if self.on_change is not None:
                    self.on_change()


def _validate_routes(runtime: HarnessRuntime, spec: ComparisonSpec, contract_only: bool) -> None:
    if runtime.run_store.backend != "memory" or runtime.config.runtime.execution.role != "embedded":
        raise ValueError("comparison requires an isolated memory runtime in embedded mode")
    provider = runtime.config.models.providers.get(spec.provider)
    if provider is None or not provider.enabled:
        raise ValueError("comparison provider must be configured and enabled")
    if not contract_only and provider.adapter != "openai_compatible":
        raise ValueError("real comparison requires a live openai_compatible provider, never Mock")
    expected = [(spec.provider, spec.model)]
    for logical in runtime.config.models.logical_models.values():
        if [(c.provider, c.model) for c in logical.candidates] != expected:
            raise ValueError("all logical models must use the same single provider/model")
    for route in runtime.config.models.agent_routes.values():
        if (route.provider, route.model) != expected[0]:
            raise ValueError("agent route would change the comparison provider/model")


def _write_progress(report: dict, path: Path) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    ) as stream:
        temporary = Path(stream.name)
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _known_call(call: dict, spec: ComparisonSpec) -> bool:
    return (
        call.get("status") == "ok"
        and (call.get("provider"), call.get("model")) == (spec.provider, spec.model)
        and all(
            type(call.get(key)) is int and call[key] >= 0
            for key in ("input_tokens", "output_tokens")
        )
        and call["input_tokens"] + call["output_tokens"] > 0
    )


def _budget_passed(row: dict, spec: ComparisonSpec) -> bool:
    calls = row.get("model_calls", [])
    budget = spec.budget
    return (
        bool(calls)
        and all(_known_call(c, spec) for c in calls)
        and (
            budget.token_budget is None
            or sum(c["input_tokens"] + c["output_tokens"] for c in calls) <= budget.token_budget
        )
        and len(calls) <= budget.max_model_calls
        and 0 <= row.get("latency_ms", -1) <= budget.run_timeout_seconds * 1000
        and all(
            c["output_tokens"] <= budget.max_output_tokens_per_call
            and 0 <= c.get("latency_ms", -1) <= budget.call_timeout_seconds * 1000
            for c in calls
        )
        and (
            budget.cost_budget_usd is None
            or (
                spec.pricing is not None
                and all(c.get("estimated_cost_usd") is not None for c in calls)
                and sum(c["estimated_cost_usd"] for c in calls) <= budget.cost_budget_usd
            )
        )
    )


def summarize_comparison(report: dict, spec: ComparisonSpec, *, contract_only: bool) -> None:
    results = {}
    for engine in ("legacy", "blackboard"):
        rows = [row for row in report["cases"] if row["engine"] == engine]
        calls = [call for row in rows for call in row.get("model_calls", [])]
        complete = (
            len(rows) == len(spec.cases)
            and {r["case_id"] for r in rows} == {c.id for c in spec.cases}
        ) and all(r["status"] not in {"not_run", "running", "interrupted"} for r in rows)
        usage_known = (
            complete
            and all(r.get("model_calls") for r in rows)
            and all(_known_call(c, spec) for c in calls)
        )
        activation_rows = [r.get("activations", []) for r in rows]
        activations = [a for records in activation_rows for a in records]
        ineffective = sum(
            a["status"] in {"no_progress", "failed", "rejected", "retryable"} for a in activations
        )
        results[engine] = {
            "status": "completed" if complete else "incomplete",
            "quality_pass_rate": (
                sum(r.get("passed", False) for r in rows) / len(spec.cases) if complete else None
            ),
            "input_tokens": sum(c.get("input_tokens", 0) for c in calls) if usage_known else None,
            "output_tokens": sum(c.get("output_tokens", 0) for c in calls) if usage_known else None,
            "estimated_cost_usd": (
                sum(c["estimated_cost_usd"] for c in calls)
                if usage_known and spec.pricing
                else None
            ),
            "latency_ms": sum(r.get("latency_ms", 0) for r in rows) if complete else None,
            "ineffective_activations": ineffective if engine == "blackboard" and complete else None,
            "activation_count": len(activations) if engine == "blackboard" and complete else None,
            "usage_known": usage_known,
            "observed_input_tokens": sum(c.get("input_tokens", 0) for c in calls),
            "observed_output_tokens": sum(c.get("output_tokens", 0) for c in calls),
            "unresolved_calls": sum(c["status"] != "ok" for c in calls),
        }
    old, new = results["legacy"], results["blackboard"]
    thresholds = spec.thresholds
    complete = report["status"] == "completed" and all(
        r["status"] == "completed" for r in results.values()
    )
    gates = {
        "complete_real_comparison": complete and not contract_only,
        "quality": complete
        and new["quality_pass_rate"]
        >= max(thresholds.min_quality_pass_rate, old["quality_pass_rate"]),
        "usage_known": complete and all(r["usage_known"] for r in results.values()),
        "cost": (
            None
            if thresholds.max_cost_ratio is None
            or any(r["estimated_cost_usd"] is None for r in results.values())
            else new["estimated_cost_usd"] <= old["estimated_cost_usd"] * thresholds.max_cost_ratio
        ),
        "latency": complete
        and new["latency_ms"] <= old["latency_ms"] * thresholds.max_latency_ratio,
        "effective_activations": complete
        and bool(new["activation_count"])
        and (
            new["ineffective_activations"] / new["activation_count"]
            <= thresholds.max_ineffective_activation_rate
        ),
        "budgets": complete and all(r.get("budget_passed", False) for r in report["cases"]),
    }
    required_gates = [
        name for name in gates if name != "cost" or thresholds.max_cost_ratio is not None
    ]
    report.update(
        results=results,
        gates=gates,
        required_gates=required_gates,
        excluded_gates=(
            (["cost"] if thresholds.max_cost_ratio is None else [])
            + (["monetary_budget"] if spec.budget.cost_budget_usd is None else [])
            + (["token_budget"] if spec.budget.token_budget is None else [])
        ),
        comparison_gates_passed=all(gates[name] is True for name in required_gates),
        quality_claim=complete and not contract_only,
        # Backup/restore and deployment-mode gates are separate steps 6.3/6.4.
        eligible_for_default_switch=False,
    )


def reassess_comparison(source: Path, spec: ComparisonSpec, output: Path) -> dict[str, Any]:
    """Exclude cost/token allowances from frozen observations, without new model calls."""
    raw = source.read_bytes()
    original = json.loads(raw)
    frozen = ComparisonSpec.model_validate(original["manifest"])
    manifest_hash = hashlib.sha256(
        json.dumps(original["manifest"], sort_keys=True).encode()
    ).hexdigest()
    if manifest_hash != original.get("manifest_sha256"):
        raise ValueError("source comparison manifest digest does not match")
    if original["status"] != "completed" or original["evaluation_scope"] not in {
        "comparison_contract_test",
        "real_provider_engine_comparison",
    }:
        raise ValueError("reassessment requires a completed comparison")
    if spec.thresholds.max_cost_ratio is not None or spec.budget.cost_budget_usd is not None:
        raise ValueError("reassessment only supports excluding cost and monetary budget")
    prior_policy = frozen.model_dump(mode="json")
    prior_policy["thresholds"]["max_cost_ratio"] = None
    prior_policy["budget"]["cost_budget_usd"] = None
    if spec.budget.token_budget is None:
        prior_policy["budget"]["token_budget"] = None
    if prior_policy != spec.model_dump(mode="json"):
        raise ValueError("reassessment cannot change cases, routes or execution/quality limits")
    expected = {(case.id, engine) for case in spec.cases for engine in ("legacy", "blackboard")}
    rows = original["cases"]
    if len(rows) != len(expected) or {(r["case_id"], r["engine"]) for r in rows} != expected:
        raise ValueError("source comparison must contain exactly one pair for every case")
    answers = {case.id: case.expected for case in spec.cases}
    for row in rows:
        row["checks"] = {
            **score_output(row.get("output", ""), answers[row["case_id"]]),
            "status": row["status"] == "succeeded",
            "no_errors": not row.get("errors"),
        }
        row["passed"] = all(row["checks"].values())
        row["budget_passed"] = _budget_passed(row, spec)
    report = {
        "version": spec.version,
        "assessment_kind": "policy_reassessment",
        "assessed_at": datetime.now(UTC).isoformat(),
        "evaluation_scope": original["evaluation_scope"],
        "status": "completed",
        "source_report": {
            "path": str(source),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "manifest_sha256": manifest_hash,
            "implementation_sha256": original.get("implementation_sha256"),
            "frozen_at": original.get("frozen_at"),
            "gates": original["gates"],
            "comparison_gates_passed": original["comparison_gates_passed"],
        },
        "policy_change": (
            "Cost and monetary budget excluded by supplied policy; observations unchanged."
        ),
        "manifest": spec.model_dump(mode="json"),
        "cases": rows,
    }
    summarize_comparison(
        report, spec, contract_only=original["evaluation_scope"] == "comparison_contract_test"
    )
    save_report(report, output, overwrite=False)
    return report


async def run_comparison(
    runtime: HarnessRuntime,
    spec: ComparisonSpec,
    output: Path,
    *,
    contract_only: bool = False,
) -> dict[str, Any]:
    _validate_routes(runtime, spec, contract_only)
    old_policy = runtime.config.runtime.model_copy(deep=True)
    old_gateway, old_graph = runtime.model_gateway, runtime.graph
    meter = ComparisonGateway(old_gateway, spec)
    policy = runtime.config.runtime
    policy.model_retry_attempts = 0  # Every transmitted request must have its own accounting row.
    policy.worker_max_concurrency = policy.reviewer_max_concurrency = 1
    if spec.budget.cost_budget_usd is not None:
        policy.cost_budget_usd = spec.budget.cost_budget_usd
    policy.node_timeout_seconds = spec.budget.call_timeout_seconds
    policy.run_timeout_seconds = spec.budget.run_timeout_seconds
    policy.recovery_enabled = False
    runtime.model_gateway = meter
    runtime.graph = build_root_graph(
        config=runtime.config,
        gateway=meter,
        prompts=runtime.prompt_registry,
        trace=TraceRecorder(enabled=False),
        tool_gateway=runtime.tool_gateway,
        skills=runtime.skill_registry,
        checkpointer=runtime.checkpointer,
    )
    manifest = spec.model_dump(mode="json")
    source_root = Path(__file__).parent
    implementation = hashlib.sha256()
    for source in sorted(source_root.rglob("*.py")):
        implementation.update(str(source.relative_to(source_root)).encode())
        implementation.update(b"\0" + source.read_bytes() + b"\0")
    report = {
        "version": spec.version,
        "evaluation_scope": "comparison_contract_test"
        if contract_only
        else "real_provider_engine_comparison",
        "status": "preflight",
        "frozen_at": datetime.now(UTC).isoformat(),
        "manifest": manifest,
        "implementation_sha256": implementation.hexdigest(),
        "manifest_sha256": hashlib.sha256(
            json.dumps(manifest, sort_keys=True).encode()
        ).hexdigest(),
        "runtime_policy": policy.model_dump(mode="json"),
        "agent_config": runtime.config.agents.model_dump(mode="json"),
        "prompt_hashes": {
            name: hashlib.sha256(runtime.prompt_registry.get(name).body.encode()).hexdigest()
            for name in ("planner", "worker", "reviewer", "judge", "synthesizer")
        },
        "provider": {
            "name": spec.provider,
            "model": spec.model,
            "api_family": runtime.config.models.providers[spec.provider].api_family,
            "structured_output_mode": (
                runtime.config.models.providers[spec.provider].structured_output_mode
            ),
            "target_fingerprint": runtime.provider_target_fingerprints.get(spec.provider),
        },
        "metric_notes": {
            "quality": "Whole-document JSON answers for the frozen, closed-world task set only.",
            "fees": "null without supplied channel pricing; failed calls have unknown usage.",
            "budget": (
                "Stop further calls after measured exhaustion; input usage can overrun "
                "a call budget and fails the gate. Null monetary budget excludes currency "
                "from comparison checks; existing engine admission limits remain in effect."
            ),
            "activations": "Blackboard ledger attempts; legacy has no equivalent and is null.",
            "sampling": "One pair per case; not a statistical or general task-quality claim.",
        },
        "quality_claim": False,
        "eligible_for_default_switch": False,
        "cases": [
            {"case_id": case.id, "engine": engine, "status": "not_run"}
            for case in spec.cases
            for engine in ("legacy", "blackboard")
        ],
    }
    manager = RunManager(runtime)
    owned_output = False
    try:
        save_report(report, output, overwrite=False)
        owned_output = True
        meter.on_change = lambda: _write_progress(report, output)
        if not contract_only:
            report["preflight_calls"] = meter.calls.setdefault("comparison-preflight", [])
            request = ModelRequest(
                run_id="comparison-preflight",
                trace_id="comparison-preflight",
                node_name="preflight",
                agent_role="worker-01",
                logical_model="worker-fast",
                max_output_tokens=256,
                messages=[
                    ModelMessage(role="user", content='Return JSON with "ok" equal to true.')
                ],
                response_schema={
                    "title": "Preflight",
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                    "additionalProperties": False,
                },
            )
            try:
                response = await meter.complete(request)
                if response.structured_output != {"ok": True}:
                    raise ValueError("preflight contract mismatch")
            except Exception as exc:
                report.update(status="preflight_failed", error_type=type(exc).__name__)
                return report
            finally:
                report["preflight_calls"] = meter.calls.get("comparison-preflight", [])
        report["status"] = "running"
        _write_progress(report, output)
        for index, case in enumerate(spec.cases):
            engines = ("legacy", "blackboard") if index % 2 == 0 else ("blackboard", "legacy")
            for engine in engines:
                row = next(
                    r for r in report["cases"] if r["case_id"] == case.id and r["engine"] == engine
                )
                request_data = {
                    "goal": case.goal + "\n最终交付必须仅包含所要求的 JSON 对象，不添加解释文字。",
                    "engine": engine,
                    "task_type": case.task_type,
                }
                if engine == "blackboard":
                    request_data["blackboard"] = {
                        "acceptance": [
                            {
                                "id": "saved-draft",
                                "description": "保存可读取的候选草稿；质量由外部评分。",
                                "claim_key": "draft.content",
                                "claim_type": "delivery",
                                "scope": {"verification": "stored_content_only"},
                                "required_checks": ["content_stored"],
                            }
                        ]
                    }
                started = time.perf_counter()
                run_id = await manager.start(TaskRequest.model_validate(request_data))
                row["run_id"] = run_id
                row["status"] = "running"
                row["model_calls"] = meter.calls.setdefault(run_id, [])
                _write_progress(report, output)
                result = await manager.wait(run_id)
                row.update(
                    status=result.status,
                    output=result.output,
                    latency_ms=round((time.perf_counter() - started) * 1000),
                    error_codes=[error.code for error in result.errors],
                    errors=[error.model_dump(mode="json") for error in result.errors],
                )
                calls = meter.calls.get(run_id, [])
                row["model_calls"] = calls
                row["checks"] = {
                    **score_output(result.output, case.expected),
                    "status": result.status == "succeeded",
                    "no_errors": not result.errors,
                }
                row["passed"] = all(row["checks"].values())
                row["budget_passed"] = _budget_passed(row, spec)
                if engine == "blackboard":
                    store = await runtime.blackboard_store()
                    records = await build_activation_store(store).records(run_id)
                    row["activations"] = [
                        {
                            "id": r.candidate.id,
                            "attempt": r.attempt,
                            "source": r.spec.id,
                            "status": r.status,
                        }
                        for r in records
                    ]
                execution = (await runtime.run_store.get(run_id)).execution
                row["execution"] = execution.model_dump(mode="json") if execution else None
                _write_progress(report, output)
            if any(c["status"] != "ok" for calls in meter.calls.values() for c in calls):
                report["status"] = "interrupted_provider_error"
                break
        else:
            report["status"] = "completed"
        return report
    except BaseException as exc:
        report.update(status="interrupted", error_type=type(exc).__name__)
        raise
    finally:
        await manager.close()
        meter.on_change = None
        for row in report["cases"]:
            if row["status"] == "running":
                row["status"] = "interrupted"
        runtime.model_gateway, runtime.graph = old_gateway, old_graph
        # Preserve the object referenced by the original gateway and graph.
        for name, value in old_policy.__dict__.items():
            setattr(policy, name, value)
        if owned_output:
            summarize_comparison(report, spec, contract_only=contract_only)
            report["finished_at"] = datetime.now(UTC).isoformat()
            _write_progress(report, output)
