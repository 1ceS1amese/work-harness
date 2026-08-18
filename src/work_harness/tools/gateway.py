from __future__ import annotations

import asyncio
import json
import time
from uuid import uuid4

from work_harness.observability.logging import get_logger
from work_harness.tools.base import (
    ToolApprovalRequired,
    ToolExecutionResult,
    ToolPermissionError,
)
from work_harness.tools.registry import ToolRegistry


class ToolGateway:
    def __init__(
        self, registry: ToolRegistry, *, global_max_result_chars: int | None = None
    ) -> None:
        self._registry = registry
        self._global_max_result_chars = global_max_result_chars
        self._logger = get_logger("work_harness.tool_gateway")

    def definitions(self, *, role: str, names: list[str]) -> list[dict[str, object]]:
        return self._registry.definitions(role=role, names=names)

    async def execute(
        self,
        *,
        run_id: str,
        role: str,
        tool_name: str,
        arguments: dict[str, object],
        approved: bool = False,
        call_id: str | None = None,
    ) -> ToolExecutionResult:
        handler = self._registry.get(tool_name)
        config = self._registry.config.tools[tool_name]
        if role not in config.allowed_roles:
            raise ToolPermissionError(f"role {role} is not allowed to use {tool_name}")
        if config.requires_approval and not approved:
            raise ToolApprovalRequired(f"tool {tool_name} requires approval")

        validated_input = handler.input_model.model_validate(arguments)
        call_id = call_id or f"tool_{uuid4().hex}"
        if not call_id.strip() or len(call_id) > 200:
            raise ValueError("tool call id must contain between 1 and 200 characters")
        started = time.perf_counter()
        result = await asyncio.wait_for(
            handler.execute(validated_input),
            timeout=config.timeout_seconds,
        )
        validated_output = handler.output_model.model_validate(result)
        duration_ms = int((time.perf_counter() - started) * 1000)
        output = validated_output.model_dump(mode="json")
        serialized = json.dumps(output, ensure_ascii=False)
        max_result_chars = config.max_result_chars
        if self._global_max_result_chars is not None:
            max_result_chars = min(max_result_chars, self._global_max_result_chars)
        truncated = len(serialized) > max_result_chars
        summary = serialized[:max_result_chars]
        self._logger.info(
            "tool_completed",
            extra={"run_id": run_id, "node": role, "duration_ms": duration_ms},
        )
        return ToolExecutionResult(
            tool_name=tool_name,
            call_id=call_id,
            output=output,
            output_summary=summary,
            truncated=truncated,
            duration_ms=duration_ms,
        )
