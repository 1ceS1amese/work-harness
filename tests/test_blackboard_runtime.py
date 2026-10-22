"""Product admission and restart tests use real stores/graphs with deterministic models."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from work_harness.api.app import create_app
from work_harness.blackboard.controller import BlackboardController
from work_harness.blackboard.models import Fact, Hint
from work_harness.blackboard.protocol import BoardClosedError, BoardNotFoundError
from work_harness.config.loader import load_app_config
from work_harness.domain.enums import RunStatus
from work_harness.domain.models import TaskRequest
from work_harness.llm.mock import MockProvider
from work_harness.prompts import PromptRegistry
from work_harness.runtime import build_runtime
from work_harness.service import IdempotencyConflictError, RunManager

ROOT = Path(__file__).resolve().parents[1]


def draft_request(**updates) -> TaskRequest:
    return TaskRequest.model_validate(
        {
            "goal": "Write a candidate launch brief",
            "engine": "blackboard",
            "blackboard": {
                "acceptance": [
                    {
                        "id": "saved-draft",
                        "description": "Save a readable draft, without certifying its accuracy",
                        "claim_key": "draft.content",
                        "claim_type": "delivery",
                        "scope": {"verification": "stored_content_only"},
                        "required_checks": ["content_stored"],
                    }
                ],
            },
            **updates,
        }
    )


pytestmark = pytest.mark.usefixtures("legacy_engine")

@pytest.fixture
def sqlite_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, file_name in (
        ("WORK_HARNESS_CHECKPOINT_PATH", "checkpoints.sqlite"),
        ("WORK_HARNESS_RUN_STORE_PATH", "runs.sqlite"),
        ("WORK_HARNESS_BLACKBOARD_PATH", "blackboard.sqlite"),
    ):
        monkeypatch.setenv(name, str(tmp_path / file_name))


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
async def test_one_manager_runs_both_engines_with_pinned_versions(backend, sqlite_paths) -> None:
    async with build_runtime(
        ROOT, checkpoint_backend=backend, provider_store_backend="memory"
    ) as runtime:
        manager = RunManager(runtime)
        try:
            legacy_id = await manager.start(
                TaskRequest(goal="Write a launch brief", engine="legacy")
            )
            board_id = await manager.start(draft_request())
            legacy, board_result = await asyncio.gather(
                manager.wait(legacy_id), manager.wait(board_id)
            )
            assert legacy.engine == "legacy" and legacy.status == "succeeded"
            assert board_result.engine == "blackboard" and board_result.status == "succeeded"
            assert board_result.usage.input_tokens > 0
            assert board_result.output and board_result.plan is None
            assert board_result.artifacts == board_result.reviews == []
            old_version = (await runtime.run_store.get(legacy_id)).execution
            new_version = (await runtime.run_store.get(board_id)).execution
            assert old_version.workflow_revision == "agent-subgraphs-v2"
            assert new_version.workflow_revision == "blackboard-v8"
            assert old_version.schema_version == new_version.schema_version == 1
            assert old_version.configuration_fingerprint != new_version.configuration_fingerprint
            board = await (await runtime.blackboard_store()).snapshot(board_id)
            facts = [item for item in board.objects if isinstance(item, Fact)]
            assert len(facts) == 1
            assert facts[0].claim_type == "delivery"
            assert facts[0].verification.checks == ("content_stored",)
            assert (
                board.by_id()[board.root_intent_id].acceptance
                == draft_request().blackboard.acceptance
            )
        finally:
            await manager.close()


@pytest.mark.asyncio
async def test_default_engine_changes_do_not_rebind_idempotent_requests() -> None:
    async with build_runtime(ROOT, checkpoint_backend="memory") as runtime:
        manager = RunManager(runtime)
        try:
            legacy_request = TaskRequest(goal="Draft a brief", idempotency_key="implicit-legacy")
            legacy_id = await manager.start(legacy_request)
            assert (await manager.wait(legacy_id)).status == "succeeded"
            runtime.config.runtime.workflow.engine = "blackboard"
            assert await manager.start(legacy_request) == legacy_id
            request = draft_request(engine=None, idempotency_key="implicit-board")
            board_id = await manager.start(request)
            assert (await manager.wait(board_id)).engine == "blackboard"
            runtime.config.runtime.workflow.engine = "legacy"
            assert await manager.start(request) == board_id
            assert (await runtime.run_store.get(legacy_id)).engine == "legacy"
            assert (await runtime.run_store.get(board_id)).engine == "blackboard"
            with pytest.raises(IdempotencyConflictError):
                await manager.start(draft_request(idempotency_key="implicit-legacy"))
        finally:
            await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ("legacy", "blackboard"))
async def test_recovery_before_first_checkpoint_uses_saved_engine_and_input(
    engine, sqlite_paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = (
        draft_request() if engine == "blackboard" else TaskRequest(goal="Draft a launch brief")
    )
    async with build_runtime(
        ROOT, checkpoint_backend="sqlite", provider_store_backend="memory"
    ) as first:
        manager = RunManager(first)
        monkeypatch.setattr(manager, "_spawn_execute", lambda *args, **kwargs: None)
        run_id = await manager.start(request)
        original = await first.run_store.get(run_id)
        assert await first.checkpointer.aget_tuple({"configurable": {"thread_id": run_id}}) is None
        assert await first.run_store.get_initial_input(run_id) is not None
        await manager.close()
    monkeypatch.setenv(
        "WORK_HARNESS_WORKFLOW_ENGINE", "legacy" if engine == "blackboard" else "blackboard"
    )
    async with build_runtime(
        ROOT, checkpoint_backend="sqlite", provider_store_backend="memory"
    ) as second:
        manager = RunManager(second)
        try:
            assert await manager.recover_incomplete() == [run_id]
            result = await manager.wait(run_id)
            assert result.engine == engine
            assert result.status == "succeeded"
            assert (await second.run_store.get(run_id)).execution == original.execution
        finally:
            await manager.close()


@pytest.mark.asyncio
async def test_sqlite_resume_does_not_repeat_an_agent_after_its_commit(
    sqlite_paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    committed = asyncio.Event()
    never = asyncio.Event()
    original_commit = BlackboardController.commit
    original_complete = MockProvider.complete
    model_calls = 0
    interrupt_once = True

    async def count_call(self, request, *, model):
        nonlocal model_calls
        model_calls += 1
        return await original_complete(self, request, model=model)

    async def interrupted_commit(self, state):
        nonlocal interrupt_once
        result = await original_commit(self, state)
        board = await self.kernel.store.snapshot(state["board_id"])
        if interrupt_once and any(
            isinstance(item, Hint) and item.topic == "task" for item in board.objects
        ):
            interrupt_once = False
            committed.set()
            await never.wait()
        return result

    monkeypatch.setattr(MockProvider, "complete", count_call)
    monkeypatch.setattr(BlackboardController, "commit", interrupted_commit)
    async with build_runtime(
        ROOT, checkpoint_backend="sqlite", provider_store_backend="memory"
    ) as first:
        manager = RunManager(first)
        run_id = await manager.start(draft_request())
        await asyncio.wait_for(committed.wait(), 5)
        calls_before = model_calls
        assert calls_before > 0
        await manager.close()
        assert (await first.run_store.get(run_id)).status == "running"
    monkeypatch.setenv("WORK_HARNESS_WORKFLOW_ENGINE", "blackboard")
    async with build_runtime(
        ROOT, checkpoint_backend="sqlite", provider_store_backend="memory"
    ) as second:
        manager = RunManager(second)
        try:
            assert await manager.recover_incomplete() == [run_id]
            result = await manager.wait(run_id)
            assert result.status == "succeeded"
            assert model_calls == calls_before
            board = await (await second.blackboard_store()).snapshot(run_id)
            assert (
                len(
                    [
                        item
                        for item in board.objects
                        if isinstance(item, Hint) and item.topic == "task"
                    ]
                )
                == 1
            )
        finally:
            await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ("legacy", "blackboard"))
async def test_configuration_mismatch_preserves_pending_work_for_a_compatible_runtime(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with build_runtime(ROOT, checkpoint_backend="memory") as runtime:
        original = RunManager(runtime)
        monkeypatch.setattr(original, "_spawn_execute", lambda *args, **kwargs: None)
        request = draft_request() if engine == "blackboard" else TaskRequest(goal="Draft a brief")
        run_id = await original.start(request)
        record = await runtime.run_store.get(run_id)
        prompts = runtime.prompt_registry
        runtime.prompt_registry = PromptRegistry(
            ROOT / "prompts", overrides={"worker": "changed prompt"}
        )
        manager = RunManager(runtime)
        try:
            assert await manager.recover_incomplete() == []
            current = await runtime.run_store.get(run_id)
            assert current.status == "pending"
            assert current.execution == record.execution
            assert current.lease_owner is None
            runtime.prompt_registry = prompts
            assert await manager.recover_incomplete() == [run_id]
            assert (await manager.wait(run_id)).status == "succeeded"
        finally:
            await manager.close()
            await original.close()


@pytest.mark.asyncio
async def test_delivery_verification_does_not_satisfy_unverified_external_claims() -> None:
    request = draft_request(
        blackboard={
            "acceptance": [
                {
                    "id": "accuracy",
                    "description": "Verify an external claim",
                    "claim_type": "derived",
                    "claim_key": "external.answer",
                    "required_checks": ["external_verified"],
                }
            ]
        }
    )
    async with build_runtime(ROOT, checkpoint_backend="memory") as runtime:
        manager = RunManager(runtime)
        try:
            run_id = await manager.start(request)
            result = await manager.wait(run_id)
            assert result.status != "succeeded"
            assert "Verify an external claim" in result.output or "accuracy" in result.output
            board = await (await runtime.blackboard_store()).snapshot(run_id)
            assert all(
                item.claim_type == "delivery" for item in board.objects if isinstance(item, Fact)
            )
        finally:
            await manager.close()


@pytest.mark.asyncio
async def test_blackboard_admission_requires_typed_acceptance_and_supported_persistence() -> None:
    async with build_runtime(ROOT, checkpoint_backend="memory") as runtime:
        manager = RunManager(runtime)
        runtime.config.runtime.workflow.engine = "blackboard"
        with pytest.raises(ValueError, match="acceptance"):
            await manager.start(TaskRequest(goal="Do something"))
        runtime.config.runtime.execution.role = "api"
        with pytest.raises(ValueError, match="distributed"):
            await manager.start(draft_request())
        assert await runtime.run_store.list() == []
        runtime.config.runtime.execution.role = "embedded"
        await manager.close()


@pytest.mark.asyncio
async def test_cancel_before_initialization_seals_the_board(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with build_runtime(ROOT, checkpoint_backend="memory") as runtime:
        manager = RunManager(runtime)
        monkeypatch.setattr(manager, "_spawn_execute", lambda *args, **kwargs: None)
        run_id = await manager.start(draft_request())
        await manager.cancel(run_id)
        result = await manager.get(run_id)
        assert result.status == RunStatus.CANCELLED
        assert result.engine == "blackboard"
        board = await (await runtime.blackboard_store()).snapshot(run_id)
        assert board.finalization.status == "cancelled"
        assert len(board.objects) == 1
        assert await manager.recover_incomplete() == []
        await manager.close()


@pytest.mark.asyncio
async def test_api_accepts_explicit_blackboard_and_reports_invalid_input(
    sqlite_paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WORK_HARNESS_PROVIDER_STORE_PATH", str(tmp_path / "providers.sqlite"))
    monkeypatch.setenv("WORK_HARNESS_WORKFLOW_ENGINE", "blackboard")
    app = create_app(ROOT)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            invalid = await client.post("/v1/runs", json={"goal": "No acceptance"})
            assert invalid.status_code == 422
            response = await client.post("/v1/runs", json=draft_request().model_dump(mode="json"))
            assert response.status_code == 202
            run_id = response.json()["run_id"]
            async with asyncio.timeout(5):
                while True:
                    result = await client.get(f"/v1/runs/{run_id}")
                    assert result.status_code == 200
                    if result.json()["status"] not in {"pending", "running"}:
                        break
                    await asyncio.sleep(0.01)
            assert result.json()["status"] == "succeeded"
            assert result.json()["engine"] == "blackboard"


@pytest.mark.asyncio
async def test_completed_blackboard_remains_readable_when_execution_settings_change() -> None:
    async with build_runtime(ROOT, checkpoint_backend="memory") as runtime:
        manager = RunManager(runtime)
        run_id = await manager.start(draft_request())
        original = await manager.wait(run_id)
        runtime.config.runtime.workflow.blackboard.agent_roles = ["removed-role"]
        runtime.prompt_registry = PromptRegistry(
            ROOT / "prompts", overrides={"worker": "new prompt"}
        )
        assert await manager.get(run_id) == original
        await manager.close()


@pytest.mark.asyncio
async def test_execution_version_binds_resolved_provider_targets_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_app_config(ROOT)
    config.models.providers["mock"].base_url_env = "WORK_HARNESS_TEST_MOCK_TARGET"
    monkeypatch.setattr(
        "work_harness.runtime.load_app_config", lambda _: config.model_copy(deep=True)
    )
    monkeypatch.setenv("WORK_HARNESS_TEST_MOCK_TARGET", "https://first.invalid/v1")
    async with build_runtime(ROOT, checkpoint_backend="memory") as runtime:
        manager = RunManager(runtime)
        original = manager._execution_version()
        runtime.config.models.providers["mock"].set_runtime_api_key("synthetic-test-secret")
        assert manager._execution_version() == original
        monkeypatch.setenv("WORK_HARNESS_TEST_MOCK_TARGET", "https://second.invalid/v1")
        assert manager._execution_version() == original
        assert "synthetic-test-secret" not in original.model_dump_json()
    async with build_runtime(ROOT, checkpoint_backend="memory") as restarted:
        assert RunManager(restarted)._execution_version() != original


@pytest.mark.asyncio
async def test_goal_validation_happens_before_admission_and_normalizes_whitespace() -> None:
    async with build_runtime(ROOT, checkpoint_backend="memory") as runtime:
        manager = RunManager(runtime)
        try:
            with pytest.raises(ValueError):
                await manager.start(draft_request(goal="   "))
            assert await runtime.run_store.list() == []
            request = draft_request(goal="  Write a brief  ")
            run_id = await manager.start(request)
            assert (await manager.wait(run_id)).status == "succeeded"
            board = await (await runtime.blackboard_store()).snapshot(run_id)
            assert board.by_id()[board.root_intent_id].objective == "Write a brief"
        finally:
            await manager.close()


@pytest.mark.asyncio
async def test_changed_board_location_does_not_recreate_an_existing_run(
    sqlite_paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with build_runtime(
        ROOT, checkpoint_backend="sqlite", provider_store_backend="memory"
    ) as first:
        manager = RunManager(first)
        monkeypatch.setattr(manager, "_spawn_execute", lambda *args, **kwargs: None)
        run_id = await manager.start(draft_request())
        original = await first.run_store.get(run_id)
        await manager.close()
    monkeypatch.setenv("WORK_HARNESS_BLACKBOARD_PATH", str(tmp_path / "different.sqlite"))
    async with build_runtime(
        ROOT, checkpoint_backend="sqlite", provider_store_backend="memory"
    ) as second:
        manager = RunManager(second)
        try:
            assert await manager.recover_incomplete() == []
            assert (await second.run_store.get(run_id)).execution == original.execution
            assert (await second.run_store.get(run_id)).status == "pending"
            with pytest.raises(BoardNotFoundError):
                await (await second.blackboard_store()).snapshot(run_id)
        finally:
            await manager.close()


@pytest.mark.asyncio
async def test_running_cancel_revokes_source_write_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    original = MockProvider.complete

    async def held(self, request, *, model):
        started.set()
        await release.wait()
        return await original(self, request, model=model)

    monkeypatch.setattr(MockProvider, "complete", held)
    async with build_runtime(ROOT, checkpoint_backend="memory") as runtime:
        manager = RunManager(runtime)
        try:
            run_id = await manager.start(draft_request())
            await asyncio.wait_for(started.wait(), 5)
            engine = manager._blackboard_engines[run_id]
            writer = await engine.kernel.writer(run_id)
            await manager.cancel(run_id)
            assert (await manager.get(run_id)).status == "cancelled"
            with pytest.raises(BoardClosedError):
                await engine.kernel.store.check_writer(writer)
            board = await engine.kernel.store.snapshot(run_id)
            assert not any(isinstance(item, Fact) for item in board.objects)
        finally:
            release.set()
            await manager.close()
