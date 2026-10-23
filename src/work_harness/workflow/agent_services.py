from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any, TypeVar
from uuid import uuid4

from pydantic import BaseModel

from work_harness.config.models import AppConfig
from work_harness.domain.enums import RiskLevel
from work_harness.domain.models import ModelCallRecord, ModelMessage, ModelRequest, ModelResponse
from work_harness.llm.gateway import ModelGateway
from work_harness.observability.trace import TraceRecorder
from work_harness.prompts import PromptRegistry
from work_harness.skills import SkillRegistry
from work_harness.tools.gateway import ToolGateway
from work_harness.workflow.state import GraphState

ContractT = TypeVar("ContractT", bound=BaseModel)


class AgentServices:
    def __init__(
        self,
        *,
        config: AppConfig,
        gateway: ModelGateway,
        prompts: PromptRegistry,
        trace: TraceRecorder,
        tool_gateway: ToolGateway,
        skills: SkillRegistry,
    ) -> None:
        self.config = config
        self.gateway = gateway
        self.prompts = prompts
        self.trace = trace
        self.tool_gateway = tool_gateway
        self.skills = skills
        self.worker_slots = asyncio.Semaphore(config.runtime.worker_max_concurrency)
        self.reviewer_slots = asyncio.Semaphore(config.runtime.reviewer_max_concurrency)

    async def structured_call(
        self,
        *,
        state: GraphState,
        node_name: str,
        agent_role: str,
        prompt_name: str,
        logical_model: str,
        operation: str,
        payload: dict[str, Any],
        output_model: type[ContractT],
        max_output_tokens: int,
    ) -> tuple[ContractT, ModelResponse]:
        prompt = self.prompts.get(prompt_name)
        trace_id = f"trace_{uuid4().hex}"
        task = state.get("task", {})
        request = ModelRequest(
            run_id=state["run_id"],
            trace_id=trace_id,
            node_name=node_name,
            agent_role=agent_role,
            logical_model=logical_model,
            model_selection=task.get("model_selection"),
            messages=[
                ModelMessage(role="system", content=prompt.body),
                ModelMessage(
                    role="user",
                    content=json.dumps(payload, ensure_ascii=False, default=str),
                ),
            ],
            required_capabilities={"chat", "structured_output"},
            response_schema=output_model.model_json_schema(),
            timeout_seconds=self.config.runtime.node_timeout_seconds,
            max_output_tokens=max_output_tokens,
            risk_level=task.get("risk_level", RiskLevel.LOW),
            metadata={
                "operation": operation,
                "payload": payload,
                "prompt_name": prompt.name,
                "prompt_version": prompt.version,
            },
        )
        response = await self.gateway.complete(request)
        if response.structured_output is None:
            raise ValueError(f"{node_name} did not return structured output")
        return output_model.model_validate(response.structured_output), response

    @staticmethod
    def model_call(
        node: str,
        response: ModelResponse,
        *,
        skill_ids: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        return ModelCallRecord(
            node=node,
            provider=response.provider,
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cached_tokens=response.usage.cached_tokens,
            usage_complete=response.usage.usage_complete,
            estimated_cost_usd=response.usage.estimated_cost_usd,
            latency_ms=response.latency_ms,
            retries=response.retries,
            fallback_from=response.fallback_from,
            skill_ids=list(skill_ids),
        ).model_dump(mode="json")

    @staticmethod
    def fingerprint(value: Any) -> str:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def parse_tool_call(raw_call: dict[str, Any]) -> tuple[str, str, dict[str, object]]:
        function = raw_call.get("function")
        call_id = raw_call.get("id")
        if not isinstance(call_id, str) or not isinstance(function, dict):
            raise ValueError("model returned an invalid tool call envelope")
        name = function.get("name")
        raw_arguments = function.get("arguments", {})
        if not isinstance(name, str):
            raise ValueError("model tool call is missing a function name")
        if isinstance(raw_arguments, str):
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError as exc:
                raise ValueError("model returned invalid JSON tool arguments") from exc
        else:
            arguments = raw_arguments
        if not isinstance(arguments, dict):
            raise ValueError("model tool arguments must be a JSON object")
        return call_id, name, arguments
