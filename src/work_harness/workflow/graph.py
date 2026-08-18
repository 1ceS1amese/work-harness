from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from work_harness.config.models import AppConfig
from work_harness.domain.enums import DecisionAction, RunStatus
from work_harness.llm.gateway import ModelGateway
from work_harness.observability.trace import TraceRecorder
from work_harness.prompts import PromptRegistry
from work_harness.skills import SkillRegistry
from work_harness.tools.gateway import ToolGateway
from work_harness.workflow.nodes import WorkflowNodes
from work_harness.workflow.state import GraphState, latest_artifacts


def build_root_graph(
    *,
    config: AppConfig,
    gateway: ModelGateway,
    prompts: PromptRegistry,
    trace: TraceRecorder,
    tool_gateway: ToolGateway,
    skills: SkillRegistry,
    checkpointer: Any,
) -> Any:
    nodes = WorkflowNodes(
        config=config,
        gateway=gateway,
        prompts=prompts,
        trace=trace,
        tool_gateway=tool_gateway,
        skills=skills,
    )
    builder = StateGraph(GraphState)
    builder.add_node("normalize", nodes.normalize)
    builder.add_node("plan", nodes.plan)
    builder.add_node("worker", nodes.worker)
    builder.add_node("schedule", nodes.schedule)
    builder.add_node("validate", nodes.validate)
    builder.add_node("reviewer", nodes.reviewer)
    builder.add_node("judge", nodes.judge)
    builder.add_node("repair_plan", nodes.repair_plan)
    builder.add_node("human_gate", nodes.human_gate)
    builder.add_node("synthesize", nodes.synthesize)
    builder.add_node("fail", nodes.fail)

    builder.add_edge(START, "normalize")
    builder.add_edge("normalize", "plan")
    builder.add_conditional_edges("plan", _dispatch_workers)
    builder.add_edge("worker", "schedule")
    builder.add_conditional_edges("schedule", _dispatch_after_schedule)
    builder.add_conditional_edges("validate", _dispatch_reviewers)
    builder.add_edge("reviewer", "judge")
    builder.add_conditional_edges(
        "judge",
        lambda state: _route_decision(state, config.runtime.max_repair_rounds),
        {
            "synthesize": "synthesize",
            "repair_plan": "repair_plan",
            "human_gate": "human_gate",
            "fail": "fail",
        },
    )
    builder.add_conditional_edges("repair_plan", _dispatch_workers)
    builder.add_conditional_edges(
        "human_gate",
        _route_after_human,
        {"synthesize": "synthesize", "fail": "fail"},
    )
    builder.add_edge("synthesize", END)
    builder.add_edge("fail", END)
    return builder.compile(checkpointer=checkpointer)


def _dispatch_workers(state: GraphState) -> list[Send] | str:
    pending = state.get("pending_subtasks", [])
    if not pending:
        return "validate"
    latest = {item["subtask_id"]: item for item in latest_artifacts(state.get("artifacts", []))}
    completed = {
        subtask_id
        for subtask_id, artifact in latest.items()
        if artifact.get("status") == "success"
    }
    ready = [item for item in pending if set(item.get("dependencies", [])) <= completed]
    if not ready:
        return "fail"
    sends: list[Send] = []
    for subtask in ready:
        previous = latest.get(subtask["subtask_id"])
        next_version = int(previous["version"]) + 1 if previous else 1
        sends.append(
            Send(
                "worker",
                {
                    "run_id": state["run_id"],
                    "task": state["task"],
                    "subtask": subtask,
                    "artifact_version": next_version,
                    "repair_round": state.get("repair_round", 0),
                    "model_calls": state.get("model_calls", []),
                },
            )
        )
    return sends


def _dispatch_after_schedule(state: GraphState) -> list[Send] | str:
    if state.get("status") == RunStatus.FAILED:
        return "fail"
    if not state.get("pending_subtasks"):
        return "validate"
    return _dispatch_workers(state)


def _dispatch_reviewers(state: GraphState) -> list[Send] | str:
    artifacts = latest_artifacts(state.get("artifacts", []))
    if not artifacts:
        return "judge"
    cross_review = state.get("task", {}).get("assignment", {}).get("cross_review", True)
    dimensions = ("fact", "logic", "constraint") if cross_review else ("constraint",)
    sends: list[Send] = []
    for artifact in artifacts:
        for dimension in dimensions:
            sends.append(
                Send(
                    "reviewer",
                    {
                        "run_id": state["run_id"],
                        "task": state["task"],
                        "artifacts": state.get("artifacts", []),
                        "model_calls": state.get("model_calls", []),
                        "review_job": {"artifact": artifact, "dimension": dimension},
                    },
                )
            )
    return sends


def _route_decision(state: GraphState, max_repair_rounds: int) -> str:
    action = state["decision"]["action"]
    if action == DecisionAction.APPROVE:
        return "synthesize"
    if action == DecisionAction.REPAIR and state.get("repair_round", 0) < max_repair_rounds:
        return "repair_plan"
    if action == DecisionAction.REQUEST_HUMAN:
        return "human_gate"
    return "fail"


def _route_after_human(state: GraphState) -> str:
    return "synthesize" if state["decision"]["action"] == DecisionAction.APPROVE else "fail"
