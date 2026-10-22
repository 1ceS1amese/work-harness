"""Adapters for rules and evidence-backed observations or verification."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.graph import END, START, StateGraph
from pydantic import JsonValue

from work_harness.blackboard.activation import match_rule
from work_harness.blackboard.models import BoardModel, FactContent, FactProposal
from work_harness.knowledge_sources.base import (
    ActivationCandidate,
    BoardChangeSet,
    BoardView,
    KnowledgeOutput,
    KnowledgeSourceSpec,
)
from work_harness.knowledge_sources.execution import (
    Invocation,
    SourceExecution,
    SourceInput,
    SourceOutput,
    SourceState,
)

Execute = Callable[[Invocation, BoardView], Awaitable[KnowledgeOutput]]


class RuleKnowledgeSource:
    def __init__(self, spec: KnowledgeSourceSpec, execute: Execute) -> None:
        self.spec = spec
        self.execute = execute

    def match(self, view: BoardView, changes: BoardChangeSet) -> ActivationCandidate | None:
        return match_rule(self.spec, view)

    def build(self, execution: SourceExecution) -> Any:
        async def run(state: SourceState) -> dict[str, Any]:
            invocation = await execution.invocation(state)
            if invocation.record.output is not None:
                return {"output": invocation.record.output.model_dump(mode="json")}
            try:
                async with asyncio.timeout(invocation.remaining_seconds):
                    output = await self.execute(invocation, invocation.record.view)
                output = KnowledgeOutput.model_validate(output.model_dump())
            except TimeoutError:
                output = KnowledgeOutput(
                    status="retryable_error", reason="知识源执行超时", usage_complete=False
                )
            except Exception as exc:
                output = KnowledgeOutput(status="failed", reason=f"{type(exc).__name__}: {exc}")
            return {"output": output.model_dump(mode="json")}

        builder = StateGraph(SourceState, input_schema=SourceInput, output_schema=SourceOutput)
        builder.add_node("execute", run)
        builder.add_node("publish", execution.publish)
        builder.add_edge(START, "execute")
        builder.add_edge("execute", "publish")
        builder.add_edge("publish", END)
        return builder.compile(name=f"source_{self.spec.id}")


class VerifiedObservation(BoardModel):
    local_id: str
    content: FactContent
    verifier_id: str
    proof: JsonValue


Observe = Callable[[Invocation, BoardView], Awaitable[tuple[VerifiedObservation, ...]]]


class ObservationKnowledgeSource(RuleKnowledgeSource):
    def __init__(self, spec: KnowledgeSourceSpec, observe: Observe) -> None:
        async def execute(invocation: Invocation, view: BoardView) -> KnowledgeOutput:
            observations = await observe(invocation, view)
            proposals: list[FactProposal] = []
            for observation in observations:
                receipt = await invocation.execution.verification.attest(
                    view.board_id,
                    observation.content,
                    verifier_id=observation.verifier_id,
                    evidence=observation.proof,
                )
                proposals.append(
                    FactProposal(
                        **observation.content.model_dump(),
                        local_id=observation.local_id,
                        verification_receipt_id=receipt.receipt_id,
                    )
                )
            return KnowledgeOutput(
                proposals=tuple(proposals), status="completed" if proposals else "no_progress"
            )

        super().__init__(spec, execute)


class VerificationKnowledgeSource(ObservationKnowledgeSource):
    """A verification adapter uses its registered evidence policy, never an Agent's confidence."""
