"""Restore actual persisted runs, then roll back admission without changing their identity."""

from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import closing

import pytest
from test_blackboard_inputs import confirmation, human_request
from test_blackboard_runtime import ROOT, draft_request

from work_harness.blackboard.models import Fact
from work_harness.blackboard.protocol import BlackboardProcessLockError
from work_harness.config.models import ProviderConfig
from work_harness.domain.models import TaskRequest
from work_harness.runtime import build_runtime
from work_harness.service import RunManager
from work_harness.storage.backup import create_sqlite_backup, restore_sqlite_backup
from work_harness.storage.blackboard_factory import build_verification_receipt_store
from work_harness.storage.provider_base import ProviderProfileSpec
from work_harness.storage.settings import ApplicationSettings


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "original"
    for directory in ("config", "prompts", "skills"):
        shutil.copytree(ROOT / directory, root / directory)
    for variable in (
        "WORK_HARNESS_CHECKPOINT_PATH",
        "WORK_HARNESS_RUN_STORE_PATH",
        "WORK_HARNESS_PROVIDER_STORE_PATH",
        "WORK_HARNESS_BLACKBOARD_PATH",
        "WORK_HARNESS_SETTINGS_PATH",
        "WORK_HARNESS_WORKFLOW_ENGINE",
        "WORK_HARNESS_ACCEPT_NEW_BLACKBOARD_RUNS",
    ):
        monkeypatch.delenv(variable, raising=False)
    return root


async def test_restore_history_evidence_and_resume_after_admission_rollback(
    project, tmp_path, monkeypatch
):
    secret = "test-only-encrypted-credential"
    settings = ApplicationSettings(prompts={"planner": "Keep the plan short and bounded."})
    async with build_runtime(project) as runtime:
        await runtime.provider_store.upsert(
            "unused",
            ProviderProfileSpec(
                config=ProviderConfig(
                    adapter="mock",
                    enabled=False,
                    allowed_models=["unused"],
                    credential_source="stored",
                )
            ),
            api_key=secret,
        )
        await runtime.settings_store.save(settings)
    # Reload before admission so the execution fingerprint includes persisted settings.
    async with build_runtime(project) as runtime:
        runtime.config.runtime.workflow.engine = "blackboard"
        manager = RunManager(runtime)
        try:
            complete_id = await manager.start(draft_request(engine=None))
            completed = await manager.wait(complete_id)
            assert completed.status == "succeeded"
            request = human_request().model_copy(
                update={"engine": None, "idempotency_key": "waiting"}
            )
            waiting_id = await manager.start(request)
            waiting = await manager.wait(waiting_id)
            assert waiting.status == "waiting_human"
            execution = (await runtime.run_store.get(waiting_id)).execution
            board = await (await runtime.blackboard_store()).snapshot(complete_id)
            checkpoint = await runtime.checkpointer.aget_tuple(
                {"configurable": {"thread_id": waiting_id}}
            )
            assert checkpoint is not None
        finally:
            await manager.close()
    bundle = create_sqlite_backup(project / "var", tmp_path / "bundle", project_root=project)
    assert all(secret.encode() not in p.read_bytes() for p in bundle.rglob("*") if p.is_file())
    restored = restore_sqlite_backup(bundle, tmp_path / "restored")
    # Equality covers every revision, event, receipt, attachment and usage ledger row.
    for name in ("blackboard.sqlite", "checkpoints.sqlite", "runs.sqlite"):
        with (
            closing(sqlite3.connect(project / "var" / name)) as before,
            closing(sqlite3.connect(restored / "var" / name)) as after,
        ):
            assert list(before.iterdump()) == list(after.iterdump())
    monkeypatch.setenv("WORK_HARNESS_ACCEPT_NEW_BLACKBOARD_RUNS", "false")
    monkeypatch.setenv("WORK_HARNESS_WORKFLOW_ENGINE", "legacy")
    async with build_runtime(restored) as runtime:
        assert runtime.config.runtime.workflow.engine == "legacy"
        assert not runtime.config.runtime.workflow.accept_new_blackboard_runs
        assert await runtime.settings_store.get() == settings
        assert await runtime.provider_store.get_api_key("unused") == secret
        manager = RunManager(runtime)
        try:
            assert await manager.get(complete_id) == completed
            store = await runtime.blackboard_store()
            assert await store.snapshot(complete_id) == board
            attachments = [ref for obj in board.objects for ref in obj.attachments]
            assert attachments
            for ref in attachments:
                assert (await store.content(complete_id, ref)).body
            receipts = build_verification_receipt_store(store)
            for fact in [obj for obj in board.objects if isinstance(obj, Fact)]:
                assert (
                    await receipts.get(complete_id, fact.verification.receipt_id)
                    == fact.verification
                )
            assert await manager.start(request) == waiting_id
            with pytest.raises(ValueError, match="new blackboard runs are disabled"):
                await manager.start(draft_request())
            assert (await runtime.run_store.get(waiting_id)).execution == execution
            await manager.submit_blackboard_input(waiting_id, await confirmation(store, waiting_id))
            resumed = await manager.wait(waiting_id)
            assert resumed.status == "succeeded" and resumed.engine == "blackboard"
            assert resumed.usage == waiting.usage
            new_id = await manager.start(TaskRequest(goal="Draft a launch brief"))
            legacy = await manager.wait(new_id)
            assert legacy.status == "succeeded" and legacy.engine == "legacy"
            assert (await runtime.run_store.get(waiting_id)).execution == execution
        finally:
            await manager.close()


async def test_backup_refuses_live_runtime_and_incomplete_or_damaged_bundle(project, tmp_path):
    target = tmp_path / "bundle"
    async with build_runtime(project) as runtime:
        manager = RunManager(runtime)
        try:
            identifier = await manager.start(draft_request())
            await manager.wait(identifier)
            with pytest.raises(BlackboardProcessLockError, match="in use"):
                create_sqlite_backup(project / "var", target, project_root=project)
            assert not target.exists()
        finally:
            await manager.close()
    board_db = project / "var" / "blackboard.sqlite"
    board_db.rename(board_db.with_suffix(".saved"))
    with pytest.raises(ValueError, match="blackboard.sqlite is missing"):
        create_sqlite_backup(project / "var", target, project_root=project)
    board_db.with_suffix(".saved").rename(board_db)
    create_sqlite_backup(project / "var", target, project_root=project)
    with pytest.raises(FileExistsError):
        create_sqlite_backup(project / "var", target, project_root=project)
    with (target / "var" / "blackboard.sqlite").open("ab") as stream:
        stream.write(b"damaged")
    with pytest.raises(ValueError, match="damaged"):
        restore_sqlite_backup(target, tmp_path / "bad-restore")
    assert not (tmp_path / "bad-restore").exists()
    with pytest.raises(FileExistsError):
        restore_sqlite_backup(target, project)


async def test_legacy_only_bundle_and_path_traversal_rejection(project, tmp_path):
    async with build_runtime(project) as runtime:
        manager = RunManager(runtime)
        try:
            identifier = await manager.start(
                TaskRequest(goal="Draft a short brief", engine="legacy")
            )
            await manager.wait(identifier)
            with pytest.raises(BlackboardProcessLockError, match="in use"):
                create_sqlite_backup(project / "var", tmp_path / "live", project_root=project)
        finally:
            await manager.close()
    bundle = create_sqlite_backup(project / "var", tmp_path / "bundle", project_root=project)
    restored = restore_sqlite_backup(bundle, tmp_path / "restored")
    assert not (restored / "var" / "blackboard.sqlite").exists()
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"][0]["name"] = "../outside"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="invalid backup member"):
        restore_sqlite_backup(bundle, tmp_path / "unsafe")
