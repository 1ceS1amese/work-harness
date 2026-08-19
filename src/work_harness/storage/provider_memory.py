from __future__ import annotations

import asyncio

from work_harness.domain.models import utc_now
from work_harness.storage.provider_base import (
    ProviderProfileRecord,
    ProviderProfileSpec,
    ProviderProfileStoreNotFoundError,
)


class MemoryProviderProfileStore:
    backend = "memory"

    def __init__(self) -> None:
        self._profiles: dict[str, ProviderProfileRecord] = {}
        self._api_keys: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def upsert(
        self,
        name: str,
        profile: ProviderProfileSpec,
        *,
        api_key: str | None = None,
        clear_api_key: bool = False,
    ) -> ProviderProfileRecord:
        async with self._lock:
            if api_key is not None and clear_api_key:
                raise ValueError("cannot set and clear a Provider credential together")
            if clear_api_key:
                self._api_keys.pop(name, None)
            elif api_key is not None:
                self._api_keys[name] = api_key
            now = utc_now()
            existing = self._profiles.get(name)
            record = ProviderProfileRecord(
                name=name,
                config=profile.config,
                model_bindings=profile.model_bindings,
                agent_bindings=profile.agent_bindings,
                credential_configured=name in self._api_keys,
                created_at=existing.created_at if existing else now,
                updated_at=now,
            )
            self._profiles[name] = record
            return record

    async def get(self, name: str) -> ProviderProfileRecord | None:
        async with self._lock:
            return self._profiles.get(name)

    async def list(self) -> list[ProviderProfileRecord]:
        async with self._lock:
            return [self._profiles[name] for name in sorted(self._profiles)]

    async def get_api_key(self, name: str) -> str | None:
        async with self._lock:
            return self._api_keys.get(name)

    async def delete(self, name: str) -> None:
        async with self._lock:
            if self._profiles.pop(name, None) is None:
                raise ProviderProfileStoreNotFoundError(name)
            self._api_keys.pop(name, None)
