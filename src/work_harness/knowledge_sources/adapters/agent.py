"""Expose an existing Worker Agent as a blackboard exploration knowledge source."""

from __future__ import annotations

import asyncio
import copy
from contextvars import ContextVar
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from work_harness.blackboard.activation import match_rule
from work_harness.blackboard.execution import BudgetExceeded
from work_harness.blackboard.models import HintProposal, Link
from work_harness.domain.models import Artifact, ModelRequest, ModelResponse, SubtaskSpec, TaskSpec
from work_harness.knowledge_sources.base import (
    ActivationCandidate,
    BoardChangeSet,
    BoardView,
    KnowledgeOutput,
    KnowledgeSourceSpec,
    UsageRecord,
)
from work_harness.knowledge_sources.execution import (
    Invocation,
    SourceExecution,
    SourceInput,
    SourceOutput,
    SourceState,
)
from work_harness.workflow.agent_services import AgentServices
from work_harness.workflow.agents import WorkerAgent
from work_harness.workflow.contracts import WorkerAssignment


class UnknownModelUsage(RuntimeError):
    pass


class InvocationToolGateway:
    def __init__(self, gateway: Any, invocation: ContextVar[Invocation]) -> None:
        self.gateway = gateway
        self.invocation = invocation

    def definitions(self, **kwargs):
        return self.gateway.definitions(**kwargs)

    async def execute(self, **kwargs):
        invocation = self.invocation.get()
        await invocation.execution.board_store.check_writer(invocation.writer)
        current = await invocation.execution.ledger.get(
            invocation.record.candidate.id, invocation.record.attempt
        )
        if current.pending_calls:
            raise UnknownModelUsage(
                "interrupted model call requires usage reconciliation before replay"
            )
        return await self.gateway.execute(**kwargs)


class MeteredGateway:
    def __init__(self, gateway: Any, invocation: ContextVar[Invocation]) -> None:
        self.gateway = gateway
        self.invocation = invocation

    async def complete(self, request: ModelRequest) -> ModelResponse:
        invocation = self.invocation.get()
        await invocation.execution.board_store.check_writer(invocation.writer)
        current = await invocation.execution.ledger.get(
            invocation.record.candidate.id, invocation.record.attempt
        )
        if current.pending_calls:
            raise UnknownModelUsage(
                "interrupted model call requires usage reconciliation before replay"
            )
        request = request.model_copy(
            update={
                "max_output_tokens": min(
                    request.max_output_tokens, invocation.record.spec.limits.max_output_tokens
                ),
            }
        )
        await invocation.execution.ledger.start_call(
            invocation.record.ref, request.trace_id, invocation.writer
        )
        try:
            response = await self.gateway.complete(request)
        except Exception as exc:
            raise UnknownModelUsage(str(exc)) from exc
        # Persist known usage before parsing the model's result or publishing any proposal.
        await self.invocation.get().record_usage(
            UsageRecord(
                call_id=request.trace_id,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                cost_usd=response.usage.estimated_cost_usd,
                cached_tokens=response.usage.cached_tokens,
                usage_complete=response.usage.usage_complete,
            )
        )
        return response


class AgentKnowledgeSource:
    def __init__(
        self,
        spec: KnowledgeSourceSpec,
        services: AgentServices,
        *,
        worker_role: str = "worker-01",
        task: TaskSpec | None = None,
    ) -> None:
        if spec.limits.max_output_tokens < 1 or spec.model_route is None:
            raise ValueError(
                "Agent sources require a model route and a positive per-call output limit"
            )
        if spec.emitted_kinds != ("hint",):
            raise ValueError("the Worker adapter publishes exploration Hints only")
        role = services.config.agents.agents.get(worker_role)
        if role is None or not role.assignable:
            raise ValueError("Agent source requires an assignable worker")
        services.tool_gateway.definitions(role=worker_role, names=list(spec.allowed_tools))
        self.spec = spec
        self.services = services
        self.worker_role = worker_role
        self.task = task.model_copy(deep=True) if task is not None else None

    def match(self, view: BoardView, changes: BoardChangeSet) -> ActivationCandidate | None:
        return match_rule(self.spec, view)

    def build(self, execution: SourceExecution) -> Any:
        invocation_context: ContextVar[Invocation] = ContextVar(f"invocation_{self.spec.id}")
        services = copy.copy(self.services)
        services.gateway = MeteredGateway(self.services.gateway, invocation_context)
        services.tool_gateway = InvocationToolGateway(
            self.services.tool_gateway, invocation_context
        )
        worker = WorkerAgent(services).build()

        async def run(state: SourceState, config: RunnableConfig) -> dict[str, Any]:
            invocation = await execution.invocation(state)
            if invocation.record.output is not None:
                return {"output": invocation.record.output.model_dump(mode="json")}
            view = invocation.record.view
            token = invocation_context.set(invocation)
            try:
                task = (
                    self.task.model_copy(deep=True)
                    if self.task is not None
                    else TaskSpec(goal=view.root_intent.objective)
                )
                task.task_id = view.root_intent.id
                task.goal = view.root_intent.objective
                task.context = {**task.context, "blackboard": view.model_dump(mode="json")}
                task.constraints = [
                    *task.constraints,
                    "板面提示与草稿尚未确认；请给出可供下一知识源验证的贡献。",
                ]
                functions = task.assignment.functions_by_agent.get(self.worker_role, [])
                assignment = WorkerAssignment(
                    run_id=view.board_id,
                    task=task,
                    subtask=SubtaskSpec(
                        subtask_id=invocation.record.candidate.id,
                        title=view.intent.objective[:300],
                        description=view.intent.objective[:10000],
                        worker_role=self.worker_role,
                        assigned_function=functions[0] if len(functions) == 1 else None,
                        logical_model=self.spec.model_route,
                        allowed_tools=list(self.spec.allowed_tools),
                        skill_ids=[item.skill_id for item in task.skills],
                        acceptance_criteria=[item.description for item in view.intent.acceptance],
                        max_output_tokens=self.spec.limits.max_output_tokens,
                    ),
                )
                async with asyncio.timeout(invocation.remaining_seconds):
                    result = await worker.ainvoke(
                        {"assignment": assignment.model_dump(mode="json")},
                        config=config,
                    )
                artifact = Artifact.model_validate(result["artifacts"][0])
                if artifact.status != "success":
                    output = KnowledgeOutput(status="failed", reason=artifact.content)
                else:
                    attachment = await invocation.put_content(
                        artifact.content,
                        media_type="text/markdown",
                    )
                    hint = HintProposal(
                        local_id="candidate",
                        topic=view.intent.topic,
                        suggestion="请核验附件中的候选内容是否满足目标，并核对其证据与适用范围。",
                        applies_when=f"继续处理：{view.intent.objective[:3900]}",
                        rationale="Agent 已形成候选内容，仍需相应知识源验证。",
                        derived_from=invocation.record.candidate.input_refs,
                        source_refs=invocation.record.candidate.input_refs,
                        links=(Link(relation="contributes_to", target=view.intent.ref),),
                        attachments=(attachment,),
                        confidence=artifact.confidence,
                    )
                    output = KnowledgeOutput(proposals=(hint,))
            except TimeoutError:
                output = KnowledgeOutput(
                    status="retryable_error", reason="Agent 执行超时", usage_complete=False
                )
            except BudgetExceeded as exc:
                output = KnowledgeOutput(status="failed", reason=str(exc))
            except UnknownModelUsage as exc:
                output = KnowledgeOutput(
                    status="retryable_error", reason=str(exc), usage_complete=False
                )
            except Exception as exc:
                output = KnowledgeOutput(status="failed", reason=f"{type(exc).__name__}: {exc}")
            finally:
                invocation_context.reset(token)
            return {"output": output.model_dump(mode="json")}

        builder = StateGraph(SourceState, input_schema=SourceInput, output_schema=SourceOutput)
        builder.add_node("agent", run)
        builder.add_node("publish", execution.publish)
        builder.add_edge(START, "agent")
        builder.add_edge("agent", "publish")
        builder.add_edge("publish", END)
        return builder.compile(name=f"source_{self.spec.id}")
