import asyncio
import os
import subprocess
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from work_harness.domain.enums import AllocationMode, RiskLevel, RunStatus
from work_harness.domain.models import (
    AgentAssignment,
    ModelResponse,
    SkillSelection,
    TaskRequest,
    Usage,
    utc_now,
)
from work_harness.llm.mock import MockProvider
from work_harness.runtime import build_runtime
from work_harness.service import (
    IdempotencyConflictError,
    RunCapacityError,
    RunManager,
)
from work_harness.storage import RunRecord
from work_harness.tools.adapters import ToolAdapterRegistry
from work_harness.tools.builtin import EchoReadTool, WorkspaceReadTool

PROJECT_ROOT = Path(__file__).resolve().parents[1]


pytestmark = pytest.mark.usefixtures("legacy_engine")

def test_execution_fingerprint_is_stable_across_python_hash_seeds() -> None:
    payload = {
        "capabilities": {
            "chat",
            "structured_output",
            "tool_calling",
            "vision",
            "streaming",
        },
        "nested": [{"risk_levels": {"critical", "high", "medium", "low"}}],
    }
    expected = RunManager._fingerprint(
        {
            "capabilities": [
                "chat",
                "streaming",
                "structured_output",
                "tool_calling",
                "vision",
            ],
            "nested": [{"risk_levels": ["critical", "high", "low", "medium"]}],
        }
    )
    script = (
        "from work_harness.service import RunManager; "
        f"print(RunManager._fingerprint({payload!r}))"
    )

    for seed in ("1", "2", "314159"):
        environment = os.environ.copy()
        environment["PYTHONHASHSEED"] = seed
        actual = subprocess.check_output(
            [sys.executable, "-c", script],
            cwd=PROJECT_ROOT,
            env=environment,
            text=True,
        ).strip()
        assert actual == expected


@pytest.mark.asyncio
async def test_root_graph_runs_parallel_workers_and_reviews() -> None:
    async with build_runtime(PROJECT_ROOT, checkpoint_backend="memory") as runtime:
        manager = RunManager(runtime)
        run_id = await manager.start(TaskRequest(goal="Create a project launch checklist"))
        result = await manager.wait(run_id)

    assert result.status == RunStatus.SUCCEEDED
    assert len(result.artifacts) == 2
    assert len(result.reviews) == 2
    assert {item.dimension for item in result.reviews} == {"comprehensive"}
    assert result.output
    assert len(result.model_calls) == 7
    assert result.usage.input_tokens > 0


@pytest.mark.asyncio
async def test_high_risk_run_interrupts_and_resumes() -> None:
    async with build_runtime(PROJECT_ROOT, checkpoint_backend="memory") as runtime:
        manager = RunManager(runtime)
        run_id = await manager.start(
            TaskRequest(goal="Delete production database", risk_level=RiskLevel.HIGH)
        )
        waiting = await manager.wait(run_id)
        assert waiting.status == RunStatus.WAITING_HUMAN
        assert len(waiting.reviews) == 6
        assert {item.dimension for item in waiting.reviews} == {
            "fact",
            "logic",
            "constraint",
        }

        await manager.resume(run_id, approved=True, note="test approval")
        resuming = await manager.get(run_id)
        assert resuming.status == RunStatus.RUNNING
        persisted = await runtime.run_store.get(run_id)
        assert persisted is not None and persisted.status == RunStatus.RUNNING
        completed = await manager.wait(run_id)

    assert completed.status == RunStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_functional_assignment_routes_homogeneous_agents() -> None:
    async with build_runtime(PROJECT_ROOT, checkpoint_backend="memory") as runtime:
        manager = RunManager(runtime)
        run_id = await manager.start(
            TaskRequest(
                goal="Analyze requirements and draft an actionable delivery",
                assignment=AgentAssignment(
                    mode=AllocationMode.CROSS_FUNCTIONAL,
                    assignees=["worker-01", "worker-02", "worker-03"],
                    functions_by_agent={
                        "worker-01": ["analysis"],
                        "worker-02": ["execution"],
                        "worker-03": ["risk"],
                    },
                    cross_review=False,
                ),
            )
        )
        result = await manager.wait(run_id)

    assert result.status == RunStatus.SUCCEEDED
    assert {item.producer for item in result.artifacts} == {
        "worker-01",
        "worker-02",
    }
    assert result.plan is not None
    assert {item.assigned_function for item in result.plan.subtasks} == {
        "analysis",
        "execution",
    }
    assert len(result.reviews) == 2
    assert {item.dimension for item in result.reviews} == {"constraint"}


@pytest.mark.asyncio
async def test_decision_task_uses_deep_review() -> None:
    async with build_runtime(PROJECT_ROOT, checkpoint_backend="memory") as runtime:
        manager = RunManager(runtime)
        run_id = await manager.start(
            TaskRequest(goal="Compare two delivery options", task_type="decision")
        )
        result = await manager.wait(run_id)

    assert result.status == RunStatus.SUCCEEDED
    assert len(result.reviews) == 6
    assert {item.dimension for item in result.reviews} == {
        "fact",
        "logic",
        "constraint",
    }


@pytest.mark.asyncio
async def test_skill_routes_workers_to_selected_provider() -> None:
    async with build_runtime(PROJECT_ROOT, checkpoint_backend="memory") as runtime:
        manager = RunManager(runtime)
        run_id = await manager.start(
            TaskRequest(
                goal="Audit the repository delivery risks",
                skills=[
                    SkillSelection(
                        skill_id="risk-audit",
                        provider="mock-secondary",
                    )
                ],
            )
        )
        result = await manager.wait(run_id)

    assert result.status == RunStatus.SUCCEEDED
    assert result.plan is not None
    assert all(item.skill_ids == ["risk-audit"] for item in result.plan.subtasks)
    worker_calls = [item for item in result.model_calls if item.node == "worker"]
    assert worker_calls
    assert {item.provider for item in worker_calls} == {"mock-secondary"}
    assert all(item.skill_ids == ["risk-audit"] for item in worker_calls)


@pytest.mark.asyncio
async def test_worker_executes_advertised_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
    tool_workspace: Path,
) -> None:
    original_complete = MockProvider.complete
    tool_adapters = ToolAdapterRegistry()
    tool_adapters.register("echo_read", lambda _: EchoReadTool())
    tool_adapters.register("workspace_read", lambda _: WorkspaceReadTool(tool_workspace))

    async def complete_with_tool(self, request, *, model):
        operation = request.metadata.get("operation")
        if operation == "plan":
            response = await original_complete(self, request, model=model)
            structured = dict(response.structured_output or {})
            structured["subtasks"] = [
                {**item, "allowed_tools": ["workspace_read"]}
                for item in structured.get("subtasks", [])
            ]
            return response.model_copy(update={"structured_output": structured})
        has_tool_result = any(message.role == "tool" for message in request.messages)
        if operation == "worker" and has_tool_result:
            tool_results = [message for message in request.messages if message.role == "tool"]
            assert all("Workspace tool fixture" in message.content for message in tool_results)
            assert all("Third line." not in message.content for message in tool_results)
        if operation == "worker" and request.tools and not has_tool_result:
            subtask_id = request.metadata["payload"]["subtask"]["subtask_id"]
            return ModelResponse(
                provider=self.name,
                model=model,
                tool_calls=[
                    {
                        "id": f"call_{subtask_id}",
                        "type": "function",
                        "function": {
                            "name": "workspace_read",
                            "arguments": '{"operation":"read","path":"README.md","max_lines":2}',
                        },
                    }
                ],
                usage=Usage(input_tokens=10, output_tokens=5),
            )
        return await original_complete(self, request, model=model)

    monkeypatch.setattr(MockProvider, "complete", complete_with_tool)
    async with build_runtime(
        PROJECT_ROOT, checkpoint_backend="memory", tool_adapters=tool_adapters
    ) as runtime:
        manager = RunManager(runtime)
        run_id = await manager.start(TaskRequest(goal="Inspect the repository documentation"))
        result = await manager.wait(run_id)

    assert result.status == RunStatus.SUCCEEDED
    assert len(result.tool_calls) == 2
    assert {item.tool_name for item in result.tool_calls} == {"workspace_read"}
    assert all("Workspace tool fixture" in item.output_summary for item in result.tool_calls)
    assert all("Third line." not in item.output_summary for item in result.tool_calls)


@pytest.mark.asyncio
async def test_scheduler_owns_artifact_and_review_routing_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_complete = MockProvider.complete

    async def complete_with_wrong_routing(self, request, *, model):
        response = await original_complete(self, request, model=model)
        structured = dict(response.structured_output or {})
        operation = request.metadata.get("operation")
        if operation == "worker":
            structured.update(
                {
                    "run_id": "run_from_untrusted_model",
                    "subtask_id": "wrong-subtask",
                    "producer": "wrong-agent",
                    "version": 99,
                }
            )
        elif operation == "review":
            structured.update(
                {
                    "artifact_id": "artifact_from_untrusted_model",
                    "artifact_version": 99,
                    "reviewer": "wrong-reviewer",
                    "dimension": "wrong-dimension",
                }
            )
        return response.model_copy(update={"structured_output": structured})

    monkeypatch.setattr(MockProvider, "complete", complete_with_wrong_routing)
    async with build_runtime(PROJECT_ROOT, checkpoint_backend="memory") as runtime:
        manager = RunManager(runtime)
        run_id = await manager.start(TaskRequest(goal="Verify provider routing invariants"))
        result = await manager.wait(run_id)

    assert result.status == RunStatus.SUCCEEDED
    assert result.plan is not None
    planned = {item.subtask_id: item for item in result.plan.subtasks}
    assert {item.subtask_id for item in result.artifacts} == set(planned)
    assert all(item.run_id == run_id and item.version == 1 for item in result.artifacts)
    assert all(item.producer == planned[item.subtask_id].worker_role for item in result.artifacts)
    artifacts = {item.artifact_id: item for item in result.artifacts}
    assert all(item.artifact_id in artifacts for item in result.reviews)
    assert all(
        item.artifact_version == artifacts[item.artifact_id].version
        for item in result.reviews
    )
    assert {item.reviewer for item in result.reviews} == {"reviewer"}
    assert {item.dimension for item in result.reviews} == {"comprehensive"}


@pytest.mark.asyncio
async def test_sqlite_checkpoint_recovers_after_runtime_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint = tmp_path / "checkpoint.sqlite"
    run_store = tmp_path / "runs.sqlite"
    monkeypatch.setenv("WORK_HARNESS_CHECKPOINT_PATH", str(checkpoint))
    monkeypatch.setenv("WORK_HARNESS_RUN_STORE_PATH", str(run_store))
    request = TaskRequest(
        goal="Delete production database",
        risk_level=RiskLevel.HIGH,
        idempotency_key="restart-safe-request",
    )

    async with build_runtime(PROJECT_ROOT) as first_runtime:
        first_manager = RunManager(first_runtime)
        run_id = await first_manager.start(request)
        waiting = await first_manager.wait(run_id)
        assert waiting.status == RunStatus.WAITING_HUMAN

    async with build_runtime(PROJECT_ROOT) as second_runtime:
        second_manager = RunManager(second_runtime)
        assert await second_manager.start(request) == run_id
        assert [item.run_id for item in await second_manager.list()] == [run_id]
        recovered = await second_manager.get(run_id)
        assert recovered.status == RunStatus.WAITING_HUMAN
        await second_manager.resume(run_id, approved=True)
        completed = await second_manager.wait(run_id)

    assert completed.status == RunStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_recovery_fails_orphaned_index_without_checkpoint() -> None:
    async with build_runtime(PROJECT_ROOT, checkpoint_backend="memory") as runtime:
        await runtime.run_store.reserve(RunRecord(run_id="run_orphaned"))
        manager = RunManager(runtime)
        assert await manager.recover_incomplete() == []
        recovered = await manager.get("run_orphaned")

    assert recovered.status == RunStatus.FAILED


@pytest.mark.asyncio
async def test_recovery_resumes_claimed_checkpoint_state() -> None:
    class Snapshot:
        def __init__(self, values):
            self.values = values

    class RecoverableGraph:
        def __init__(self):
            self.values = {"run_id": "run_recoverable", "status": "running"}
            self.resumed = False

        async def aget_state(self, _):
            return Snapshot(dict(self.values))

        async def ainvoke(self, input_value, _):
            assert input_value is None
            self.resumed = True
            self.values["status"] = "succeeded"

    async with build_runtime(PROJECT_ROOT, checkpoint_backend="memory") as runtime:
        graph = RecoverableGraph()
        runtime.graph = graph
        await runtime.run_store.reserve(
            RunRecord(run_id="run_recoverable", status=RunStatus.RUNNING)
        )
        manager = RunManager(runtime)
        assert await manager.recover_incomplete() == ["run_recoverable"]
        result = await manager.wait("run_recoverable")

    assert graph.resumed is True
    assert result.status == RunStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_run_manager_bounds_concurrent_graph_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_complete = MockProvider.complete

    async def delayed_complete(self, request, *, model):
        await asyncio.sleep(0.02)
        return await original_complete(self, request, model=model)

    monkeypatch.setattr(MockProvider, "complete", delayed_complete)
    async with build_runtime(PROJECT_ROOT, checkpoint_backend="memory") as runtime:
        runtime.config.runtime.run_max_concurrency = 1
        manager = RunManager(runtime)
        first = await manager.start(TaskRequest(goal="First bounded run"))
        second = await manager.start(TaskRequest(goal="Second bounded run"))
        await asyncio.sleep(0.01)

        assert manager.stats()["active_runs"] == 1
        assert manager.stats()["queued_runs"] == 1

        first_result, second_result = await asyncio.gather(
            manager.wait(first),
            manager.wait(second),
        )

    assert first_result.status == RunStatus.SUCCEEDED
    assert second_result.status == RunStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_run_manager_rejects_new_work_when_admission_queue_is_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_provider = asyncio.Event()
    original_complete = MockProvider.complete

    async def blocked_complete(self, request, *, model):
        await release_provider.wait()
        return await original_complete(self, request, model=model)

    monkeypatch.setattr(MockProvider, "complete", blocked_complete)
    async with build_runtime(PROJECT_ROOT, checkpoint_backend="memory") as runtime:
        runtime.config.runtime.run_max_concurrency = 1
        runtime.config.runtime.run_queue_capacity = 1
        runtime.config.runtime.run_queue_retry_after_seconds = 7
        manager = RunManager(runtime)
        first_request = TaskRequest(
            goal="First admitted run",
            idempotency_key="first-admission-key",
        )
        first = await manager.start(first_request)
        second = await manager.start(
            TaskRequest(
                goal="Second admitted run",
                idempotency_key="second-admission-key",
            )
        )
        await asyncio.sleep(0)

        assert await manager.start(first_request) == first
        with pytest.raises(IdempotencyConflictError):
            await manager.start(
                TaskRequest(
                    goal="Conflicting retry",
                    idempotency_key="first-admission-key",
                )
            )
        with pytest.raises(RunCapacityError) as rejected:
            await manager.start(TaskRequest(goal="Rejected third run"))

        stats = manager.stats()
        assert rejected.value.retry_after_seconds == 7
        assert stats["active_runs"] == 1
        assert stats["queued_runs"] == 1
        assert stats["inflight_runs"] == 2
        assert stats["inflight_capacity"] == 2
        assert stats["available_admission_slots"] == 0
        assert stats["admitted_runs_total"] == 2
        assert stats["rejected_runs_total"] == 1

        release_provider.set()
        first_result, second_result = await asyncio.gather(
            manager.wait(first),
            manager.wait(second),
        )
        assert manager.stats()["available_admission_slots"] == 2

    assert first_result.status == RunStatus.SUCCEEDED
    assert second_result.status == RunStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_recovery_scan_obeys_admission_capacity() -> None:
    class Snapshot:
        def __init__(self, values):
            self.values = values

    class RecoverableGraph:
        def __init__(self):
            self.values = {
                "run_recovery_a": {"run_id": "run_recovery_a", "status": "running"},
                "run_recovery_b": {"run_id": "run_recovery_b", "status": "running"},
            }
            self.release = asyncio.Event()

        async def aget_state(self, config):
            run_id = config["configurable"]["thread_id"]
            return Snapshot(dict(self.values[run_id]))

        async def ainvoke(self, input_value, config):
            assert input_value is None
            run_id = config["configurable"]["thread_id"]
            await self.release.wait()
            self.values[run_id]["status"] = "succeeded"

    async with build_runtime(PROJECT_ROOT, checkpoint_backend="memory") as runtime:
        graph = RecoverableGraph()
        runtime.graph = graph
        runtime.config.runtime.run_max_concurrency = 1
        runtime.config.runtime.run_queue_capacity = 0
        for run_id in graph.values:
            await runtime.run_store.reserve(
                RunRecord(run_id=run_id, status=RunStatus.RUNNING)
            )

        manager = RunManager(runtime)
        first_batch = await manager.recover_incomplete()
        assert len(first_batch) == 1
        assert manager.stats()["recovery_backlog_observed"] == 1
        assert manager.stats()["recovered_runs_total"] == 1

        graph.release.set()
        await manager.wait(first_batch[0])
        second_batch = await manager.recover_incomplete()
        assert set(second_batch) == set(graph.values) - set(first_batch)
        await manager.wait(second_batch[0])

        assert manager.stats()["recovered_runs_total"] == 2
        assert manager.stats()["recovery_backlog_observed"] == 0


@pytest.mark.asyncio
async def test_two_worker_managers_consume_shared_queue_without_duplicate_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls_by_run: dict[str, int] = {}
    original_complete = MockProvider.complete

    async def counted_complete(self, request, *, model):
        calls_by_run[request.run_id] = calls_by_run.get(request.run_id, 0) + 1
        await asyncio.sleep(0.001)
        return await original_complete(self, request, model=model)

    monkeypatch.setattr(MockProvider, "complete", counted_complete)
    async with build_runtime(PROJECT_ROOT, checkpoint_backend="memory") as runtime:
        api_config = runtime.config.model_copy(deep=True)
        api_config.runtime.execution.role = "api"
        api_config.runtime.run_max_concurrency = 1
        worker_a_config = runtime.config.model_copy(deep=True)
        worker_a_config.runtime.execution.role = "worker"
        worker_a_config.runtime.run_max_concurrency = 1
        worker_b_config = worker_a_config.model_copy(deep=True)
        api_runtime = replace(runtime, config=api_config)
        worker_a_runtime = replace(runtime, config=worker_a_config)
        worker_b_runtime = replace(runtime, config=worker_b_config)
        api = RunManager(api_runtime)
        worker_a = RunManager(worker_a_runtime)
        worker_b = RunManager(worker_b_runtime)
        await api.initialize()
        await worker_a.initialize()
        await worker_b.initialize()
        try:
            live_workers = await runtime.run_store.list_live_workers(stale_seconds=30)
            assert len(live_workers) == 2
            assert sum(item.capacity for item in live_workers) == 2
            run_ids = [
                await api.start(TaskRequest(goal=f"Distributed run {index}"))
                for index in range(6)
            ]
            for _ in range(200):
                stats = await runtime.run_store.queue_stats()
                if stats.completed_jobs == len(run_ids):
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("shared queue did not drain before the test deadline")

            results = [await api.get(run_id) for run_id in run_ids]
        finally:
            await worker_a.close()
            await worker_b.close()
            await api.close()
        assert await runtime.run_store.list_live_workers(stale_seconds=30) == []

    assert all(result.status == RunStatus.SUCCEEDED for result in results)
    assert set(calls_by_run) == set(run_ids)
    assert all(count == 7 for count in calls_by_run.values())


@pytest.mark.asyncio
async def test_reclaimed_queue_job_resumes_existing_checkpoint_without_restarting() -> None:
    class Snapshot:
        def __init__(self, values):
            self.values = values

    class ReclaimedGraph:
        def __init__(self):
            self.values = {"run_id": "run_reclaimed", "status": "running"}
            self.input_value = "not-called"

        async def aget_state(self, _):
            return Snapshot(dict(self.values))

        async def ainvoke(self, input_value, _):
            self.input_value = input_value
            self.values["status"] = "succeeded"

    async with build_runtime(PROJECT_ROOT, checkpoint_backend="memory") as runtime:
        graph = ReclaimedGraph()
        runtime.graph = graph
        worker_config = runtime.config.model_copy(deep=True)
        worker_config.runtime.execution.role = "worker"
        worker = RunManager(replace(runtime, config=worker_config))
        await runtime.run_store.submit(
            RunRecord(run_id="run_reclaimed"),
            {"goal": "must not restart from raw input"},
            capacity=2,
            max_attempts=3,
            graph_revision=worker.execution_fingerprint,
        )
        abandoned = await runtime.run_store.claim_job(
            owner="crashed-worker",
            lease_seconds=30,
            graph_revision=worker.execution_fingerprint,
        )
        assert abandoned is not None
        runtime.run_store._jobs[abandoned.job_id] = abandoned.model_copy(
            update={"lease_expires_at": utc_now() - timedelta(seconds=1)}
        )
        await worker.initialize()
        try:
            for _ in range(100):
                record = await runtime.run_store.get("run_reclaimed")
                if record and record.status == RunStatus.SUCCEEDED:
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("reclaimed run did not complete")
        finally:
            await worker.close()

    assert graph.input_value is None


@pytest.mark.asyncio
async def test_attempt_exhaustion_reconciles_terminal_checkpoint_before_dead_letter() -> None:
    class Snapshot:
        values = {"run_id": "run_reconcile_terminal", "status": "succeeded"}

    class TerminalGraph:
        invoked = False

        async def aget_state(self, _):
            return Snapshot()

        async def ainvoke(self, *_):
            self.invoked = True
            raise AssertionError("terminal checkpoint must not run again")

    async with build_runtime(PROJECT_ROOT, checkpoint_backend="memory") as runtime:
        graph = TerminalGraph()
        runtime.graph = graph
        worker_config = runtime.config.model_copy(deep=True)
        worker_config.runtime.execution.role = "worker"
        worker = RunManager(replace(runtime, config=worker_config))
        await runtime.run_store.submit(
            RunRecord(run_id="run_reconcile_terminal"),
            {"goal": "already completed before ack"},
            capacity=2,
            max_attempts=1,
            graph_revision=worker.execution_fingerprint,
        )
        abandoned = await runtime.run_store.claim_job(
            owner="crashed-worker",
            lease_seconds=30,
            graph_revision=worker.execution_fingerprint,
        )
        assert abandoned is not None
        runtime.run_store._jobs[abandoned.job_id] = abandoned.model_copy(
            update={"lease_expires_at": utc_now() - timedelta(seconds=1)}
        )

        await worker.initialize()
        try:
            for _ in range(100):
                stats = await runtime.run_store.queue_stats()
                if stats.completed_jobs == 1:
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("terminal checkpoint was not reconciled")
        finally:
            await worker.close()

        record = await runtime.run_store.get("run_reconcile_terminal")
        assert record is not None and record.status == RunStatus.SUCCEEDED
        assert graph.invoked is False
        assert (await runtime.run_store.queue_stats()).dead_jobs == 0
