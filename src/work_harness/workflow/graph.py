from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from work_harness.config.models import AppConfig
from work_harness.llm.gateway import ModelGateway
from work_harness.observability.trace import TraceRecorder
from work_harness.prompts import PromptRegistry
from work_harness.skills import SkillRegistry
from work_harness.tools.gateway import ToolGateway
from work_harness.workflow.agents import ReviewAgent, WorkerAgent
from work_harness.workflow.nodes import WorkflowNodes
from work_harness.workflow.routing import (
    _dispatch_after_schedule,
    _dispatch_reviewers,
    _dispatch_workers,
    _route_after_human,
    _route_decision,
)
from work_harness.workflow.state import GraphState


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
    builder.add_node("worker", WorkerAgent(nodes).build())
    builder.add_node("schedule", nodes.schedule)
    builder.add_node("validate", nodes.validate)
    builder.add_node("reviewer", ReviewAgent(nodes).build())
    builder.add_node("judge", nodes.judge)
    builder.add_node("repair_plan", nodes.repair_plan)
    builder.add_node("human_gate", nodes.human_gate)
    builder.add_node("synthesize", nodes.synthesize)
    builder.add_node("fail", nodes.fail)

    builder.add_edge(START, "normalize")
    builder.add_edge("normalize", "plan")
    builder.add_conditional_edges("plan", _dispatch_workers, ["worker", "validate", "fail"])
    builder.add_edge("worker", "schedule")
    builder.add_conditional_edges(
        "schedule", _dispatch_after_schedule, ["worker", "validate", "fail"],
    )
    builder.add_conditional_edges(
        "validate",
        lambda state: _dispatch_reviewers(state, config.runtime),
        ["reviewer", "judge"],
    )
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
    builder.add_conditional_edges("repair_plan", _dispatch_workers, ["worker", "validate", "fail"])
    builder.add_conditional_edges(
        "human_gate",
        _route_after_human,
        {"synthesize": "synthesize", "fail": "fail"},
    )
    builder.add_edge("synthesize", END)
    builder.add_edge("fail", END)
    return builder.compile(checkpointer=checkpointer)
