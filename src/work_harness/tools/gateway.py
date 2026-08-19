from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from work_harness.observability.logging import get_logger
from work_harness.tools.base import (
    ToolApprovalRequired,
    ToolExecutionResult,
    ToolPermissionError,
)
from work_harness.tools.registry import ToolRegistry


@dataclass(frozen=True, slots=True)
class _CachedToolResult:
    output: dict[str, Any]
    output_summary: str
    truncated: bool


class ToolGateway:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        global_max_result_chars: int | None = None,
        cache_max_entries: int = 0,
    ) -> None:
        self._registry = registry
        self._global_max_result_chars = global_max_result_chars
        self._cache_max_entries = cache_max_entries
        self._cache: OrderedDict[tuple[str, str, str, str], _CachedToolResult] = (
            OrderedDict()
        )
        self._cache_lock = asyncio.Lock()
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
        cache_key = self._cache_key(run_id, role, tool_name, validated_input.model_dump())
        if config.idempotent and self._cache_max_entries:
            async with self._cache_lock:
                cached = self._cache.get(cache_key)
                if cached is not None:
                    self._cache.move_to_end(cache_key)
                    self._logger.info(
                        "tool_cache_hit",
                        extra={"run_id": run_id, "node": role, "tool": tool_name},
                    )
                    return ToolExecutionResult(
                        tool_name=tool_name,
                        call_id=call_id,
                        output=cached.output,
                        output_summary=cached.output_summary,
                        truncated=cached.truncated,
                        duration_ms=0,
                        cache_hit=True,
                    )
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
        if config.idempotent and self._cache_max_entries:
            async with self._cache_lock:
                self._cache[cache_key] = _CachedToolResult(
                    output=output,
                    output_summary=summary,
                    truncated=truncated,
                )
                self._cache.move_to_end(cache_key)
                while len(self._cache) > self._cache_max_entries:
                    self._cache.popitem(last=False)
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

    @staticmethod
    def _cache_key(
        run_id: str,
        role: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> tuple[str, str, str, str]:
        serialized = json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        fingerprint = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        return run_id, role, tool_name, fingerprint

    async def clear_run(self, run_id: str) -> None:
        async with self._cache_lock:
            stale = [key for key in self._cache if key[0] == run_id]
            for key in stale:
                self._cache.pop(key, None)
