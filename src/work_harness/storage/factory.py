from __future__ import annotations

from pathlib import Path

from work_harness.config.models import RunStoreConfig
from work_harness.storage.base import RunStore
from work_harness.storage.memory import MemoryRunStore
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
    if selected != "sqlite":
        raise ValueError(f"unsupported run store backend: {selected}")
    path = Path(config.path)
    if not path.is_absolute():
        path = project_root / path
    return SQLiteRunStore(path)
