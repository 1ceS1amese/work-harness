from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class GraphState(TypedDict, total=False):
    run_id: str
    raw_input: dict[str, Any]
    task: dict[str, Any]
    plan: dict[str, Any]
    pending_subtasks: list[dict[str, Any]]
    subtask: dict[str, Any]
    artifact_version: int
    artifacts: Annotated[list[dict[str, Any]], operator.add]
    validation_issues: list[dict[str, Any]]
    review_job: dict[str, Any]
    reviews: Annotated[list[dict[str, Any]], operator.add]
    decision: dict[str, Any]
    repair_round: int
    status: str
    final_output: str
    errors: Annotated[list[dict[str, Any]], operator.add]
    model_calls: Annotated[list[dict[str, Any]], operator.add]
    tool_calls: Annotated[list[dict[str, Any]], operator.add]


def latest_artifacts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for item in items:
        subtask_id = str(item["subtask_id"])
        if subtask_id not in latest or int(item["version"]) > int(latest[subtask_id]["version"]):
            latest[subtask_id] = item
    return [latest[key] for key in sorted(latest)]


def latest_reviews(
    reviews: list[dict[str, Any]], artifacts: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    versions = {item["artifact_id"]: item["version"] for item in latest_artifacts(artifacts)}
    return [
        review
        for review in reviews
        if versions.get(review["artifact_id"]) == review["artifact_version"]
    ]
