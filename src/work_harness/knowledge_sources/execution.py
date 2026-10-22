"""Invocation services and the common private result publishing boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TypedDict

from work_harness.blackboard.execution import ActivationRecord, ActivationRef, ActivationStore
from work_harness.blackboard.kernel import BlackboardKernel
from work_harness.blackboard.protocol import BoardClosedError, StaleWriterError, WriterToken
from work_harness.blackboard.store import BlackboardStore
from work_harness.blackboard.verification import VerificationRegistry
from work_harness.knowledge_sources.base import KnowledgeOutput, UsageRecord


class SourceInput(TypedDict):
    activation_id: str
    activation_attempt: int


class SourceOutput(TypedDict):
    completed_activations: list[dict[str, Any]]


class SourceState(SourceInput, SourceOutput, total=False):
    output: dict[str, Any]


@dataclass(frozen=True)
class SourceExecution:
    ledger: ActivationStore
    kernel: BlackboardKernel
    _writers: dict[str, WriterToken] = field(default_factory=dict)

    @property
    def board_store(self) -> BlackboardStore:
        return self.kernel.store

    @property
    def verification(self) -> VerificationRegistry:
        return self.kernel.verification

    async def writer(self, board_id: str, *, takeover: bool = False) -> WriterToken:
        # This execution instance never borrows a later generation from the mutable kernel cache.
        if takeover and board_id in self._writers:
            raise StaleWriterError("takeover requires a new source execution instance")
        if board_id not in self._writers:
            self._writers[board_id] = await self.kernel.writer(board_id, takeover=takeover)
        writer = self._writers[board_id]
        await self.board_store.check_writer(writer)
        return writer

    async def invocation(self, state: SourceInput) -> Invocation:
        ref = ActivationRef(
            activation_id=state["activation_id"], attempt=state["activation_attempt"]
        )
        record = await self.ledger.get(ref.activation_id, ref.attempt)
        writer = await self.writer(record.candidate.board_id)
        return Invocation(await self.ledger.begin(ref, writer), self, writer)

    async def publish(self, state: SourceState) -> SourceOutput:
        ref = ActivationRef(
            activation_id=state["activation_id"], attempt=state["activation_attempt"]
        )
        record = await self.ledger.get(ref.activation_id, ref.attempt)
        output = KnowledgeOutput.model_validate(state["output"])
        try:
            writer = await self.writer(record.candidate.board_id)
            await self.ledger.save_output(
                ref.activation_id, ref.attempt, output, writer=writer, now=self.kernel.clock()
            )
        except BoardClosedError:
            await self.ledger.stop(ref, reason="board_closed", retry=False, now=self.kernel.clock())
        return {"completed_activations": [ref.model_dump()]}


@dataclass(frozen=True)
class Invocation:
    record: ActivationRecord
    execution: SourceExecution
    writer: WriterToken

    @property
    def remaining_seconds(self) -> float:
        return max(0, (self.record.deadline - self.execution.kernel.clock()).total_seconds())

    async def record_usage(self, usage: UsageRecord) -> None:
        await self.execution.ledger.record_usage(
            self.record.candidate.id,
            self.record.attempt,
            usage,
        )

    async def put_content(self, body: str, *, media_type: str):
        return await self.execution.kernel.put_content(
            self.record.candidate.board_id, body, media_type=media_type, writer=self.writer
        )
