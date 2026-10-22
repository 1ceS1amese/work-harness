"""Per-invocation LangGraph Agents with private model/tool conversation state."""

from __future__ import annotations

import json
from typing import Any, TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from work_harness.domain.models import (
    Artifact,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    Review,
    RunError,
    ToolCallRecord,
)
from work_harness.workflow.agent_services import AgentServices
from work_harness.workflow.contracts import (
    ReviewAssignment,
    WorkerAssignment,
    interaction,
    stable_id,
)


class AgentInput(TypedDict):
    assignment: dict[str, Any]


class AgentOutput(TypedDict, total=False):
    artifacts: list[dict[str, Any]]
    reviews: list[dict[str, Any]]
    model_calls: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    errors: list[dict[str, Any]]
    agent_interactions: list[dict[str, Any]]


class WorkerState(AgentInput, AgentOutput, total=False):
    messages: list[dict[str, Any]]
    response: dict[str, Any]
    tool_index: int
    steps: int
    failure: str


class WorkerAgent:
    def __init__(self, services: AgentServices) -> None:
        self.services = services

    def build(self) -> Any:
        builder = StateGraph(WorkerState, input_schema=AgentInput, output_schema=AgentOutput)
        builder.add_node("prepare", self.prepare)
        builder.add_node("model", self.model)
        builder.add_node("tools", self.tools)
        builder.add_node("publish", self.publish)
        builder.add_edge(START, "prepare")
        builder.add_edge("prepare", "model")
        builder.add_conditional_edges("model", self.after_model, ["tools", "publish"])
        builder.add_conditional_edges("tools", self.after_tools, ["tools", "model", "publish"])
        builder.add_edge("publish", END)
        # Inherit the root checkpointer, with an isolated namespace per Send.
        return builder.compile(name="worker_agent")

    def prepare(self, state: WorkerState) -> dict[str, Any]:
        job = WorkerAssignment.model_validate(state["assignment"])
        services = self.services
        role = services.config.agents.agents.get(job.subtask.worker_role)
        if role is None or not role.assignable:
            raise ValueError(f"non-assignable agent: {job.subtask.worker_role}")
        skill = services.skills.resolve(
            job.task.skills,
            job.subtask.skill_ids,
            function=job.subtask.assigned_function,
        )
        prompt = services.prompts.get("worker")
        system = prompt.body
        if skill.instructions:
            system = f"{system}\n\n{skill.instructions}"
        return {
            "messages": [
                ModelMessage(role="system", content=system).model_dump(mode="json"),
                ModelMessage(
                    role="user",
                    content=json.dumps(self.payload(job), ensure_ascii=False),
                ).model_dump(mode="json"),
            ],
            "steps": 0,
            "model_calls": [],
            "tool_calls": [],
            "failure": "",
        }

    @staticmethod
    def payload(job: WorkerAssignment) -> dict[str, Any]:
        payload = job.model_dump(mode="json", exclude={"run_id", "artifact_version"})
        payload["version"] = job.artifact_version
        return payload

    async def model(self, state: WorkerState) -> dict[str, Any]:
        services = self.services
        job = WorkerAssignment.model_validate(state["assignment"])
        subtask = job.subtask
        role = services.config.agents.agents[subtask.worker_role]
        max_steps = min(role.max_steps, services.config.runtime.max_node_steps)
        if state["steps"] >= max_steps:
            return {"failure": "worker exceeded its configured model/tool step limit"}
        max_output_tokens = min(subtask.max_output_tokens, role.max_output_tokens)
        skill = services.skills.resolve(
            job.task.skills,
            subtask.skill_ids,
            function=subtask.assigned_function,
        )
        tools = services.tool_gateway.definitions(
            role=subtask.worker_role, names=subtask.allowed_tools
        )
        prompt = services.prompts.get("worker")
        request = ModelRequest(
            run_id=job.run_id,
            trace_id=f"trace_{uuid4().hex}",
            node_name="worker",
            agent_role=subtask.worker_role,
            logical_model=subtask.logical_model,
            messages=[ModelMessage.model_validate(item) for item in state["messages"]],
            required_capabilities={"chat", "structured_output"}
            | set(skill.required_capabilities)
            | ({"tool_calling"} if tools else set()),
            provider_preferences=list(skill.provider_preferences),
            provider_allowlist=set(skill.provider_allowlist),
            tools=tools,
            response_schema=Artifact.model_json_schema(),
            timeout_seconds=min(role.timeout_seconds, services.config.runtime.node_timeout_seconds),
            max_output_tokens=max_output_tokens,
            risk_level=job.task.risk_level,
            metadata={
                "operation": "worker",
                "payload": self.payload(job),
                "prompt_name": prompt.name,
                "prompt_version": prompt.version,
                "tool_round": len(state["tool_calls"]),
                "skill_ids": list(skill.skill_ids),
            },
        )
        async with services.worker_slots:
            async with services.trace.span(
                run_id=job.run_id,
                node="worker.model",
                agent_role=subtask.worker_role,
            ):
                response = await services.gateway.complete(request)
        calls = state["model_calls"] + [
            services.model_call("worker", response, skill_ids=tuple(subtask.skill_ids)),
        ]
        update: dict[str, Any] = {
            "response": response.model_dump(mode="json"),
            "model_calls": calls,
            "steps": state["steps"] + 1,
            "tool_index": 0,
        }
        if response.tool_calls:
            if not tools:
                update["failure"] = "worker requested a tool that was not advertised"
            elif len(state["tool_calls"]) + len(response.tool_calls) > max_steps:
                update["failure"] = "worker exceeded its configured tool call limit"
            else:
                update["messages"] = state["messages"] + [
                    ModelMessage(
                        role="assistant",
                        content=response.content,
                        tool_calls=response.tool_calls,
                        reasoning_content=response.reasoning_content,
                        response_items=response.response_items,
                    ).model_dump(mode="json")
                ]
        return update

    @staticmethod
    def after_model(state: WorkerState) -> str:
        if state.get("failure") or not state["response"].get("tool_calls"):
            return "publish"
        return "tools"

    async def tools(self, state: WorkerState) -> dict[str, Any]:
        services = self.services
        job = WorkerAssignment.model_validate(state["assignment"])
        raw = state["response"]["tool_calls"][state["tool_index"]]
        call_id, name, arguments = services.parse_tool_call(raw)
        if name not in job.subtask.allowed_tools:
            return {"failure": f"worker requested a disallowed tool: {name}"}
        if any(call["call_id"] == call_id for call in state["tool_calls"]):
            return {"failure": "worker reused a tool call identifier"}
        async with services.worker_slots:
            async with services.trace.span(run_id=job.run_id, node="worker.tools"):
                execution = await services.tool_gateway.execute(
                    run_id=job.run_id,
                    role=job.subtask.worker_role,
                    tool_name=name,
                    arguments=arguments,
                    call_id=call_id,
                )
        record = ToolCallRecord(
            node="worker",
            role=job.subtask.worker_role,
            tool_name=name,
            call_id=execution.call_id,
            input_fingerprint=services.fingerprint(arguments),
            output_summary=execution.output_summary,
            truncated=execution.truncated,
            duration_ms=execution.duration_ms,
            cache_hit=execution.cache_hit,
        )
        return {
            "tool_index": state["tool_index"] + 1,
            "tool_calls": state["tool_calls"] + [record.model_dump(mode="json")],
            "messages": state["messages"]
            + [
                ModelMessage(
                    role="tool",
                    tool_call_id=execution.call_id,
                    content=execution.output_summary,
                ).model_dump(mode="json")
            ],
        }

    @staticmethod
    def after_tools(state: WorkerState) -> str:
        if state.get("failure"):
            return "publish"
        if state["tool_index"] < len(state["response"]["tool_calls"]):
            return "tools"
        return "model"

    def publish(self, state: WorkerState) -> dict[str, Any]:
        job = WorkerAssignment.model_validate(state["assignment"])
        failure = state.get("failure")
        if failure:
            content: dict[str, Any] = {"content": failure, "confidence": 0, "status": "failed"}
        else:
            response = ModelResponse.model_validate(state["response"])
            if response.structured_output is None:
                raise ValueError("worker did not return structured output")
            content = response.structured_output
        artifact = Artifact.model_validate(
            {
                **content,
                "artifact_id": (
                    job.previous_artifact.artifact_id
                    if job.previous_artifact
                    else stable_id("artifact", job.run_id, job.subtask.subtask_id)
                ),
                "run_id": job.run_id,
                "subtask_id": job.subtask.subtask_id,
                "producer": job.subtask.worker_role,
                "version": job.artifact_version,
            }
        )
        if len(artifact.content) > self.services.config.runtime.max_artifact_chars:
            raise ValueError("artifact exceeds configured size limit")
        common = {
            "run_id": job.run_id,
            "subtask_id": job.subtask.subtask_id,
            "artifact_version": job.artifact_version,
            "repair_round": job.repair_round,
        }
        exchanges = [
            interaction(
                **common,
                kind="repair" if job.previous_artifact else "assignment",
                sender="coordinator",
                recipient=job.subtask.worker_role,
                artifact_id=job.previous_artifact.artifact_id if job.previous_artifact else None,
            )
        ]
        for review in job.feedback:
            exchanges.append(
                interaction(
                    **{**common, "artifact_version": review.artifact_version},
                    kind="repair",
                    sender=review.reviewer,
                    recipient=job.subtask.worker_role,
                    artifact_id=review.artifact_id,
                    review_id=review.review_id,
                )
            )
        for dependency in job.dependency_artifacts:
            exchanges.append(
                interaction(
                    **{**common, "artifact_version": dependency.version},
                    kind="dependency",
                    sender=dependency.producer,
                    recipient=job.subtask.worker_role,
                    artifact_id=dependency.artifact_id,
                )
            )
        exchanges.append(
            interaction(
                **common,
                kind="result",
                sender=job.subtask.worker_role,
                recipient="coordinator",
                artifact_id=artifact.artifact_id,
            )
        )
        return {
            "artifacts": [artifact.model_dump(mode="json")],
            "agent_interactions": exchanges,
            "errors": [
                RunError(code="agent_limit", message=failure, node="worker").model_dump(
                    mode="json",
                )
            ]
            if failure
            else [],
        }


class ReviewState(AgentInput, AgentOutput, total=False):
    review: dict[str, Any]


class ReviewAgent:
    def __init__(self, services: AgentServices) -> None:
        self.services = services

    def build(self) -> Any:
        builder = StateGraph(ReviewState, input_schema=AgentInput, output_schema=AgentOutput)
        builder.add_node("review", self.review)
        builder.add_node("publish", self.publish)
        builder.add_edge(START, "review")
        builder.add_edge("review", "publish")
        builder.add_edge("publish", END)
        return builder.compile(name="review_agent")

    async def review(self, state: ReviewState) -> dict[str, Any]:
        services = self.services
        job = ReviewAssignment.model_validate(state["assignment"])
        role = services.config.agents.agents["reviewer"]
        async with services.reviewer_slots:
            async with services.trace.span(run_id=job.run_id, node="reviewer.review"):
                review, response = await services.structured_call(
                    state={"run_id": job.run_id, "task": job.task.model_dump(mode="json")},
                    node_name="reviewer",
                    agent_role="reviewer",
                    prompt_name="reviewer",
                    logical_model=role.logical_model,
                    operation="review",
                    payload=job.model_dump(mode="json"),
                    output_model=Review,
                    max_output_tokens=role.max_output_tokens,
                )
        review = review.model_copy(
            update={
                "review_id": stable_id(
                    "review",
                    job.run_id,
                    job.artifact.artifact_id,
                    job.artifact.version,
                    job.dimension,
                ),
                "artifact_id": job.artifact.artifact_id,
                "artifact_version": job.artifact.version,
                "reviewer": "reviewer",
                "dimension": job.dimension,
            }
        )
        return {
            "review": review.model_dump(mode="json"),
            "model_calls": [services.model_call("reviewer", response)],
        }

    @staticmethod
    def publish(state: ReviewState) -> dict[str, Any]:
        job = ReviewAssignment.model_validate(state["assignment"])
        return {
            "reviews": [state["review"]],
            "agent_interactions": [
                interaction(
                    run_id=job.run_id,
                    kind="review",
                    sender="reviewer",
                    recipient="coordinator",
                    subtask_id=job.subtask.subtask_id,
                    artifact_id=job.artifact.artifact_id,
                    artifact_version=job.artifact.version,
                    review_id=state["review"]["review_id"],
                    repair_round=job.repair_round,
                )
            ],
        }
