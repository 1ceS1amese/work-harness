"""Recovery at the actual SQLite/graph boundaries, with generation and attempt isolation."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager, closing, suppress
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from test_blackboard_execution_storage import LIMITS, Execution, setup

from work_harness.blackboard.controller import BlackboardController
from work_harness.blackboard.execution import RunLimits
from work_harness.blackboard.protocol import BlackboardError, StaleWriterError
from work_harness.domain.models import ModelResponse, Usage
from work_harness.knowledge_sources.adapters.agent import AgentKnowledgeSource
from work_harness.knowledge_sources.adapters.callback import RuleKnowledgeSource
from work_harness.knowledge_sources.base import (
    ActivationRule,
    KnowledgeOutput,
    KnowledgeSourceSpec,
    SourceLimits,
    UsageRecord,
)
from work_harness.knowledge_sources.registry import KnowledgeSourceRegistry
from work_harness.llm.mock import MockProvider
from work_harness.observability.trace import TraceRecorder
from work_harness.runtime import build_runtime
from work_harness.storage.blackboard_sqlite import SQLiteBlackboardStore
from work_harness.tools.gateway import ToolGateway
from work_harness.workflow.agent_services import AgentServices
from work_harness.workflow.agents import WorkerAgent
from work_harness.workflow.blackboard_runner import BlackboardRunner

CONFIG = {"configurable": {"thread_id": "board"}, "recursion_limit": 100}
ROOT = Path(__file__).resolve().parents[1]


class Crash(BaseException):
    """Bypass business-error handling just as process loss does."""


@dataclass
class Harness:
    execution: Execution
    controller: BlackboardController
    runner: BlackboardRunner


@asynccontextmanager
async def harness(path: Path, calls: list, *, create=False, work=None, checkpoint=True, now=None):
    store = SQLiteBlackboardStore(path / "board.sqlite")
    await store.initialize()
    try:
        execution = await setup(store, create=create)
        if now is not None:
            execution.kernel.clock = lambda: now
        spec = KnowledgeSourceSpec(
            id="observe",
            version="1",
            emitted_kinds=("fact", "hint"),
            activation_rule=ActivationRule(reason="Verify the answer"),
            adapter_type="observation",
            limits=SourceLimits(
                token_budget=100, cost_budget_usd=0.1, max_attempts=2, timeout_seconds=30
            ),
        )

        async def execute(invocation, view):
            calls.append((invocation.record.candidate.id, invocation.record.attempt))
            await invocation.record_usage(UsageRecord(call_id="observation", input_tokens=7))
            if work is not None:
                await work(invocation, view)
            return KnowledgeOutput(proposals=(await execution.fact(invocation.record),))

        registry = KnowledgeSourceRegistry()
        registry.register(RuleKnowledgeSource(spec, execute))
        controller = BlackboardController(
            execution.kernel, registry, ledger=execution.ledger, limits=LIMITS
        )
        if checkpoint:
            async with AsyncSqliteSaver.from_conn_string(str(path / "checkpoints.sqlite")) as saver:
                runner = BlackboardRunner(controller, checkpointer=saver)
                yield Harness(execution, controller, runner)
        else:
            yield Harness(execution, controller, BlackboardRunner(controller))
    finally:
        await store.close()


@pytest.mark.parametrize("window", ("reservation", "private_output", "board_commit"))
async def test_reopen_reconciles_each_crash_window_without_repeating_work(
    tmp_path, monkeypatch, window
):
    calls = []
    async with harness(tmp_path, calls, create=True) as first:
        target, method = {
            "reservation": (first.execution.ledger, "reserve_batch"),
            "private_output": (first.execution.ledger, "save_output"),
            "board_commit": (first.execution.kernel, "commit"),
        }[window]
        original = getattr(target, method)

        async def crash_after(*args, **kwargs):
            await original(*args, **kwargs)
            raise Crash(window)

        monkeypatch.setattr(target, method, crash_after)
        with pytest.raises(Crash):
            await first.runner.run("board", CONFIG)
        run = await first.execution.ledger.run("board")
        before = (await first.execution.ledger.records("board"))[0]
    async with harness(tmp_path, calls) as second:
        result = await second.runner.run("board", CONFIG)
        record = (await second.execution.ledger.records("board"))[0]
        assert result["status"] == "succeeded"
        assert calls == [(record.candidate.id, 1)]
        assert record.status == "completed" and record.attempt == 1
        assert record.deadline == before.deadline
        assert (await second.execution.ledger.run("board")).deadline == run.deadline
        assert (await second.execution.ledger.summary("board")).tokens == 7
        assert (await second.execution.ledger.summary("board")).no_progress == 0
        events = await second.execution.store.events("board")
        assert len(events) == 3
        assert sum(event.activation_id == record.candidate.id for event in events) == 1
        final = await second.execution.store.snapshot("board")
        assert result["finalized_at_revision"] == final.revision
        repeated = await second.runner.run("board", CONFIG)
        for key in ("status", "final_output", "finalized_at_revision", "stop_reason"):
            assert repeated[key] == result[key]
        assert await second.execution.store.snapshot("board") == final


async def test_sealed_board_reconciles_receipt_without_reopening_or_executing(
    tmp_path, monkeypatch
):
    calls = []
    async with harness(tmp_path, calls, create=True) as first:
        original = first.execution.kernel.commit

        async def legacy_commit_then_seal(*args, **kwargs):
            # Simulate the historical two-transaction window. New commits settle atomically.
            kwargs.pop("settlement", None)
            await original(*args, **kwargs)
            await first.execution.kernel.finalize(
                "board", status="succeeded", reason="goal_satisfied"
            )
            raise Crash()

        monkeypatch.setattr(first.execution.kernel, "commit", legacy_commit_then_seal)
        with pytest.raises(Crash):
            await first.runner.run("board", CONFIG)
        final = await first.execution.store.snapshot("board")
        assert (await first.execution.ledger.records("board"))[0].status == "result_ready"
    async with harness(tmp_path, calls) as second:
        result = await second.runner.run("board", CONFIG)
        assert result["status"] == "succeeded"
        assert (await second.execution.ledger.records("board"))[0].status == "completed"
        assert await second.execution.store.snapshot("board") == final
        assert len(calls) == 1


async def test_missing_checkpoint_retries_with_a_new_attempt_and_retains_unknown_usage(tmp_path):
    calls = []

    async def crash(invocation, view):
        raise Crash()

    async with harness(tmp_path, calls, create=True, checkpoint=False, work=crash) as first:
        with pytest.raises(Crash):
            await first.runner.run("board")
        deadline = (await first.execution.ledger.run("board")).deadline
    async with harness(tmp_path, calls, checkpoint=False) as second:
        result = await second.runner.run("board")
        history = await second.execution.ledger.attempts(calls[0][0])
        assert result["status"] == "succeeded"
        assert [attempt for _, attempt in calls] == [1, 2]
        assert history[0].reason == "checkpoint_missing" and not history[0].usage_complete
        assert history[1].status == "completed"
        summary = await second.execution.ledger.summary("board")
        assert summary.tokens == 14 and summary.reserved_tokens == 0
        assert (await second.execution.ledger.run("board")).deadline == deadline


async def test_expired_checkpoint_attempt_is_not_executed_as_the_retry(tmp_path):
    calls = []

    async def crash(invocation, view):
        raise Crash()

    async with harness(tmp_path, calls, create=True, work=crash) as first:
        with pytest.raises(Crash):
            await first.runner.run("board", CONFIG)
        old = (await first.execution.ledger.records("board"))[0]
    async with harness(tmp_path, calls, now=old.deadline + timedelta(seconds=1)) as second:
        result = await second.runner.run("board", CONFIG)
        assert result["status"] == "succeeded"
        assert [attempt for _, attempt in calls] == [1, 2]
        history = await second.execution.ledger.attempts(old.candidate.id)
        assert history[0].reason == "attempt_timeout"
        assert history[1].status == "completed"


async def test_cancelled_checkpoint_never_restarts_its_source(tmp_path):
    calls = []

    async def crash(invocation, view):
        raise Crash()

    async with harness(tmp_path, calls, create=True, work=crash) as first:
        with pytest.raises(Crash):
            await first.runner.run("board", CONFIG)
        await first.execution.kernel.cancel("board")
        final = await first.execution.store.snapshot("board")
    async with harness(tmp_path, calls) as second:
        assert (await second.runner.run("board", CONFIG))["status"] == "cancelled"
        assert len(calls) == 1
        assert (await second.execution.ledger.records("board"))[0].status == "rejected"
        assert (await second.execution.ledger.summary("board")).tokens == 7
        assert await second.execution.store.snapshot("board") == final


async def test_incompatible_source_is_rejected_before_resuming(tmp_path):
    calls = []

    async def crash(invocation, view):
        raise Crash()

    async with harness(tmp_path, calls, create=True, work=crash) as first:
        with pytest.raises(Crash):
            await first.runner.run("board", CONFIG)
        before = await first.execution.ledger.records("board")
    async with harness(tmp_path, calls) as second:
        source = second.controller.registry.get("observe")
        changed = KnowledgeSourceRegistry()
        changed.register(
            RuleKnowledgeSource(source.spec.model_copy(update={"version": "2"}), source.execute)
        )
        controller = BlackboardController(
            second.execution.kernel, changed, ledger=second.execution.ledger, limits=LIMITS
        )
        runner = BlackboardRunner(controller, checkpointer=second.runner.checkpointer)
        control = await second.execution.store.control("board")
        with pytest.raises(BlackboardError, match="changed"):
            await runner.run("board", CONFIG, takeover=True)
        assert await second.execution.ledger.records("board") == before
        assert await second.execution.store.control("board") == control
        assert len(calls) == 1


async def test_old_invocation_cannot_borrow_a_new_writer_or_attempt(tmp_path):
    calls = []
    async with harness(tmp_path, calls, create=True, checkpoint=False) as first:
        await first.controller.bootstrap({"board_id": "board"})
        state = await first.controller.assess({"board_id": "board"})
        source_input = first.controller.dispatch(state)[0].arg
        invocation = await first.controller.execution.invocation(source_input)
        old = invocation.record
        # Reusing the kernel must not update the old SourceExecution's pinned token.
        writer = await first.execution.kernel.writer("board", takeover=True)
        with pytest.raises(StaleWriterError):
            # Bypass the service precheck: the ledger transaction must fence the old writer too.
            await first.execution.ledger.save_output(
                old.candidate.id, old.attempt, KnowledgeOutput(), writer=invocation.writer
            )
        assert (await first.execution.ledger.get(old.candidate.id)).output is None
        await first.execution.ledger.adopt(old.ref, writer, recovering=True)
        await first.execution.ledger.stop(old.ref, reason="interrupted", retry=True)
        new = await first.execution.ledger.reserve(
            old.candidate, old.view, old.spec, LIMITS, writer=writer
        )
        assert new and new.attempt == 2
        with pytest.raises(StaleWriterError):
            await invocation.put_content("late", media_type="text/plain")
        with pytest.raises(StaleWriterError):
            await first.controller.execution.publish(
                {**source_input, "output": KnowledgeOutput().model_dump()}
            )
        await invocation.record_usage(UsageRecord(call_id="late", input_tokens=9))
        assert (await first.execution.ledger.get(old.candidate.id, 2)).output is None
        assert (await first.execution.ledger.usage(old.candidate.id, 1))[0].tokens == 9


async def test_checkpoint_from_another_board_is_rejected(tmp_path):
    calls = []
    async with harness(tmp_path, calls, create=True) as first:
        await first.runner.run("board", CONFIG)
        with pytest.raises(BlackboardError, match="different board"):
            await first.runner.run("another-board", CONFIG)


async def test_another_thread_cannot_silently_restart_a_durable_run(tmp_path):
    calls = []

    async def crash(invocation, view):
        raise Crash()

    async with harness(tmp_path, calls, create=True, work=crash) as first:
        with pytest.raises(Crash):
            await first.runner.run("board", CONFIG)
        before = await first.execution.ledger.records("board")
    async with harness(tmp_path, calls) as second:
        with pytest.raises(BlackboardError, match="another checkpoint thread"):
            await second.runner.run("board", {"configurable": {"thread_id": "wrong-thread"}})
        assert await second.execution.ledger.records("board") == before
        assert len(calls) == 1


async def test_v3_running_record_is_conservatively_recovered_and_upgraded(tmp_path):
    calls = []
    async with harness(tmp_path, calls, create=True, checkpoint=False) as first:
        await first.controller.bootstrap({"board_id": "board"})
        await first.controller.assess({"board_id": "board"})
        deadline = (await first.execution.ledger.run("board")).deadline
    with closing(sqlite3.connect(tmp_path / "board.sqlite")) as connection, connection:
        payload = json.loads(
            connection.execute("SELECT payload FROM board_activation_attempts").fetchone()[0]
        )
        for key in ("writer", "started", "usage_uncertain", "pending_calls"):
            payload.pop(key)
        connection.execute(
            "UPDATE board_activation_attempts SET payload = ?", (json.dumps(payload),)
        )
        payload = json.loads(connection.execute("SELECT payload FROM board_control").fetchone()[0])
        for key in ("workflow_revision", "registry_digest", "checkpoint_thread_id"):
            payload.pop(key)
        connection.execute("UPDATE board_control SET payload = ?", (json.dumps(payload),))
        connection.execute("UPDATE blackboard_schema SET version = 3")
    async with harness(tmp_path, calls, checkpoint=False) as second:
        assert (await second.runner.run("board"))["status"] == "succeeded"
        assert [attempt for _, attempt in calls] == [2]
        history = await second.execution.ledger.attempts(calls[0][0])
        assert history[0].started and history[0].reason == "checkpoint_missing"
        assert (await second.execution.ledger.summary("board")).reserved_tokens == 0
        assert (await second.execution.ledger.run("board")).deadline == deadline
        with closing(sqlite3.connect(tmp_path / "board.sqlite")) as connection:
            assert connection.execute("SELECT version FROM blackboard_schema").fetchone()[0] == 9


async def test_usage_receipt_and_pending_call_clear_are_one_transaction(tmp_path):
    async with harness(tmp_path, [], create=True, checkpoint=False) as current:
        await current.controller.bootstrap({"board_id": "board"})
        state = await current.controller.assess({"board_id": "board"})
        invocation = await current.controller.execution.invocation(
            current.controller.dispatch(state)[0].arg
        )
        ledger = current.execution.ledger
        ref = invocation.record.ref
        await ledger.start_call(ref, "model-call", invocation.writer)
        with closing(sqlite3.connect(tmp_path / "board.sqlite")) as connection, connection:
            connection.execute(
                "CREATE TRIGGER reject_usage BEFORE INSERT ON board_usage "
                "BEGIN SELECT RAISE(ABORT, 'usage failure'); END"
            )
        with pytest.raises(sqlite3.IntegrityError, match="usage failure"):
            await invocation.record_usage(UsageRecord(call_id="model-call", input_tokens=7))
        assert (await ledger.get(ref.activation_id)).pending_calls == ("model-call",)
        assert (await ledger.summary("board")).tokens == 0
        with closing(sqlite3.connect(tmp_path / "board.sqlite")) as connection, connection:
            connection.execute("DROP TRIGGER reject_usage")
        await invocation.record_usage(UsageRecord(call_id="model-call", input_tokens=7))
        assert not (await ledger.get(ref.activation_id)).pending_calls
        assert (await ledger.summary("board")).tokens == 7


@asynccontextmanager
async def agent_harness(path: Path, *, create=False):
    async with harness(path, [], create=create) as base:
        async with build_runtime(
            ROOT,
            checkpoint_backend="memory",
            provider_store_backend="memory",
            run_store_backend="memory",
        ) as runtime:
            services = AgentServices(
                config=runtime.config,
                gateway=runtime.model_gateway,
                prompts=runtime.prompt_registry,
                trace=TraceRecorder(enabled=False),
                tool_gateway=runtime.tool_gateway,
                skills=runtime.skill_registry,
            )
            registry = KnowledgeSourceRegistry()
            registry.register(
                AgentKnowledgeSource(
                    KnowledgeSourceSpec(
                        id="agent",
                        version="1",
                        emitted_kinds=("hint",),
                        adapter_type="agent",
                        activation_rule=ActivationRule(
                            absent_hint_topics=("answer",), reason="Inspect draft"
                        ),
                        model_route="worker-fast",
                        allowed_tools=("echo_read",),
                        limits=SourceLimits(
                            token_budget=1000, cost_budget_usd=0.1, timeout_seconds=30
                        ),
                    ),
                    services,
                )
            )
            source = base.controller.registry.get("observe")
            registry.register(
                RuleKnowledgeSource(
                    source.spec.model_copy(
                        update={
                            "activation_rule": ActivationRule(
                                hint_topics=("answer",), reason="Verify result"
                            ),
                        }
                    ),
                    source.execute,
                )
            )
            controller = BlackboardController(
                base.execution.kernel,
                registry,
                ledger=base.execution.ledger,
                limits=RunLimits(token_budget=5000, timeout_seconds=120, retry_delay_seconds=0),
            )
            yield Harness(
                base.execution,
                controller,
                BlackboardRunner(controller, checkpointer=base.runner.checkpointer),
            )


def nested_values(snapshot) -> list[dict[str, Any]]:
    result = [snapshot.values]
    for task in snapshot.tasks:
        if hasattr(task.state, "values"):
            result.extend(nested_values(task.state))
    return result


@pytest.mark.parametrize("window", ("before_request", "in_flight"))
async def test_sqlite_agent_checkpoint_resumes_without_repeating_completed_tool(
    tmp_path, monkeypatch, window
):
    original_complete = MockProvider.complete
    original_tool = ToolGateway.execute
    original_model = WorkerAgent.model
    reached_model = asyncio.Event()
    block = True
    tool_calls = []
    model_budgets = []

    async def complete(self, request, *, model):
        if request.metadata["operation"] == "worker":
            model_budgets.append(request.max_output_tokens)
            if not request.metadata["tool_round"]:
                return ModelResponse(
                    provider=self.name,
                    model=model,
                    usage=Usage(input_tokens=10, output_tokens=5),
                    tool_calls=[
                        {
                            "id": "read-once",
                            "type": "function",
                            "function": {
                                "name": "echo_read",
                                "arguments": '{"text":"tool-result"}',
                            },
                        }
                    ],
                )
            if block and window == "in_flight":
                reached_model.set()
                await asyncio.Event().wait()
        return await original_complete(self, request, model=model)

    async def tool(self, **kwargs):
        tool_calls.append(kwargs)
        return await original_tool(self, **kwargs)

    async def model_node(self, state):
        if block and window == "before_request" and state.get("tool_calls"):
            reached_model.set()
            await asyncio.Event().wait()
        return await original_model(self, state)

    monkeypatch.setattr(MockProvider, "complete", complete)
    monkeypatch.setattr(ToolGateway, "execute", tool)
    monkeypatch.setattr(WorkerAgent, "model", model_node)
    async with agent_harness(tmp_path, create=True) as first:
        running = asyncio.create_task(first.runner.run("board", CONFIG))
        try:
            await asyncio.wait_for(reached_model.wait(), 10)
        finally:
            running.cancel()
            with suppress(asyncio.CancelledError):
                await running
        snapshot = await first.runner.graph.aget_state(CONFIG, subgraphs=True)
        assert any(v.get("tool_calls") for v in nested_values(snapshot))
        assert "messages" not in snapshot.values
        before = (await first.execution.ledger.records("board"))[0]
        assert before.started and before.status == "running"
        assert bool(before.pending_calls) == (window == "in_flight")
        deadline = (await first.execution.ledger.run("board")).deadline
    block = False
    async with agent_harness(tmp_path) as second:
        result = await second.runner.run("board", CONFIG)
        assert len(tool_calls) == 1
        assert model_budgets == [1000, 1000]
        agent = await second.execution.ledger.get(before.candidate.id)
        assert agent.attempt == 1
        assert agent.deadline == before.deadline
        assert (await second.execution.ledger.run("board")).deadline == deadline
        summary = await second.execution.ledger.summary("board")
        if window == "before_request":
            assert result["status"] == "succeeded" and agent.status == "completed"
            assert summary.tokens == 214  # 15 tool request + 192 final response + 7 observation.
            assert not agent.usage_uncertain and summary.reserved_tokens == 0
        else:
            assert result["status"] == "failed" and agent.status == "failed"
            assert "requires usage reconciliation" in agent.reason
            assert agent.usage_uncertain and summary.tokens == 15
            assert summary.reserved_tokens == 0
        # Only trusted reconciliation can release the interrupted request's unknown reservation.
        await second.execution.ledger.complete_usage(agent.candidate.id, 1)
        assert (await second.execution.ledger.summary("board")).reserved_tokens == 0
