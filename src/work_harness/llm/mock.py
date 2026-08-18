from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from work_harness.domain.enums import DecisionAction, ReviewVerdict
from work_harness.domain.models import ModelRequest, ModelResponse, Usage


class MockProvider:
    """Deterministic provider for tests, local development, and baseline replay."""

    protocol_version = "1.0"
    capabilities = frozenset({"chat", "structured_output", "tool_calling"})

    def __init__(self, name: str = "mock") -> None:
        self.name = name

    async def healthcheck(self) -> bool:
        return True

    async def complete(self, request: ModelRequest, *, model: str) -> ModelResponse:
        operation = str(request.metadata.get("operation", "chat"))
        payload = request.metadata.get("payload", {})
        structured = self._dispatch(operation, payload, request.run_id)
        content = structured.get("output", "") if operation == "synthesize" else json.dumps(
            structured, ensure_ascii=False, default=str
        )
        return ModelResponse(
            content=content,
            structured_output=structured,
            finish_reason="stop",
            usage=Usage(input_tokens=64, output_tokens=128),
            latency_ms=1,
            provider=self.name,
            model=model,
            provider_request_id=f"mock_{uuid4().hex}",
        )

    def _dispatch(self, operation: str, payload: dict[str, Any], run_id: str) -> dict[str, Any]:
        if operation == "plan":
            goal = payload["task"]["goal"]
            return {
                "version": 1,
                "rationale": "Use two independent perspectives before synthesis.",
                "subtasks": [
                    {
                        "subtask_id": "analysis",
                        "title": "Analyze the request",
                        "description": f"Identify the core work and assumptions for: {goal}",
                        "dependencies": [],
                        "worker_role": "worker-01",
                        "logical_model": "worker-fast",
                        "allowed_tools": [],
                        "acceptance_criteria": ["Core requirements and assumptions are explicit"],
                        "token_budget": 3000,
                        "cost_budget_usd": 0.2,
                    },
                    {
                        "subtask_id": "delivery",
                        "title": "Draft an actionable result",
                        "description": f"Produce an actionable contribution for: {goal}",
                        "dependencies": [],
                        "worker_role": "worker-01",
                        "logical_model": "worker-fast",
                        "allowed_tools": [],
                        "acceptance_criteria": [
                            "Result is concrete and follows stated constraints"
                        ],
                        "token_budget": 3000,
                        "cost_budget_usd": 0.2,
                    },
                ],
            }
        if operation == "worker":
            subtask = payload["subtask"]
            version = int(payload.get("version", 1))
            return {
                "run_id": run_id,
                "subtask_id": subtask["subtask_id"],
                "producer": subtask["worker_role"],
                "version": version,
                "content": f"{subtask['title']}: {subtask['description']}",
                "evidence": [
                    {
                        "source": "task_input",
                        "summary": (
                            "Derived directly from the normalized task and acceptance criteria."
                        ),
                    }
                ],
                "assumptions": ["Mock Provider validates workflow behavior, not answer quality."],
                "unresolved_questions": [],
                "confidence": 0.8,
                "status": "success",
                "duration_ms": 1,
            }
        if operation == "review":
            artifact = payload["artifact"]
            return {
                "artifact_id": artifact["artifact_id"],
                "artifact_version": artifact["version"],
                "reviewer": "reviewer",
                "dimension": payload["dimension"],
                "verdict": ReviewVerdict.PASS,
                "issues": [],
                "confidence": 0.9,
            }
        if operation == "judge":
            task = payload.get("task", {})
            if task.get("risk_level") in {"high", "critical"}:
                return {
                    "action": DecisionAction.REQUEST_HUMAN,
                    "reasons": ["High-risk work requires explicit human approval."],
                    "target_subtasks": [],
                    "human_prompt": "Approve continuation of this high-risk task?",
                }
            validation_issues = payload.get("validation_issues", [])
            reviews = payload.get("reviews", [])
            failed_reviews = [item for item in reviews if item["verdict"] != ReviewVerdict.PASS]
            if validation_issues or failed_reviews:
                targets = sorted(
                    {
                        item.get("subtask_id", "")
                        for item in validation_issues
                        if item.get("subtask_id")
                    }
                )
                return {
                    "action": DecisionAction.REPAIR,
                    "reasons": ["Deterministic validation or review requires revision."],
                    "target_subtasks": targets,
                }
            return {
                "action": DecisionAction.APPROVE,
                "reasons": ["All deterministic checks and independent reviews passed."],
                "target_subtasks": [],
            }
        if operation == "synthesize":
            artifacts = payload.get("artifacts", [])
            output = "\n\n".join(item["content"] for item in artifacts)
            return {"output": output}
        return {"output": request_text(payload)}


def request_text(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)
