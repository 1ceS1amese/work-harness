from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from work_harness.blackboard.inputs import HumanInput, HumanInputResult
from work_harness.blackboard.models import Fact, Hint, Intent
from work_harness.blackboard.protocol import BlackboardDelta, BoardSnapshot
from work_harness.domain.models import (
    Artifact,
    Decision,
    ModelRequest,
    ModelResponse,
    Plan,
    Review,
    RunResult,
    TaskSpec,
)
from work_harness.knowledge_sources.base import BoardView, KnowledgeSourceSpec
from work_harness.workflow.contracts import ReviewAssignment, WorkerAssignment

SCHEMAS: dict[str, type[BaseModel]] = {
    "blackboard_input.schema.json": HumanInput,
    "blackboard_input_result.schema.json": HumanInputResult,
    "blackboard_fact.schema.json": Fact,
    "blackboard_intent.schema.json": Intent,
    "blackboard_hint.schema.json": Hint,
    "blackboard_delta.schema.json": BlackboardDelta,
    "blackboard_snapshot.schema.json": BoardSnapshot,
    "blackboard_view.schema.json": BoardView,
    "knowledge_source.schema.json": KnowledgeSourceSpec,
    "task.schema.json": TaskSpec,
    "plan.schema.json": Plan,
    "artifact.schema.json": Artifact,
    "review.schema.json": Review,
    "decision.schema.json": Decision,
    "model_request.schema.json": ModelRequest,
    "model_response.schema.json": ModelResponse,
    "run_result.schema.json": RunResult,
    "worker_assignment.schema.json": WorkerAssignment,
    "review_assignment.schema.json": ReviewAssignment,
}


def export_schemas(output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for filename, model in SCHEMAS.items():
        path = output_dir / filename
        path.write_text(
            json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        written.append(path)
    return written
