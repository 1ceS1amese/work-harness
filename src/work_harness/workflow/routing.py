from __future__ import annotations

from langgraph.types import Send

from work_harness.config.models import RuntimeConfig
from work_harness.domain.enums import DecisionAction, RiskLevel, RunStatus
from work_harness.workflow.contracts import ReviewAssignment, WorkerAssignment
from work_harness.workflow.state import GraphState, latest_artifacts, latest_reviews


def _dispatch_workers(state: GraphState) -> list[Send] | str:
    pending = state.get("pending_subtasks", [])
    if not pending:
        return "validate"
    latest = {item["subtask_id"]: item for item in latest_artifacts(state.get("artifacts", []))}
    required = state.get("required_versions", {})
    completed = {
        subtask_id
        for subtask_id, artifact in latest.items()
        if artifact.get("status") == "success"
        and int(artifact["version"]) >= required.get(subtask_id, 1)
    }
    ready = [item for item in pending if set(item.get("dependencies", [])) <= completed]
    if not ready:
        return "fail"
    sends: list[Send] = []
    for subtask in ready:
        previous = latest.get(subtask["subtask_id"])
        next_version = int(previous["version"]) + 1 if previous else 1
        assignment = WorkerAssignment(
            run_id=state["run_id"],
            task=state["task"],
            subtask=subtask,
            artifact_version=next_version,
            repair_round=state.get("repair_round", 0),
            dependency_artifacts=[latest[key] for key in subtask.get("dependencies", [])],
            previous_artifact=previous,
            feedback=[
                review
                for review in state.get("reviews", [])
                if previous
                and review["artifact_id"] == previous["artifact_id"]
                and review["artifact_version"] == previous["version"]
            ],
            repair_reasons=state.get("decision", {}).get("reasons", []) if previous else [],
            validation_issues=[
                issue
                for issue in state.get("validation_issues", [])
                if issue.get("subtask_id") == subtask["subtask_id"]
            ],
        )
        sends.append(Send("worker", {"assignment": assignment.model_dump(mode="json")}))
    return sends


def _dispatch_after_schedule(state: GraphState) -> list[Send] | str:
    if state.get("status") == RunStatus.FAILED:
        return "fail"
    if not state.get("pending_subtasks"):
        return "validate"
    return _dispatch_workers(state)


def _review_dimensions(state: GraphState, runtime: RuntimeConfig) -> tuple[str, ...]:
    cross_review = state.get("task", {}).get("assignment", {}).get("cross_review", True)
    if not cross_review:
        return ("constraint",)
    if runtime.review_strategy == "three_dimensional":
        return ("fact", "logic", "constraint")
    if runtime.review_strategy == "comprehensive":
        return ("comprehensive",)
    risk = RiskLevel(state.get("task", {}).get("risk_level", RiskLevel.LOW))
    task_type = str(state.get("task", {}).get("task_type", "general"))
    if risk in runtime.deep_review_risk_levels or task_type in runtime.deep_review_task_types:
        return ("fact", "logic", "constraint")
    return ("comprehensive",)


def _dispatch_reviewers(
    state: GraphState,
    runtime: RuntimeConfig,
) -> list[Send] | str:
    artifacts = latest_artifacts(state.get("artifacts", []))
    if not artifacts:
        return "judge"
    dimensions = _review_dimensions(state, runtime)
    reviewed = {
        (item["artifact_id"], item["artifact_version"], item["dimension"])
        for item in latest_reviews(state.get("reviews", []), artifacts)
    }
    subtasks = {item["subtask_id"]: item for item in state["plan"]["subtasks"]}
    sends: list[Send] = []
    for artifact in artifacts:
        for dimension in dimensions:
            if (artifact["artifact_id"], artifact["version"], dimension) in reviewed:
                continue
            assignment = ReviewAssignment(
                run_id=state["run_id"],
                task=state["task"],
                subtask=subtasks[artifact["subtask_id"]],
                artifact=artifact,
                dimension=dimension,
                repair_round=state.get("repair_round", 0),
            )
            sends.append(Send("reviewer", {"assignment": assignment.model_dump(mode="json")}))
    return sends or "judge"


def _route_decision(state: GraphState, max_repair_rounds: int) -> str:
    action = state["decision"]["action"]
    if action == DecisionAction.APPROVE:
        return "synthesize"
    if action == DecisionAction.REPAIR and state.get("repair_round", 0) < max_repair_rounds:
        return "repair_plan"
    if action == DecisionAction.REQUEST_HUMAN:
        return "human_gate"
    return "fail"


def _route_after_human(state: GraphState) -> str:
    return "synthesize" if state["decision"]["action"] == DecisionAction.APPROVE else "fail"
