"""Validated, task-scoped handoffs between the coordinator and Agent graphs."""

from __future__ import annotations

import json
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field, model_validator

from work_harness.domain.models import (
    AgentInteraction,
    Artifact,
    ContractModel,
    Review,
    SubtaskSpec,
    TaskSpec,
)

# Changes to topology or checkpoint contracts require a new revision, even when
# the package version has not changed. API and Worker must agree on this value.
WORKFLOW_REVISION = "agent-subgraphs-v2"


def stable_id(kind: str, *parts: object) -> str:
    identity = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return f"{kind}_{uuid5(NAMESPACE_URL, identity).hex}"


class WorkerAssignment(ContractModel):
    run_id: str
    task: TaskSpec
    subtask: SubtaskSpec
    artifact_version: int = Field(default=1, ge=1)
    repair_round: int = Field(default=0, ge=0)
    dependency_artifacts: list[Artifact] = Field(default_factory=list)
    previous_artifact: Artifact | None = None
    feedback: list[Review] = Field(default_factory=list)
    repair_reasons: list[str] = Field(default_factory=list)
    validation_issues: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_context(self) -> WorkerAssignment:
        received = [item.subtask_id for item in self.dependency_artifacts]
        if len(received) != len(set(received)) or set(received) != set(self.subtask.dependencies):
            raise ValueError("handoff must contain exactly the declared dependencies")
        for artifact in self.dependency_artifacts:
            if artifact.run_id != self.run_id or artifact.status != "success":
                raise ValueError("dependency must be a successful artifact from this run")
        previous = self.previous_artifact
        if previous is not None:
            if (
                previous.run_id != self.run_id
                or previous.subtask_id != self.subtask.subtask_id
                or self.artifact_version != previous.version + 1
            ):
                raise ValueError("repair must follow the previous version of this subtask")
        elif self.artifact_version != 1:
            raise ValueError("a new subtask must start at version 1")
        for review in self.feedback:
            if previous is None or (
                review.artifact_id != previous.artifact_id
                or review.artifact_version != previous.version
            ):
                raise ValueError("feedback must refer to the previous artifact version")
        return self


class ReviewAssignment(ContractModel):
    run_id: str
    task: TaskSpec
    subtask: SubtaskSpec
    artifact: Artifact
    dimension: str = Field(min_length=1)
    repair_round: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_artifact(self) -> ReviewAssignment:
        if (
            self.artifact.run_id != self.run_id
            or self.artifact.subtask_id != self.subtask.subtask_id
            or self.artifact.producer != self.subtask.worker_role
        ):
            raise ValueError("review handoff does not match its run and assigned subtask")
        return self


def interaction(
    *,
    run_id: str,
    kind: str,
    sender: str,
    recipient: str,
    subtask_id: str,
    artifact_version: int,
    repair_round: int,
    artifact_id: str | None = None,
    review_id: str | None = None,
) -> dict[str, Any]:
    return AgentInteraction(
        interaction_id=stable_id(
            "interaction",
            run_id,
            kind,
            sender,
            recipient,
            subtask_id,
            artifact_id,
            artifact_version,
            review_id,
            repair_round,
        ),
        kind=kind,
        sender=sender,
        recipient=recipient,
        subtask_id=subtask_id,
        artifact_id=artifact_id,
        artifact_version=artifact_version,
        review_id=review_id,
        repair_round=repair_round,
    ).model_dump(mode="json")
