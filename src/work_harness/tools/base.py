from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class ToolPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolExecutionResult(ToolPayload):
    tool_name: str
    call_id: str
    output: dict[str, object]
    output_summary: str
    truncated: bool = False
    duration_ms: int = Field(ge=0)
    cache_hit: bool = False


class ToolHandler(Protocol):
    name: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]

    async def execute(self, payload: BaseModel) -> BaseModel: ...


class ToolError(RuntimeError):
    pass


class ToolPermissionError(ToolError):
    pass


class ToolApprovalRequired(ToolError):
    pass


class ToolNotFoundError(ToolError):
    pass
