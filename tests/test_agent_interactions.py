from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import suppress
from pathlib import Path

import pytest
from pydantic import ValidationError

from work_harness.domain.models import (
    AgentAssignment,
    Artifact,
    ModelResponse,
    SubtaskSpec,
    TaskRequest,
    TaskSpec,
    Usage,
)
from work_harness.llm.mock import MockProvider
from work_harness.runtime import build_runtime
from work_harness.service import RunManager
from work_harness.tools.gateway import ToolGateway
from work_harness.workflow.contracts import WorkerAssignment

ROOT = Path(__file__).resolve().parents[1]


def task(name: str, dependencies: tuple[str, ...] = (), *, tools: bool = False) -> dict:
    return SubtaskSpec(
        subtask_id=name,
        title=name,
        description=f"Complete {name}",
        dependencies=list(dependencies),
        acceptance_criteria=[f"Verify {name}"],
        allowed_tools=["echo_read"] if tools else [],
    ).model_dump(mode="json")


def tool_response(provider: str, model: str, call_id: str) -> ModelResponse:
    return ModelResponse(
        provider=provider,
        model=model,
        usage=Usage(input_tokens=10, output_tokens=5),
        tool_calls=[
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": "echo_read",
                    "arguments": '{"text":"tool-result"}',
                },
            }
        ],
    )


pytestmark = pytest.mark.usefixtures("legacy_engine")

@pytest.mark.parametrize("run_budget", (18_000, 40_000))
async def test_planner_receives_cumulative_worker_allowance_capped_by_run(monkeypatch, run_budget):
    original = MockProvider.complete
    seen = []

    async def complete(self, request, *, model):
        response = await original(self, request, model=model)
        if request.node_name == "plan":
            limits = request.metadata["payload"]["execution_limits"]
            assert limits["token_accounting"] == "observed_usage_only"
            assert "run_token_budget" not in limits
            budget = limits["workers"]["worker-01"]["max_output_tokens"]
            assert budget == 20_000
            subtask = response.structured_output["subtasks"][0]
            subtask["max_output_tokens"] = budget
            response.structured_output["subtasks"] = [subtask]
        if request.node_name == "worker":
            seen.append(request.max_output_tokens)
            response.usage = Usage(input_tokens=13_000, output_tokens=500)
        return response

    monkeypatch.setattr(MockProvider, "complete", complete)
    async with build_runtime(ROOT, checkpoint_backend="memory") as runtime:
        runtime.config.runtime.token_budget = run_budget
        for role in runtime.config.agents.agents.values():
            if role.assignable:
                role.max_output_tokens = 20_000
        manager = RunManager(runtime)
        try:
            run_id = await manager.start(TaskRequest(goal="Calculate one total"))
            result = await manager.wait(run_id)
        finally:
            await manager.close()
    assert result.status == "succeeded"
    assert seen == [20_000]
    assert result.usage.input_tokens >= 13_000


@pytest.mark.asyncio
async def test_dependency_handoffs_are_scoped_and_private_histories_do_not_escape(monkeypatch):
    original = MockProvider.complete
    received = {}

    async def complete(self, request, *, model):
        response = await original(self, request, model=model)
        operation = request.metadata["operation"]
        if operation == "plan":
            return response.model_copy(
                update={
                    "structured_output": {
                        "subtasks": [
                            task("source"),
                            task("consumer", ("source",)),
                            task("unrelated"),
                        ]
                    }
                }
            )
        if operation == "worker":
            payload = request.metadata["payload"]
            name = payload["subtask"]["subtask_id"]
            received[name] = payload
            assert len(request.messages) == 2
            if name == "consumer":
                assert [a["subtask_id"] for a in payload["dependency_artifacts"]] == ["source"]
                assert payload["dependency_artifacts"][0]["content"] == "upstream evidence"
            if name == "source":
                response.structured_output["content"] = "upstream evidence"
        return response

    monkeypatch.setattr(MockProvider, "complete", complete)
    async with build_runtime(ROOT, checkpoint_backend="memory") as runtime:
        manager = RunManager(runtime)
        run_id = await manager.start(
            TaskRequest(
                goal="Check scoped handoffs",
                assignment=AgentAssignment(mode="direct", assignees=["worker-01"]),
            )
        )
        result = await manager.wait(run_id)
        snapshot = await runtime.graph.aget_state(manager._graph_config(run_id))
        nested = runtime.graph.get_graph(xray=True)
        assert any("worker:model" in name for name in nested.nodes)
        assert any("reviewer:review" in name for name in nested.nodes)

    assert result.status == "succeeded"
    assert {item.producer for item in result.artifacts} == {"worker-01"}
    assert received["unrelated"]["dependency_artifacts"] == []
    assert not {"messages", "assignment", "response", "tool_index"}.intersection(snapshot.values)
    dependencies = [item for item in result.agent_interactions if item.kind == "dependency"]
    assert len(dependencies) == 1
    assert dependencies[0].subtask_id == "consumer"
    assert len({item.interaction_id for item in result.agent_interactions}) == len(
        result.agent_interactions
    )


@pytest.mark.asyncio
async def test_targeted_repair_invalidates_descendants_and_delivers_versioned_feedback(monkeypatch):
    original = MockProvider.complete
    workers = []
    reviews = []

    async def complete(self, request, *, model):
        response = await original(self, request, model=model)
        operation = request.metadata["operation"]
        payload = request.metadata["payload"]
        if operation == "plan":
            response.structured_output = {
                "subtasks": [
                    task("source"),
                    task("middle", ("source",)),
                    task("leaf", ("middle",)),
                    task("unrelated"),
                ]
            }
        elif operation == "worker":
            name, version = payload["subtask"]["subtask_id"], payload["version"]
            workers.append((name, version))
            if version == 2:
                assert payload["previous_artifact"]["version"] == 1
                assert payload["feedback"]
                assert payload["repair_reasons"] == ["Correct upstream evidence"]
                assert all(a["version"] == 2 for a in payload["dependency_artifacts"])
            response.structured_output["content"] = f"{name} version {version}"
        elif operation == "review":
            artifact = payload["artifact"]
            reviews.append((artifact["subtask_id"], artifact["version"]))
            assert payload["subtask"]["acceptance_criteria"]
            if artifact["subtask_id"] == "source" and artifact["version"] == 1:
                response.structured_output.update(
                    {
                        "verdict": "revise",
                        "issues": [
                            {
                                "code": "incorrect_evidence",
                                "severity": "error",
                                "message": "Evidence needs correction",
                                "recommendation": "Recheck source",
                            }
                        ],
                    }
                )
        elif operation == "judge" and payload["repair_round"] == 0:
            response.structured_output = {
                "action": "repair",
                "target_subtasks": ["source"],
                "reasons": ["Correct upstream evidence"],
            }
        return response

    monkeypatch.setattr(MockProvider, "complete", complete)
    async with build_runtime(ROOT, checkpoint_backend="memory") as runtime:
        manager = RunManager(runtime)
        run_id = await manager.start(TaskRequest(goal="Repair a dependency chain"))
        result = await manager.wait(run_id)

    assert result.status == "succeeded"
    assert result.repair_rounds == 1
    assert [name for name, version in workers if version == 2] == ["source", "middle", "leaf"]
    assert Counter(workers)[("unrelated", 1)] == 1
    assert ("unrelated", 2) not in workers
    assert Counter(reviews)[("unrelated", 1)] == 1
    assert len(result.artifacts) == 7
    assert len(result.reviews) == 7
    for name in ("source", "middle", "leaf"):
        assert len({a.artifact_id for a in result.artifacts if a.subtask_id == name}) == 1


@pytest.mark.asyncio
async def test_worker_tool_checkpoint_resumes_after_runtime_restart_without_repeating_tool(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("WORK_HARNESS_CHECKPOINT_PATH", str(tmp_path / "checkpoints.sqlite"))
    monkeypatch.setenv("WORK_HARNESS_RUN_STORE_PATH", str(tmp_path / "runs.sqlite"))
    original = MockProvider.complete
    execute = ToolGateway.execute
    reached_second_call = asyncio.Event()
    block = True
    tool_count = 0

    async def complete(self, request, *, model):
        if request.metadata["operation"] == "plan":
            return ModelResponse(
                provider=self.name,
                model=model,
                structured_output={"subtasks": [task("source", tools=True)]},
            )
        if request.metadata["operation"] == "worker":
            if not request.metadata["tool_round"]:
                return tool_response(self.name, model, "read-once")
            if block:
                reached_second_call.set()
                await asyncio.Event().wait()
        return await original(self, request, model=model)

    async def counted_execute(self, **kwargs):
        nonlocal tool_count
        tool_count += 1
        return await execute(self, **kwargs)

    monkeypatch.setattr(MockProvider, "complete", complete)
    monkeypatch.setattr(ToolGateway, "execute", counted_execute)
    config = {"configurable": {"thread_id": "run_child_restart"}, "recursion_limit": 50}
    async with build_runtime(ROOT, provider_store_backend="memory") as runtime:
        running = asyncio.create_task(
            runtime.graph.ainvoke(
                {
                    "run_id": "run_child_restart",
                    "raw_input": {"goal": "Resume inside a Worker"},
                },
                config,
            )
        )
        try:
            await asyncio.wait_for(reached_second_call.wait(), timeout=5)
        finally:
            running.cancel()
            with suppress(asyncio.CancelledError):
                await running
        snapshot = await runtime.graph.aget_state(config, subgraphs=True)
        assert snapshot.tasks[0].state.values["tool_calls"][0]["call_id"] == "read-once"
        assert "messages" not in snapshot.values
    block = False
    async with build_runtime(ROOT, provider_store_backend="memory") as runtime:
        result = await runtime.graph.ainvoke(None, config)

    assert result["status"] == "succeeded"
    assert tool_count == 1
    assert len(result["tool_calls"]) == 1
    assert len(result["artifacts"]) == 1
    assert len([c for c in result["model_calls"] if c["node"] == "worker"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["steps", "budget", "partial"])
async def test_agent_limits_terminate_without_losing_usage(monkeypatch, failure_mode):
    original = MockProvider.complete
    calls = 0

    async def complete(self, request, *, model):
        nonlocal calls
        if request.metadata["operation"] == "plan":
            return ModelResponse(
                provider=self.name,
                model=model,
                structured_output={"subtasks": [task("bounded", tools=True)]},
            )
        if request.metadata["operation"] == "worker":
            calls += 1
            if failure_mode == "steps":
                return tool_response(self.name, model, f"call-{calls}")
            response = await original(self, request, model=model)
            if failure_mode == "budget":
                response.usage = Usage(input_tokens=6000)
            else:
                response.structured_output["status"] = "partial"
            return response
        return await original(self, request, model=model)

    monkeypatch.setattr(MockProvider, "complete", complete)
    async with build_runtime(ROOT, checkpoint_backend="memory") as runtime:
        manager = RunManager(runtime)
        run_id = await manager.start(TaskRequest(goal="Bound the Agent loop"))
        result = await manager.wait(run_id)

    assert result.status == ("succeeded" if failure_mode == "budget" else "failed")
    assert bool(result.errors) == (failure_mode != "budget")
    assert calls == (8 if failure_mode == "steps" else 1)
    assert len([c for c in result.model_calls if c.node == "worker"]) == calls
    if failure_mode == "budget":
        assert result.usage.input_tokens >= 6000


def test_handoff_rejects_cross_run_or_unrelated_dependencies():
    artifact = Artifact(
        run_id="other-run",
        subtask_id="upstream",
        producer="worker-01",
        content="private",
        confidence=1,
    )
    with pytest.raises(ValidationError, match="from this run"):
        WorkerAssignment(
            run_id="run",
            task=TaskSpec(goal="test"),
            subtask=task("consumer", ("upstream",)),
            dependency_artifacts=[artifact],
        )
    with pytest.raises(ValidationError, match="declared dependencies"):
        WorkerAssignment(
            run_id="run",
            task=TaskSpec(goal="test"),
            subtask=task("consumer"),
            dependency_artifacts=[artifact],
        )


@pytest.mark.asyncio
async def test_distributed_fingerprint_includes_prompt_content_and_workflow_revision(monkeypatch):
    from work_harness.prompts import PromptRegistry

    async with build_runtime(ROOT, checkpoint_backend="memory") as runtime:
        manager = RunManager(runtime)
        original = manager.execution_fingerprint
        prompts = runtime.prompt_registry
        runtime.prompt_registry = PromptRegistry(ROOT / "prompts", overrides={"worker": "Changed"})
        assert manager.execution_fingerprint != original
        runtime.prompt_registry = prompts
        assert manager.execution_fingerprint == original
        with monkeypatch.context() as patch:
            patch.setattr("work_harness.service.WORKFLOW_REVISION", "future-graph")
            assert manager.execution_fingerprint != original


@pytest.mark.asyncio
async def test_legacy_checkpoint_remains_readable_and_resume_rejects_before_mutation():
    async with build_runtime(ROOT, checkpoint_backend="memory") as runtime:
        manager = RunManager(runtime)
        run_id = await manager.start(TaskRequest(goal="Approve a migration", risk_level="high"))
        assert (await manager.wait(run_id)).status == "waiting_human"
        await runtime.graph.aupdate_state(
            manager._graph_config(run_id),
            {"workflow_revision": "legacy"},
        )
        with pytest.raises(ValueError, match="workflow revision is incompatible"):
            await manager.resume(run_id, approved=True)
        assert (await manager.get(run_id)).status == "waiting_human"
        assert (await runtime.run_store.get(run_id)).status == "waiting_human"


@pytest.mark.asyncio
async def test_long_dependency_chain_respects_configured_plan_size(monkeypatch):
    original = MockProvider.complete

    async def complete(self, request, *, model):
        response = await original(self, request, model=model)
        if request.metadata["operation"] == "plan":
            response.structured_output = {
                "subtasks": [
                    task(f"step-{index}", (f"step-{index - 1}",) if index else ())
                    for index in range(12)
                ]
            }
        return response

    monkeypatch.setattr(MockProvider, "complete", complete)
    async with build_runtime(ROOT, checkpoint_backend="memory") as runtime:
        runtime.config.runtime.max_planned_subtasks = 12
        manager = RunManager(runtime)
        run_id = await manager.start(TaskRequest(goal="Execute a long, bounded dependency chain"))
        result = await manager.wait(run_id)
    assert result.status == "succeeded"
    assert len(result.artifacts) == 12
    assert len(result.reviews) == 12
