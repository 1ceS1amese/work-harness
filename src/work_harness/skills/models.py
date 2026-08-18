from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from work_harness.domain.enums import AgentFunction


class SkillModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class SkillManifest(SkillModel):
    schema_version: int = 1
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    logical_model: str
    required_capabilities: set[str] = Field(default_factory=lambda: {"chat"})
    allowed_tools: list[str] = Field(default_factory=list)
    compatible_providers: list[str] = Field(min_length=1)
    preferred_providers: list[str] = Field(default_factory=list)
    applies_to: set[AgentFunction] = Field(default_factory=set)

    @model_validator(mode="after")
    def validate_provider_preferences(self) -> SkillManifest:
        if len(self.allowed_tools) != len(set(self.allowed_tools)):
            raise ValueError("skill allowed_tools must be unique")
        if len(self.compatible_providers) != len(set(self.compatible_providers)):
            raise ValueError("skill compatible_providers must be unique")
        if len(self.preferred_providers) != len(set(self.preferred_providers)):
            raise ValueError("skill preferred_providers must be unique")
        unknown = set(self.preferred_providers) - set(self.compatible_providers)
        if unknown:
            raise ValueError(f"preferred providers must be compatible: {sorted(unknown)}")
        return self


@dataclass(frozen=True, slots=True)
class SkillDefinition:
    skill_id: str
    description: str
    instructions: str
    manifest: SkillManifest
    path: Path


class SkillDescriptor(SkillModel):
    id: str
    description: str
    version: str
    logical_model: str
    required_capabilities: list[str]
    allowed_tools: list[str]
    compatible_providers: list[str]
    preferred_providers: list[str]
    available_providers: list[str]
    applies_to: list[str]


@dataclass(frozen=True, slots=True)
class ResolvedSkillContext:
    skill_ids: tuple[str, ...]
    instructions: str
    logical_model: str | None
    required_capabilities: frozenset[str]
    allowed_tools: tuple[str, ...]
    provider_preferences: tuple[str, ...]
    provider_allowlist: frozenset[str]
