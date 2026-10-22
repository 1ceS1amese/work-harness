from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo

from work_harness.blackboard.store import BlackboardStore
from work_harness.storage.blackboard_memory import MemoryBlackboardStore
from work_harness.storage.blackboard_postgres import PostgresBlackboardStore
from work_harness.storage.blackboard_sqlite import SQLiteBlackboardStore


@pytest.fixture(autouse=True)
def use_explicit_local_test_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep deterministic Mock Providers confined to automated tests."""

    monkeypatch.setenv("WORK_HARNESS_PROFILE", "local")


@pytest.fixture
def legacy_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit compatibility mode for tests of the pre-blackboard workflow."""
    monkeypatch.setenv("WORK_HARNESS_WORKFLOW_ENGINE", "legacy")


@pytest.fixture
async def postgres_blackboard_factory():
    """Each test owns a schema; additional clients share it without touching other data."""
    stores = []
    admin = None
    dsn = None
    schema = f"bb_test_{uuid4().hex}"

    async def create(*, migrate=True, initialize=True):
        nonlocal admin, dsn
        if admin is None:
            base = os.getenv("WORK_HARNESS_TEST_DATABASE_URL", "").strip()
            if not base:
                pytest.skip("WORK_HARNESS_TEST_DATABASE_URL is not configured")
            admin = await AsyncConnection.connect(base, autocommit=True)
            await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            dsn = make_conninfo(base, options=f"-csearch_path={schema}")
        store = PostgresBlackboardStore(dsn)
        stores.append(store)
        if migrate:
            await store.setup()
        if initialize:
            await store.initialize()
        return store

    try:
        yield create
    finally:
        for store in stores:
            await store.close()
        if admin is not None:
            try:
                await admin.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
                )
            finally:
                await admin.close()


@pytest.fixture(params=("memory", "sqlite", "postgres"))
async def blackboard_store(
    request: pytest.FixtureRequest, tmp_path: Path, postgres_blackboard_factory
) -> AsyncIterator[BlackboardStore]:
    if request.param == "postgres":
        yield await postgres_blackboard_factory()
        return
    store = (
        MemoryBlackboardStore()
        if request.param == "memory"
        else SQLiteBlackboardStore(tmp_path / "blackboard.sqlite")
    )
    await store.initialize()
    try:
        yield store
    finally:
        await store.close()
