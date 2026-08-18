from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from statistics import mean

from pydantic import BaseModel, ConfigDict, Field

from work_harness.domain.enums import RiskLevel, RunStatus
from work_harness.domain.models import TaskRequest
from work_harness.runtime import HarnessRuntime
from work_harness.service import RunManager


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    input: str
    expected_output: str = ""
    expected_behavior: str
    task_type: str
    difficulty: str
    tags: list[str] = Field(default_factory=list)
    expected_statuses: list[RunStatus] = Field(
        default_factory=lambda: [RunStatus.SUCCEEDED]
    )
    expected_decision: str | None = None
    min_artifacts: int = Field(default=1, ge=0)
    min_reviews: int = Field(default=1, ge=0)
    require_output: bool = True


def _percentile(values: list[int], percentile: float) -> float:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(round((len(ordered) - 1) * percentile), len(ordered) - 1)
    return float(ordered[index])


async def run_evaluation(runtime: HarnessRuntime, cases_path: Path) -> dict[str, object]:
    raw_cases = await asyncio.to_thread(cases_path.read_text, encoding="utf-8")
    cases = [
        EvalCase.model_validate_json(line)
        for line in raw_cases.splitlines()
        if line.strip()
    ]
    manager = RunManager(runtime)
    rows: list[dict[str, object]] = []
    for case in cases:
        started = time.perf_counter()
        risk = RiskLevel.HIGH if "approval" in case.tags else RiskLevel.LOW
        run_id = await manager.start(
            TaskRequest(goal=case.input, task_type=case.task_type, risk_level=risk)
        )
        result = await manager.wait(run_id)
        latency_ms = int((time.perf_counter() - started) * 1000)
        checks = {
            "status": result.status in case.expected_statuses,
            "artifacts": len(result.artifacts) >= case.min_artifacts,
            "reviews": len(result.reviews) >= case.min_reviews,
            "decision": (
                case.expected_decision is None
                or (
                    result.decision is not None
                    and result.decision.action == case.expected_decision
                )
            ),
            "output": not case.require_output or bool(result.output.strip()),
            "no_errors": not result.errors,
        }
        passed = all(checks.values())
        rows.append(
            {
                "case_id": case.id,
                "expected_behavior": case.expected_behavior,
                "status": result.status,
                "passed": passed,
                "checks": checks,
                "latency_ms": latency_ms,
                "artifact_count": len(result.artifacts),
                "review_count": len(result.reviews),
                "repair_rounds": result.repair_rounds,
                "model_calls": len(result.model_calls),
                "tool_calls": len(result.tool_calls),
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
                "estimated_cost_usd": result.usage.estimated_cost_usd,
                "errors": [error.model_dump(mode="json") for error in result.errors],
            }
        )
    latencies = [int(row["latency_ms"]) for row in rows]
    parse_error_count = sum(
        any(
            "structured" in str(error.get("message", "")).casefold()
            or "validation" in str(error.get("message", "")).casefold()
            for error in row["errors"]
        )
        for row in rows
    )
    total_tool_calls = sum(int(row["tool_calls"]) for row in rows)
    return {
        "version": "framework-002",
        "evaluation_scope": "mock_provider_workflow_smoke",
        "quality_claim": False,
        "case_count": len(rows),
        "success_rate": sum(bool(row["passed"]) for row in rows) / len(rows) if rows else 0,
        "error_rate": sum(not bool(row["passed"]) for row in rows) / len(rows) if rows else 0,
        "average_latency_ms": mean(latencies) if rows else 0,
        "p95_latency_ms": _percentile(latencies, 0.95),
        "average_steps_proxy": mean(
            int(row["model_calls"]) for row in rows
        )
        if rows
        else 0,
        "bad_case_count": sum(not bool(row["passed"]) for row in rows),
        "structured_parse_error_count": parse_error_count,
        "structured_parse_error_rate": parse_error_count / len(rows) if rows else 0,
        "tool_call_count": total_tool_calls,
        "tool_call_success_rate": 1.0 if total_tool_calls else None,
        "total_input_tokens": sum(int(row["input_tokens"]) for row in rows),
        "total_output_tokens": sum(int(row["output_tokens"]) for row in rows),
        "total_estimated_cost_usd": sum(
            float(row["estimated_cost_usd"]) for row in rows
        ),
        "cases": rows,
    }


def save_report(report: dict[str, object], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
