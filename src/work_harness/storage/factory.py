from __future__ import annotations

import os
from pathlib import Path

from work_harness.config.models import RunStoreConfig
from work_harness.storage.base import RunStore
from work_harness.storage.memory import MemoryRunStore
from work_harness.storage.postgres import PostgresRunStore
from work_harness.storage.sqlite import SQLiteRunStore


def build_run_store(
    config: RunStoreConfig,
    project_root: Path,
    *,
    backend: str | None = None,
) -> RunStore:
    selected = backend or config.backend
    if selected == "memory":
        return MemoryRunStore()
    if selected == "sqlite":
        path = Path(config.path)
        if not path.is_absolute():
            path = project_root / path
        return SQLiteRunStore(path)
    if selected == "postgres":
        dsn = os.getenv(config.dsn_env, "").strip()
        if not dsn:
            raise ValueError(
                f"PostgreSQL run store requires {config.dsn_env} to be configured"
            )
        return PostgresRunStore(
            dsn,
            pool_min_size=config.pool_min_size,
            pool_max_size=config.pool_max_size,
        )
    raise ValueError(f"unsupported run store backend: {selected}")
