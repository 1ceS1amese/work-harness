import asyncio
from pathlib import Path

import pytest

from work_harness.domain.enums import AllocationMode, RiskLevel, RunStatus
from work_harness.domain.models import (
    AgentAssignment,
    ModelResponse,
    SkillSelection,
    TaskRequest,
    Usage,
)
from work_harness.llm.mock import MockProvider
from work_harness.runtime import build_runtime
from work_harness.service import RunManager
from work_harness.storage import RunRecord

PROJECT_ROOT = Path(__file__).resolve().parents[1]


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
) -> None:
    original_complete = MockProvider.complete

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
    async with build_runtime(PROJECT_ROOT, checkpoint_backend="memory") as runtime:
        manager = RunManager(runtime)
        run_id = await manager.start(TaskRequest(goal="Inspect the repository documentation"))
        result = await manager.wait(run_id)

    assert result.status == RunStatus.SUCCEEDED
    assert len(result.tool_calls) == 2
    assert {item.tool_name for item in result.tool_calls} == {"workspace_read"}
    assert all("Work Harness" in item.output_summary for item in result.tool_calls)


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
