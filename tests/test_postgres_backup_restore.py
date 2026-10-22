"""Restore a real pg_dump into an isolated database and resume through API/Worker."""

from __future__ import annotations

import asyncio
import os
import shutil
from contextlib import AsyncExitStack, asynccontextmanager
from uuid import uuid4

import pytest
from psycopg import AsyncConnection, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from test_backup_restore import project as project
from test_blackboard_inputs import confirmation, human_request
from test_blackboard_postgres_runtime import distributed_database as distributed_database
from test_blackboard_postgres_runtime import mock_providers, wait_result
from test_blackboard_runtime import draft_request

from work_harness.domain.models import TaskRequest
from work_harness.runtime import build_runtime
from work_harness.service import RunManager
from work_harness.storage.backup import create_postgres_backup, restore_backup
from work_harness.storage.blackboard_factory import (
    build_activation_store,
    build_verification_receipt_store,
)


def pg_environment(dsn):
    values = conninfo_to_dict(dsn)
    result = dict(os.environ)
    for name, value in values.items():
        result["PG" + name.upper() if name != "dbname" else "PGDATABASE"] = value
    return result


@asynccontextmanager
async def managers(root, dsn, monkeypatch):
    monkeypatch.setenv("WORK_HARNESS_DATABASE_URL", dsn)
    async with AsyncExitStack() as stack:
        result = []
        for role in ("api", "worker"):
            monkeypatch.setenv("WORK_HARNESS_EXECUTION_ROLE", role)
            runtime = await stack.enter_async_context(
                build_runtime(root, provider_adapters=mock_providers())
            )
            manager = RunManager(runtime)
            stack.push_async_callback(manager.close)
            result.append(manager)
        # The Worker must advertise the same version before API admission.
        await result[1].initialize()
        await result[0].initialize()
        yield result


async def test_postgres_dump_restore_preserves_evidence_and_waiting_run(
    project, distributed_database, tmp_path, monkeypatch
):
    if not all(shutil.which(name) for name in ("pg_dump", "psql")):
        pytest.skip("PostgreSQL client utilities are required")
    # Both API and Worker use the same file assets; all calls use deterministic models.
    dsn = distributed_database
    request = human_request().model_copy(update={"idempotency_key": "restored-human-run"})
    async with managers(project, dsn, monkeypatch) as (api, worker):
        complete_id = await api.start(draft_request())
        completed = await wait_result(api, complete_id)
        assert completed.status == "succeeded"
        waiting_id = await api.start(request)
        waiting = await wait_result(api, waiting_id)
        assert waiting.status == "waiting_human"
        for _ in range(200):
            if (await api.runtime.run_store.get(waiting_id)).status == "waiting_human":
                break
            await asyncio.sleep(0.025)
        else:
            raise AssertionError("waiting run was not settled in the queue")
        execution = (await api.runtime.run_store.get(waiting_id)).execution
        store = await api.runtime.blackboard_store()
        board = await store.snapshot(complete_id)
        events = await store.events(complete_id)
        ledger = build_activation_store(store)
        usage = await ledger.summary(complete_id)
        activations = await ledger.records(complete_id)
        checkpoint = await api.runtime.checkpointer.aget_tuple(
            {"configurable": {"thread_id": waiting_id}}
        )
        assert checkpoint is not None and usage.tokens > 0
    dump = tmp_path / "database.sql"
    with dump.open("wb") as stream:
        process = await asyncio.create_subprocess_exec(
            "pg_dump",
            "--no-owner",
            "--no-privileges",
            env=pg_environment(dsn),
            stdout=stream,
            stderr=asyncio.subprocess.PIPE,
        )
        _, error = await process.communicate()
    assert process.returncode == 0, error.decode()
    bundle = create_postgres_backup(
        dump, tmp_path / "bundle", project_root=project, local_data=project / "var"
    )
    restored = restore_backup(bundle, tmp_path / "restored")
    database_name = "bb_restore_" + uuid4().hex
    admin = await AsyncConnection.connect(dsn, autocommit=True)
    restored_dsn = make_conninfo(dsn, dbname=database_name)
    try:
        await admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
        process = await asyncio.create_subprocess_exec(
            "psql",
            "-X",
            "--set=ON_ERROR_STOP=1",
            "--single-transaction",
            "--file",
            str(restored / "database.sql"),
            env=pg_environment(restored_dsn),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, error = await process.communicate()
        assert process.returncode == 0, error.decode()
        monkeypatch.setenv("WORK_HARNESS_WORKFLOW_ENGINE", "legacy")
        monkeypatch.setenv("WORK_HARNESS_ACCEPT_NEW_BLACKBOARD_RUNS", "false")
        async with managers(restored, restored_dsn, monkeypatch) as (api, worker):
            store = await api.runtime.blackboard_store()
            assert await api.get(complete_id) == completed
            assert await store.snapshot(complete_id) == board
            assert await store.events(complete_id) == events
            ledger = build_activation_store(store)
            assert await ledger.summary(complete_id) == usage
            assert await ledger.records(complete_id) == activations
            receipts = build_verification_receipt_store(store)
            for obj in board.objects:
                for ref in obj.attachments:
                    assert (await store.content(complete_id, ref)).body
                if obj.kind == "fact":
                    assert (
                        await receipts.get(complete_id, obj.verification.receipt_id)
                        == obj.verification
                    )
            assert await api.start(request) == waiting_id
            with pytest.raises(ValueError, match="new blackboard runs are disabled"):
                await api.start(draft_request())
            await api.submit_blackboard_input(waiting_id, await confirmation(store, waiting_id))
            resumed = await wait_result(api, waiting_id)
            assert resumed.engine == "blackboard" and resumed.status == "succeeded"
            assert resumed.usage == waiting.usage
            assert (await api.runtime.run_store.get(waiting_id)).execution == execution
            new_id = await api.start(TaskRequest(goal="Draft a launch brief"))
            new = await wait_result(api, new_id)
            assert new.engine == "legacy" and new.status == "succeeded"
    finally:
        try:
            await admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(database_name)
                )
            )
        finally:
            await admin.close()
