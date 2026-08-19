from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from work_harness.config.models import ProviderConfig
from work_harness.domain.models import utc_now


class ProviderProfileSpec(BaseModel):
    """Persistable Provider settings; credential values use the store's secret channel."""

    model_config = ConfigDict(extra="forbid")

    config: ProviderConfig
    model_bindings: dict[str, str] = Field(default_factory=dict)
    agent_bindings: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_model_bindings(self) -> ProviderProfileSpec:
        invalid = {
            binding: model
            for bindings in (self.model_bindings, self.agent_bindings)
            for binding, model in bindings.items()
            if not binding.strip() or not model.strip()
        }
        if invalid:
            raise ValueError("provider bindings cannot contain blank names")
        unavailable = (
            set(self.model_bindings.values()) | set(self.agent_bindings.values())
        ) - set(self.config.allowed_models)
        if unavailable:
            raise ValueError(
                "provider model bindings must use allowlisted models: "
                f"{sorted(unavailable)}"
            )
        return self


class ProviderProfileRecord(ProviderProfileSpec):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    credential_configured: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ProviderProfileStoreNotFoundError(KeyError):
    pass


class ProviderProfileStore(Protocol):
    backend: str

    async def initialize(self) -> None: ...

    async def close(self) -> None: ...

    async def upsert(
        self,
        name: str,
        profile: ProviderProfileSpec,
        *,
        api_key: str | None = None,
        clear_api_key: bool = False,
    ) -> ProviderProfileRecord: ...

    async def get(self, name: str) -> ProviderProfileRecord | None: ...

    async def list(self) -> list[ProviderProfileRecord]: ...

    async def get_api_key(self, name: str) -> str | None: ...

    async def delete(self, name: str) -> None: ...
