from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

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

SCHEMAS: dict[str, type[BaseModel]] = {
    "task.schema.json": TaskSpec,
    "plan.schema.json": Plan,
    "artifact.schema.json": Artifact,
    "review.schema.json": Review,
    "decision.schema.json": Decision,
    "model_request.schema.json": ModelRequest,
    "model_response.schema.json": ModelResponse,
    "run_result.schema.json": RunResult,
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

