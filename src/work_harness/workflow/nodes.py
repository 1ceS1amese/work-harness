from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any, TypeVar
from uuid import uuid4

from pydantic import BaseModel

from work_harness.config.models import AppConfig
from work_harness.domain.enums import (
    AgentFunction,
    AllocationMode,
    ArtifactStatus,
    DecisionAction,
    RiskLevel,
    RunStatus,
    Severity,
)
from work_harness.domain.models import (
    AgentAssignment,
    Artifact,
    Decision,
    ModelCallRecord,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    Plan,
    Review,
    RunError,
    SubtaskSpec,
    TaskRequest,
    TaskSpec,
    ToolCallRecord,
)
from work_harness.llm.gateway import ModelGateway
from work_harness.observability.trace import TraceRecorder
from work_harness.prompts import PromptRegistry
from work_harness.tools.gateway import ToolGateway
from work_harness.workflow.state import GraphState, latest_artifacts, latest_reviews

ContractT = TypeVar("ContractT", bound=BaseModel)


class WorkflowNodes:
    def __init__(
        self,
        *,
        config: AppConfig,
        gateway: ModelGateway,
        prompts: PromptRegistry,
        trace: TraceRecorder,
        tool_gateway: ToolGateway,
    ) -> None:
        self.config = config
        self.gateway = gateway
        self.prompts = prompts
        self.trace = trace
        self.tool_gateway = tool_gateway
        self.worker_slots = asyncio.Semaphore(config.runtime.worker_max_concurrency)
        self.reviewer_slots = asyncio.Semaphore(config.runtime.reviewer_max_concurrency)

    async def _structured_call(
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
        token_budget: int,
        cost_budget_usd: float,
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
            token_budget=token_budget,
            cost_budget_usd=cost_budget_usd,
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
    def _model_call(node: str, response: ModelResponse) -> dict[str, Any]:
        return ModelCallRecord(
            node=node,
            provider=response.provider,
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cached_tokens=response.usage.cached_tokens,
            estimated_cost_usd=response.usage.estimated_cost_usd,
            latency_ms=response.latency_ms,
            retries=response.retries,
            fallback_from=response.fallback_from,
        ).model_dump(mode="json")

    @staticmethod
    def _fingerprint(value: Any) -> str:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def _parse_tool_call(raw_call: dict[str, Any]) -> tuple[str, str, dict[str, object]]:
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

    async def _worker_call(
        self,
        *,
        state: GraphState,
        subtask: SubtaskSpec,
        token_budget: int,
        cost_budget_usd: float,
        max_steps: int,
    ) -> tuple[Artifact, list[ModelResponse], list[dict[str, Any]]]:
        prompt = self.prompts.get("worker")
        payload = {
            "task": state["task"],
            "subtask": subtask.model_dump(mode="json"),
            "version": state.get("artifact_version", 1),
            "repair_round": state.get("repair_round", 0),
        }
        messages = [
            ModelMessage(role="system", content=prompt.body),
            ModelMessage(
                role="user",
                content=json.dumps(payload, ensure_ascii=False, default=str),
            ),
        ]
        tools = self.tool_gateway.definitions(
            role=subtask.worker_role,
            names=subtask.allowed_tools,
        )
        responses: list[ModelResponse] = []
        tool_records: list[dict[str, Any]] = []
        for _ in range(max_steps):
            request = ModelRequest(
                run_id=state["run_id"],
                trace_id=f"trace_{uuid4().hex}",
                node_name="worker",
                agent_role=subtask.worker_role,
                logical_model=subtask.logical_model,
                messages=messages,
                required_capabilities={"chat", "structured_output"}
                | ({"tool_calling"} if tools else set()),
                tools=tools,
                response_schema=Artifact.model_json_schema(),
                timeout_seconds=self.config.runtime.node_timeout_seconds,
                token_budget=token_budget,
                cost_budget_usd=cost_budget_usd,
                risk_level=state["task"].get("risk_level", RiskLevel.LOW),
                metadata={
                    "operation": "worker",
                    "payload": payload,
                    "prompt_name": prompt.name,
                    "prompt_version": prompt.version,
                    "tool_round": len(tool_records),
                },
            )
            response = await self.gateway.complete(request)
            responses.append(response)
            if response.tool_calls:
                if not tools:
                    raise ValueError("worker requested a tool that was not advertised")
                messages.append(
                    ModelMessage(
                        role="assistant",
                        content=response.content,
                        tool_calls=response.tool_calls,
                    )
                )
                for raw_call in response.tool_calls:
                    call_id, name, arguments = self._parse_tool_call(raw_call)
                    if name not in subtask.allowed_tools:
                        raise ValueError(f"worker requested a disallowed tool: {name}")
                    execution = await self.tool_gateway.execute(
                        run_id=state["run_id"],
                        role=subtask.worker_role,
                        tool_name=name,
                        arguments=arguments,
                        call_id=call_id,
                    )
                    tool_records.append(
                        ToolCallRecord(
                            node="worker",
                            role=subtask.worker_role,
                            tool_name=name,
                            call_id=execution.call_id,
                            input_fingerprint=self._fingerprint(arguments),
                            output_summary=execution.output_summary,
                            truncated=execution.truncated,
                            duration_ms=execution.duration_ms,
                        ).model_dump(mode="json")
                    )
                    messages.append(
                        ModelMessage(
                            role="tool",
                            tool_call_id=execution.call_id,
                            content=execution.output_summary,
                        )
                    )
                continue
            if response.structured_output is None:
                raise ValueError("worker did not return structured output")
            return Artifact.model_validate(response.structured_output), responses, tool_records
        raise ValueError("worker exceeded its configured model/tool step limit")

    def _budget_exceeded(self, state: GraphState) -> bool:
        calls = state.get("model_calls", [])
        tokens = sum(
            int(item.get("input_tokens", 0)) + int(item.get("output_tokens", 0))
            for item in calls
        )
        cost = sum(float(item.get("estimated_cost_usd", 0)) for item in calls)
        return (
            tokens >= self.config.runtime.token_budget
            or cost >= self.config.runtime.cost_budget_usd
        )

    async def normalize(self, state: GraphState) -> dict[str, Any]:
        run_id = state["run_id"]
        async with self.trace.span(run_id=run_id, node="normalize"):
            request = TaskRequest.model_validate(state["raw_input"])
            risk = request.risk_level
            lower_goal = request.goal.casefold()
            high_risk_markers = (
                "删除生产",
                "生产数据库",
                "delete production",
                "drop database",
                "send email",
                "publish externally",
            )
            if risk == RiskLevel.LOW and any(marker in lower_goal for marker in high_risk_markers):
                risk = RiskLevel.HIGH
            task = TaskSpec(
                goal=request.goal,
                context=request.context,
                constraints=request.constraints,
                expected_output=request.expected_output,
                task_type=request.task_type,
                risk_level=risk,
                assignment=request.assignment,
            )
            return {
                "task": task.model_dump(mode="json"),
                "status": RunStatus.RUNNING.value,
                "repair_round": 0,
            }

    async def plan(self, state: GraphState) -> dict[str, Any]:
        async with self.trace.span(run_id=state["run_id"], node="plan"):
            role = self.config.agents.agents["planner"]
            plan, response = await self._structured_call(
                state=state,
                node_name="plan",
                agent_role="planner",
                prompt_name="planner",
                logical_model=role.logical_model,
                operation="plan",
                payload={"task": state["task"]},
                output_model=Plan,
                token_budget=role.token_budget,
                cost_budget_usd=role.cost_budget_usd,
            )
            if len(plan.subtasks) > self.config.runtime.max_planned_subtasks:
                raise ValueError(
                    f"plan has {len(plan.subtasks)} subtasks; limit is "
                    f"{self.config.runtime.max_planned_subtasks}"
                )
            plan = self._apply_assignment_policy(plan, TaskSpec.model_validate(state["task"]))
            return {
                "plan": plan.model_dump(mode="json"),
                "pending_subtasks": [item.model_dump(mode="json") for item in plan.subtasks],
                "model_calls": [self._model_call("plan", response)],
            }

    async def worker(self, state: GraphState) -> dict[str, Any]:
        async with self.worker_slots:
            async with self.trace.span(run_id=state["run_id"], node="worker"):
                subtask = SubtaskSpec.model_validate(state["subtask"])
                role = self.config.agents.agents.get(subtask.worker_role)
                if role is None or not role.assignable:
                    raise ValueError(
                        f"subtask references non-assignable agent: {subtask.worker_role}"
                    )
                artifact, responses, tool_records = await self._worker_call(
                    state=state,
                    subtask=subtask,
                    token_budget=min(subtask.token_budget, role.token_budget),
                    cost_budget_usd=min(subtask.cost_budget_usd, role.cost_budget_usd),
                    max_steps=role.max_steps,
                )
                if len(artifact.content) > self.config.runtime.max_artifact_chars:
                    raise ValueError("artifact exceeds configured size limit")
                return {
                    "artifacts": [artifact.model_dump(mode="json")],
                    "model_calls": [
                        self._model_call("worker", response) for response in responses
                    ],
                    "tool_calls": tool_records,
                }

    def _apply_assignment_policy(self, plan: Plan, task: TaskSpec) -> Plan:
        assignment = AgentAssignment.model_validate(task.assignment)
        assignable = {
            name: role for name, role in self.config.agents.agents.items() if role.assignable
        }
        unknown = set(assignment.assignees) - set(assignable)
        if unknown:
            raise ValueError(f"task assignment references unknown agents: {sorted(unknown)}")

        selected = assignment.assignees or list(assignable)
        if not selected:
            raise ValueError("no assignable agents are configured")

        rewritten: list[SubtaskSpec] = []
        loads: dict[str, int] = {name: 0 for name in selected}
        for index, subtask in enumerate(plan.subtasks):
            needed_function = self._infer_function(subtask)
            if assignment.mode == AllocationMode.AUTO:
                owner = (
                    self._functional_owner(
                        needed_function,
                        selected,
                        assignment.functions_by_agent,
                        loads,
                    )
                    if assignment.functions_by_agent
                    else min(
                        selected,
                        key=lambda name: (loads[name], selected.index(name)),
                    )
                )
            elif assignment.mode == AllocationMode.DIRECT:
                owner = selected[index % len(selected)]
            else:
                owner = self._functional_owner(
                    needed_function,
                    selected,
                    assignment.functions_by_agent,
                    loads,
                )
            loads[owner] = loads.get(owner, 0) + 1
            role = assignable[owner]
            allowed_tools = [item for item in subtask.allowed_tools if item in role.allowed_tools]
            selected_functions = assignment.functions_by_agent.get(owner, [])
            assigned_function = (
                needed_function
                if not selected_functions or needed_function in selected_functions
                else selected_functions[0]
            )
            rewritten.append(
                subtask.model_copy(
                    update={
                        "worker_role": owner,
                        "assigned_function": assigned_function,
                        "logical_model": role.logical_model,
                        "allowed_tools": allowed_tools,
                    }
                )
            )
        return plan.model_copy(update={"subtasks": rewritten})

    @staticmethod
    def _functional_owner(
        needed_function: AgentFunction,
        assignees: list[str],
        functions_by_agent: dict[str, list[AgentFunction]],
        loads: dict[str, int],
    ) -> str:
        matching = [
            name
            for name in assignees
            if not functions_by_agent.get(name)
            or needed_function in functions_by_agent[name]
        ]
        candidates = matching or assignees
        return min(
            candidates,
            key=lambda name: (loads[name], assignees.index(name)),
        )

    @staticmethod
    def _infer_function(subtask: SubtaskSpec) -> AgentFunction:
        title = subtask.title.casefold()
        description = subtask.description.casefold()
        markers = {
            AgentFunction.ANALYSIS: ("分析", "需求", "假设", "比较", "compare", "analy"),
            AgentFunction.RESEARCH: (
                "研究",
                "证据",
                "来源",
                "调研",
                "research",
                "evidence",
                "source",
            ),
            AgentFunction.EXECUTION: (
                "交付",
                "实现",
                "方案",
                "执行",
                "清单",
                "draft",
                "deliver",
                "build",
                "create",
            ),
            AgentFunction.RISK: (
                "风险",
                "安全",
                "合规",
                "约束",
                "risk",
                "security",
                "compliance",
            ),
        }
        scores = {
            function: sum(marker in title for marker in function_markers) * 5
            + sum(marker in description for marker in function_markers)
            for function, function_markers in markers.items()
        }
        if not any(scores.values()):
            return AgentFunction.EXECUTION
        return max(scores, key=lambda function: scores[function])

    async def schedule(self, state: GraphState) -> dict[str, Any]:
        async with self.trace.span(run_id=state["run_id"], node="schedule"):
            latest = {item["subtask_id"]: item for item in latest_artifacts(state["artifacts"])}
            pending: list[dict[str, Any]] = []
            failed: list[str] = []
            for item in state.get("pending_subtasks", []):
                artifact = latest.get(item["subtask_id"])
                if artifact and artifact["status"] == ArtifactStatus.SUCCESS:
                    continue
                if artifact and artifact["status"] == ArtifactStatus.FAILED:
                    failed.append(item["subtask_id"])
                pending.append(item)
            if failed:
                error = RunError(
                    code="worker_failed",
                    message=f"workers failed for subtasks: {sorted(failed)}",
                    node="schedule",
                )
                return {
                    "pending_subtasks": pending,
                    "status": RunStatus.FAILED.value,
                    "errors": [error.model_dump(mode="json")],
                }
            return {"pending_subtasks": pending}

    async def validate(self, state: GraphState) -> dict[str, Any]:
        async with self.trace.span(run_id=state["run_id"], node="validate"):
            issues: list[dict[str, Any]] = []
            plan = Plan.model_validate(state["plan"])
            latest = {item["subtask_id"]: item for item in latest_artifacts(state["artifacts"])}
            for subtask in plan.subtasks:
                artifact = latest.get(subtask.subtask_id)
                if artifact is None:
                    issues.append(
                        {
                            "subtask_id": subtask.subtask_id,
                            "code": "missing_artifact",
                            "severity": Severity.ERROR.value,
                            "message": "No artifact exists for planned subtask.",
                        }
                    )
                    continue
                if not artifact.get("content", "").strip():
                    issues.append(
                        {
                            "subtask_id": subtask.subtask_id,
                            "code": "empty_artifact",
                            "severity": Severity.ERROR.value,
                            "message": "Artifact content is empty.",
                        }
                    )
                if artifact["status"] == ArtifactStatus.FAILED:
                    issues.append(
                        {
                            "subtask_id": subtask.subtask_id,
                            "code": "failed_artifact",
                            "severity": Severity.ERROR.value,
                            "message": "Worker marked the artifact as failed.",
                        }
                    )
            return {"validation_issues": issues}

    async def reviewer(self, state: GraphState) -> dict[str, Any]:
        async with self.reviewer_slots:
            async with self.trace.span(run_id=state["run_id"], node="reviewer"):
                job = state["review_job"]
                artifact = Artifact.model_validate(job["artifact"])
                role = self.config.agents.agents["reviewer"]
                review, response = await self._structured_call(
                    state=state,
                    node_name="reviewer",
                    agent_role="reviewer",
                    prompt_name="reviewer",
                    logical_model=role.logical_model,
                    operation="review",
                    payload={
                        "task": state["task"],
                        "artifact": artifact.model_dump(mode="json"),
                        "dimension": job["dimension"],
                    },
                    output_model=Review,
                    token_budget=role.token_budget,
                    cost_budget_usd=role.cost_budget_usd,
                )
                return {
                    "reviews": [review.model_dump(mode="json")],
                    "model_calls": [self._model_call("reviewer", response)],
                }

    async def judge(self, state: GraphState) -> dict[str, Any]:
        async with self.trace.span(run_id=state["run_id"], node="judge"):
            if self._budget_exceeded(state):
                decision = Decision(
                    action=DecisionAction.FAIL,
                    reasons=["Global token or cost budget has been exhausted."],
                )
                return {
                    "decision": decision.model_dump(mode="json"),
                    "status": RunStatus.FAILED.value,
                }
            role = self.config.agents.agents["judge"]
            current_reviews = latest_reviews(state.get("reviews", []), state["artifacts"])
            decision, response = await self._structured_call(
                state=state,
                node_name="judge",
                agent_role="judge",
                prompt_name="judge",
                logical_model=role.logical_model,
                operation="judge",
                payload={
                    "task": state["task"],
                    "artifacts": latest_artifacts(state["artifacts"]),
                    "validation_issues": state.get("validation_issues", []),
                    "reviews": current_reviews,
                    "repair_round": state.get("repair_round", 0),
                },
                output_model=Decision,
                token_budget=role.token_budget,
                cost_budget_usd=role.cost_budget_usd,
            )
            update: dict[str, Any] = {
                "decision": decision.model_dump(mode="json"),
                "model_calls": [self._model_call("judge", response)],
            }
            if decision.action == DecisionAction.REQUEST_HUMAN:
                update["status"] = RunStatus.WAITING_HUMAN.value
            elif decision.action == DecisionAction.FAIL:
                update["status"] = RunStatus.FAILED.value
            return update

    async def repair_plan(self, state: GraphState) -> dict[str, Any]:
        async with self.trace.span(run_id=state["run_id"], node="repair_plan"):
            decision = Decision.model_validate(state["decision"])
            plan = Plan.model_validate(state["plan"])
            targets = set(decision.target_subtasks)
            if not targets:
                targets = {item.subtask_id for item in plan.subtasks}
            selected = [
                item.model_dump(mode="json")
                for item in plan.subtasks
                if item.subtask_id in targets
            ]
            return {
                "pending_subtasks": selected,
                "repair_round": state.get("repair_round", 0) + 1,
                "validation_issues": [],
                "status": RunStatus.RUNNING.value,
            }

    async def human_gate(self, state: GraphState) -> dict[str, Any]:
        from langgraph.types import interrupt

        decision = Decision.model_validate(state["decision"])
        response = interrupt(
            {
                "run_id": state["run_id"],
                "prompt": decision.human_prompt or "This run requires explicit approval.",
                "reasons": decision.reasons,
            }
        )
        approved = isinstance(response, dict) and bool(response.get("approved"))
        if approved:
            next_decision = Decision(
                action=DecisionAction.APPROVE,
                reasons=["Human approval received."],
            )
            return {
                "decision": next_decision.model_dump(mode="json"),
                "status": RunStatus.RUNNING.value,
            }
        next_decision = Decision(
            action=DecisionAction.FAIL,
            reasons=["Human approval was denied."],
        )
        return {
            "decision": next_decision.model_dump(mode="json"),
            "status": RunStatus.FAILED.value,
        }

    async def synthesize(self, state: GraphState) -> dict[str, Any]:
        async with self.trace.span(run_id=state["run_id"], node="synthesize"):
            if self._budget_exceeded(state):
                error = RunError(
                    code="budget_exhausted",
                    message="Global token or cost budget was exhausted before synthesis.",
                    node="synthesize",
                )
                return {
                    "status": RunStatus.FAILED.value,
                    "errors": [error.model_dump(mode="json")],
                }
            role = self.config.agents.agents["synthesizer"]
            prompt = self.prompts.get("synthesizer")
            artifacts = latest_artifacts(state["artifacts"])
            request = ModelRequest(
                run_id=state["run_id"],
                trace_id=f"trace_{uuid4().hex}",
                node_name="synthesize",
                agent_role="synthesizer",
                logical_model=role.logical_model,
                messages=[
                    ModelMessage(role="system", content=prompt.body),
                    ModelMessage(
                        role="user",
                        content=json.dumps(
                            {"task": state["task"], "artifacts": artifacts},
                            ensure_ascii=False,
                            default=str,
                        ),
                    ),
                ],
                token_budget=role.token_budget,
                cost_budget_usd=role.cost_budget_usd,
                risk_level=state["task"]["risk_level"],
                metadata={
                    "operation": "synthesize",
                    "payload": {"task": state["task"], "artifacts": artifacts},
                    "prompt_name": prompt.name,
                    "prompt_version": prompt.version,
                },
            )
            response = await self.gateway.complete(request)
            return {
                "final_output": response.content,
                "status": RunStatus.SUCCEEDED.value,
                "model_calls": [self._model_call("synthesize", response)],
            }

    async def fail(self, state: GraphState) -> dict[str, Any]:
        if state.get("errors"):
            return {"status": RunStatus.FAILED.value}
        decision = state.get("decision", {})
        message = "; ".join(decision.get("reasons", [])) or "workflow failed"
        error = RunError(code="workflow_failed", message=message, node="fail")
        return {
            "status": RunStatus.FAILED.value,
            "errors": [error.model_dump(mode="json")],
        }
