"""Stable LangGraph control mechanism; knowledge determines the source sequence."""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from work_harness.blackboard.controller import BlackboardController
from work_harness.blackboard.execution import ActivationRef
from work_harness.blackboard.goals import evaluate
from work_harness.knowledge_sources.base import KnowledgeSource
from work_harness.knowledge_sources.execution import SourceExecution, SourceInput, SourceOutput
from work_harness.workflow.blackboard_state import BlackboardGraphState


def source_node(source: KnowledgeSource, execution: SourceExecution):
    child = source.build(execution)

    async def invoke(state: SourceInput, config: RunnableConfig) -> SourceOutput:
        ref = ActivationRef(
            activation_id=state["activation_id"], attempt=state["activation_attempt"]
        )
        record = await execution.ledger.get(ref.activation_id, ref.attempt)
        completed: SourceOutput = {"completed_activations": [ref.model_dump()]}
        # This wrapper executes before re-entering a saved child checkpoint. An expired
        # sibling is skipped without discarding the other children's private checkpoints.
        if record.status != "running":
            return completed
        board = await execution.board_store.snapshot(record.candidate.board_id)
        closed = board.finalization is not None
        root = board.by_id()[board.root_intent_id]
        goal_done = (
            root.status == "abandoned"
            or evaluate(root, board.by_id(), execution.kernel.clock()).satisfied
        )
        expired = execution.kernel.clock() >= record.deadline
        if closed or goal_done or expired:
            await execution.ledger.stop(
                ref,
                reason="board_closed"
                if closed
                else "goal_resolved"
                if goal_done
                else "attempt_timeout",
                retry=expired and not (closed or goal_done),
                writer=None if closed else await execution.writer(board.board_id),
                now=execution.kernel.clock(),
            )
            return completed
        # Keep the compiled child visible to LangGraph's subgraph discovery and use the
        # inherited per-invocation checkpoint namespace (including for the same source).
        return await child.ainvoke(state, config)

    return invoke


def build_blackboard_graph(controller: BlackboardController, *, checkpointer: Any = None) -> Any:
    controller.registry.freeze()
    builder = StateGraph(BlackboardGraphState)
    builder.add_node("bootstrap", controller.bootstrap)
    builder.add_node("assess", controller.assess)
    builder.add_node("commit", controller.commit)
    builder.add_node("finish", controller.finish)
    builder.add_node("wait", controller.wait)
    builder.add_node("human", controller.human)
    source_ids: list[str] = []
    for source in controller.registry.sources():
        builder.add_node(source.spec.id, source_node(source, controller.execution))
        builder.add_edge(source.spec.id, "commit")
        source_ids.append(source.spec.id)
    builder.add_edge(START, "bootstrap")
    builder.add_edge("bootstrap", "assess")
    builder.add_conditional_edges(
        "assess", controller.dispatch, [*source_ids, "assess", "finish", "wait"]
    )
    builder.add_edge("commit", "assess")
    builder.add_edge("wait", "assess")
    builder.add_edge("human", "assess")
    builder.add_conditional_edges(
        "finish", lambda state: state["route"], {"end": END, "assess": "assess", "human": "human"}
    )
    return builder.compile(checkpointer=checkpointer, name="blackboard_controller")
