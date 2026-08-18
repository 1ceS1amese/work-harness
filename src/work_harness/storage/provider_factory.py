from __future__ import annotations

from pathlib import Path

from work_harness.config.models import ProviderStoreConfig
from work_harness.storage.provider_base import ProviderProfileStore
from work_harness.storage.provider_memory import MemoryProviderProfileStore
from work_harness.storage.provider_sqlite import SQLiteProviderProfileStore


def build_provider_profile_store(
    config: ProviderStoreConfig,
    project_root: Path,
    *,
    backend: str | None = None,
) -> ProviderProfileStore:
    selected = backend or config.backend
    if selected == "memory":
        return MemoryProviderProfileStore()
    if selected != "sqlite":
        raise ValueError(f"unsupported provider store backend: {selected}")
    path = Path(config.path)
    if not path.is_absolute():
        path = project_root / path
    return SQLiteProviderProfileStore(path)
