from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from work_harness.config.models import AppConfig, RuntimeConfig

PROMPT_NAMES = frozenset(
    {"normalizer", "planner", "worker", "reviewer", "judge", "synthesizer"}
)


class EditableRuntimeSettings(BaseModel):
    """User-editable runtime policy; persistence and security paths stay immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_max_concurrency: int = Field(ge=1, le=32)
    # Defaults keep settings.json files written by earlier releases loadable.
    run_queue_capacity: int = Field(default=8, ge=0, le=10_000)
    run_queue_retry_after_seconds: int = Field(default=5, ge=1, le=300)
    worker_max_concurrency: int = Field(ge=1, le=32)
    reviewer_max_concurrency: int = Field(ge=1, le=32)
    review_strategy: Literal["adaptive", "comprehensive", "three_dimensional"]
    deep_review_risk_levels: set[Literal["low", "medium", "high", "critical"]]
    deep_review_task_types: set[
        Literal["general", "planning", "decision", "research", "code"]
    ]
    max_planned_subtasks: int = Field(ge=1, le=20)
    max_repair_rounds: int = Field(ge=0, le=5)
    node_timeout_seconds: float = Field(ge=5, le=600)
    run_timeout_seconds: float = Field(ge=10, le=3600)
    max_artifact_chars: int = Field(ge=100, le=200_000)
    max_tool_result_chars: int = Field(ge=100, le=100_000)
    model_retry_attempts: int = Field(ge=0, le=5)

    @model_validator(mode="before")
    @classmethod
    def read_old_settings(cls, value):
        if isinstance(value, dict):
            return {k: v for k, v in value.items() if k not in {"token_budget", "cost_budget_usd"}}
        return value

    @classmethod
    def from_runtime(cls, runtime: RuntimeConfig) -> EditableRuntimeSettings:
        return cls.model_validate(
            runtime.model_dump(
                include=set(cls.model_fields),
                mode="python",
            )
        )


class ApplicationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    prompts: dict[str, str] = Field(default_factory=dict)
    runtime: EditableRuntimeSettings | None = None

    @field_validator("prompts")
    @classmethod
    def validate_prompts(cls, value: dict[str, str]) -> dict[str, str]:
        unknown = set(value) - PROMPT_NAMES
        if unknown:
            raise ValueError(f"unknown prompt settings: {sorted(unknown)}")
        cleaned = {name: body.strip() for name, body in value.items()}
        invalid = {
            name
            for name, body in cleaned.items()
            if not body or len(body) > 50_000
        }
        if invalid:
            raise ValueError(
                f"prompt settings must contain 1 to 50000 characters: {sorted(invalid)}"
            )
        return cleaned


def apply_application_settings(
    config: AppConfig,
    settings: ApplicationSettings,
) -> AppConfig:
    if settings.runtime is None:
        return config
    payload = config.model_dump(mode="python")
    payload["runtime"].update(settings.runtime.model_dump(mode="python"))
    return AppConfig.model_validate(payload)


def default_settings_path(config: AppConfig) -> Path:
    override = os.getenv("WORK_HARNESS_SETTINGS_PATH", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    provider_path = Path(config.runtime.provider_store.path)
    if not provider_path.is_absolute():
        provider_path = config.project_root / provider_path
    return provider_path.with_name("settings.json")


class ApplicationSettingsStore:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.backend = "memory" if path is None else "file"
        self._value = ApplicationSettings()
        self._write_lock = asyncio.Lock()

    async def initialize(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_file():
            raw = await asyncio.to_thread(self.path.read_text, encoding="utf-8")
            self._value = ApplicationSettings.model_validate_json(raw)

    async def close(self) -> None:
        return None

    async def get(self) -> ApplicationSettings:
        return self._value.model_copy(deep=True)

    async def save(self, value: ApplicationSettings) -> ApplicationSettings:
        async with self._write_lock:
            return await self._save_locked(value)

    async def update(
        self,
        transform: Callable[[ApplicationSettings], ApplicationSettings],
    ) -> ApplicationSettings:
        """Atomically derive and persist settings from the latest value."""

        async with self._write_lock:
            current = self._value.model_copy(deep=True)
            return await self._save_locked(transform(current))

    async def _save_locked(self, value: ApplicationSettings) -> ApplicationSettings:
        self._value = value.model_copy(deep=True)
        if self.path is not None:
            await asyncio.to_thread(self._write_sync, self._value)
        return self._value.model_copy(deep=True)

    def _write_sync(self, value: ApplicationSettings) -> None:
        if self.path is None:
            return
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        payload = json.dumps(
            value.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        temporary.write_text(f"{payload}\n", encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, self.path)
        self.path.chmod(0o600)
