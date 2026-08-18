from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from work_harness.config.models import ModelsConfig
from work_harness.domain.enums import AgentFunction
from work_harness.domain.models import SkillSelection
from work_harness.skills.models import (
    ResolvedSkillContext,
    SkillDefinition,
    SkillDescriptor,
    SkillManifest,
)
from work_harness.tools.registry import ToolRegistry


class SkillError(ValueError):
    pass


class SkillNotFoundError(SkillError):
    pass


class SkillCompatibilityError(SkillError):
    pass


class SkillRegistry:
    """Load project-local SKILL.md packages and resolve safe compositions."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._skills: dict[str, SkillDefinition] = {}
        self._models: ModelsConfig | None = None

    def load(self) -> None:
        self._skills.clear()
        if not self.root.is_dir():
            return
        for skill_path in sorted(self.root.iterdir()):
            manifest_path = skill_path / "agents" / "work-harness.yaml"
            if not skill_path.is_dir() or not manifest_path.is_file():
                continue
            definition = self._load_one(skill_path, manifest_path)
            if definition.skill_id in self._skills:
                raise SkillError(f"duplicate skill id: {definition.skill_id}")
            self._skills[definition.skill_id] = definition

    def _load_one(self, skill_path: Path, manifest_path: Path) -> SkillDefinition:
        metadata, instructions = self._read_skill_markdown(skill_path / "SKILL.md")
        skill_id = str(metadata.get("name", ""))
        if skill_id != skill_path.name:
            raise SkillError(f"skill name {skill_id!r} must match directory {skill_path.name!r}")
        description = metadata.get("description")
        if not isinstance(description, str) or not description.strip():
            raise SkillError(f"skill {skill_id} requires a description")
        raw_manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        manifest = SkillManifest.model_validate(raw_manifest)
        if not instructions.strip():
            raise SkillError(f"skill {skill_id} has empty instructions")
        if len(instructions) > 20_000:
            raise SkillError(f"skill {skill_id} instructions exceed 20000 characters")
        return SkillDefinition(
            skill_id=skill_id,
            description=description.strip(),
            instructions=instructions.strip(),
            manifest=manifest,
            path=skill_path,
        )

    @staticmethod
    def _read_skill_markdown(path: Path) -> tuple[dict[str, Any], str]:
        if not path.is_file():
            raise SkillError(f"missing SKILL.md: {path}")
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---\n") or "\n---\n" not in text[4:]:
            raise SkillError(f"invalid SKILL.md frontmatter: {path}")
        raw_metadata, body = text[4:].split("\n---\n", 1)
        metadata = yaml.safe_load(raw_metadata)
        if not isinstance(metadata, dict):
            raise SkillError(f"skill frontmatter must be a mapping: {path}")
        unexpected = set(metadata) - {"name", "description"}
        if unexpected:
            raise SkillError(f"skill frontmatter has unsupported keys: {sorted(unexpected)}")
        return metadata, body

    def validate_compatibility(
        self,
        models: ModelsConfig,
        tools: ToolRegistry,
        *,
        assignable_roles: list[str],
    ) -> None:
        self._models = models
        for skill in self._skills.values():
            manifest = skill.manifest
            logical = models.logical_models.get(manifest.logical_model)
            if logical is None:
                raise SkillCompatibilityError(
                    f"skill {skill.skill_id} references unknown logical model "
                    f"{manifest.logical_model}"
                )
            unknown_tools = set(manifest.allowed_tools) - set(tools.names)
            if unknown_tools:
                raise SkillCompatibilityError(
                    f"skill {skill.skill_id} references unknown tools: {sorted(unknown_tools)}"
                )
            for tool_name in manifest.allowed_tools:
                allowed_roles = set(tools.config.tools[tool_name].allowed_roles)
                missing_roles = set(assignable_roles) - allowed_roles
                if missing_roles:
                    raise SkillCompatibilityError(
                        f"skill {skill.skill_id} tool {tool_name} is unavailable to "
                        f"roles: {sorted(missing_roles)}"
                    )
            candidates = {candidate.provider for candidate in logical.candidates}
            unknown_providers = set(manifest.compatible_providers) - set(models.providers)
            if unknown_providers:
                raise SkillCompatibilityError(
                    f"skill {skill.skill_id} references unknown providers: "
                    f"{sorted(unknown_providers)}"
                )
            eligible = []
            for provider_name in manifest.compatible_providers:
                provider = models.providers[provider_name]
                missing = manifest.required_capabilities - provider.capabilities
                if missing:
                    raise SkillCompatibilityError(
                        f"skill {skill.skill_id} provider {provider_name} lacks "
                        f"capabilities: {sorted(missing)}"
                    )
                if provider.enabled and provider_name in candidates:
                    eligible.append(provider_name)
            if not eligible:
                raise SkillCompatibilityError(
                    f"skill {skill.skill_id} has no enabled compatible provider candidate"
                )

    def validate_selections(self, selections: list[SkillSelection]) -> None:
        for selection in selections:
            skill = self.get(selection.skill_id)
            if selection.provider is not None:
                eligible = self._eligible_providers(skill)
                if selection.provider not in eligible:
                    raise SkillCompatibilityError(
                        f"provider {selection.provider} cannot run skill "
                        f"{selection.skill_id}; choose one of {eligible}"
                    )
        for function in AgentFunction:
            applicable = [
                selection.skill_id
                for selection in selections
                if self._applies(self.get(selection.skill_id), function)
            ]
            if applicable:
                self.resolve(selections, applicable, function=function)

    def resolve(
        self,
        selections: list[SkillSelection],
        skill_ids: list[str],
        *,
        function: AgentFunction | str | None,
    ) -> ResolvedSkillContext:
        selected = {item.skill_id: item for item in selections}
        requested_ids = skill_ids or [
            item.skill_id for item in selections if self._applies(self.get(item.skill_id), function)
        ]
        unknown = set(requested_ids) - set(selected)
        if unknown:
            raise SkillCompatibilityError(
                f"subtask references unselected skills: {sorted(unknown)}"
            )
        definitions = [self.get(skill_id) for skill_id in requested_ids]
        for definition in definitions:
            if not self._applies(definition, function):
                raise SkillCompatibilityError(
                    f"skill {definition.skill_id} does not apply to function {function}"
                )
        if not definitions:
            return ResolvedSkillContext(
                skill_ids=(),
                instructions="",
                logical_model=None,
                required_capabilities=frozenset(),
                allowed_tools=(),
                provider_preferences=(),
                provider_allowlist=frozenset(),
            )
        logical_models = {item.manifest.logical_model for item in definitions}
        if len(logical_models) != 1:
            raise SkillCompatibilityError(
                "composed skills must use the same logical model for one subtask"
            )
        eligible_sets = [set(self._eligible_providers(item)) for item in definitions]
        common_eligible = set.intersection(*eligible_sets)
        overrides = {
            selected[item.skill_id].provider
            for item in definitions
            if selected[item.skill_id].provider is not None
        }
        if len(overrides) > 1:
            raise SkillCompatibilityError(
                "composed skills select different providers for one subtask"
            )
        if overrides:
            chosen = next(iter(overrides))
            if chosen not in common_eligible:
                raise SkillCompatibilityError(
                    f"selected provider {chosen} is not compatible with all composed skills"
                )
            allowlist = frozenset({chosen})
            preferences = (chosen,)
        else:
            ordered = []
            for item in definitions:
                for provider in item.manifest.preferred_providers:
                    if provider in common_eligible and provider not in ordered:
                        ordered.append(provider)
            for provider in sorted(common_eligible):
                if provider not in ordered:
                    ordered.append(provider)
            allowlist = frozenset(common_eligible)
            preferences = tuple(ordered)
        if not allowlist:
            raise SkillCompatibilityError(
                "composed skills have no common enabled provider candidate"
            )
        tools = tuple(
            dict.fromkeys(tool for item in definitions for tool in item.manifest.allowed_tools)
        )
        required = frozenset(
            capability for item in definitions for capability in item.manifest.required_capabilities
        )
        instructions = "\n\n".join(
            f"# Skill: {item.skill_id}\n{item.instructions}" for item in definitions
        )
        return ResolvedSkillContext(
            skill_ids=tuple(item.skill_id for item in definitions),
            instructions=instructions,
            logical_model=next(iter(logical_models)),
            required_capabilities=required,
            allowed_tools=tools,
            provider_preferences=preferences,
            provider_allowlist=allowlist,
        )

    def planner_context(self, selections: list[SkillSelection]) -> list[dict[str, Any]]:
        return [
            {
                "id": definition.skill_id,
                "description": definition.description,
                "applies_to": sorted(definition.manifest.applies_to),
                "logical_model": definition.manifest.logical_model,
                "required_capabilities": sorted(definition.manifest.required_capabilities),
                "allowed_tools": definition.manifest.allowed_tools,
                "selected_provider": selection.provider,
            }
            for selection in selections
            for definition in [self.get(selection.skill_id)]
        ]

    def descriptors(self) -> list[SkillDescriptor]:
        return [
            SkillDescriptor(
                id=skill.skill_id,
                description=skill.description,
                version=skill.manifest.version,
                logical_model=skill.manifest.logical_model,
                required_capabilities=sorted(skill.manifest.required_capabilities),
                allowed_tools=skill.manifest.allowed_tools,
                compatible_providers=skill.manifest.compatible_providers,
                preferred_providers=skill.manifest.preferred_providers,
                available_providers=self._eligible_providers(skill),
                applies_to=sorted(skill.manifest.applies_to),
            )
            for skill in self._skills.values()
        ]

    def get(self, skill_id: str) -> SkillDefinition:
        try:
            return self._skills[skill_id]
        except KeyError as exc:
            raise SkillNotFoundError(f"unknown skill: {skill_id}") from exc

    def _eligible_providers(self, skill: SkillDefinition) -> list[str]:
        if self._models is None:
            raise RuntimeError("skill compatibility has not been validated")
        logical = self._models.logical_models[skill.manifest.logical_model]
        candidate_providers = {candidate.provider for candidate in logical.candidates}
        return [
            provider_name
            for provider_name in skill.manifest.compatible_providers
            if provider_name in candidate_providers
            and self._models.providers[provider_name].enabled
            and skill.manifest.required_capabilities
            <= self._models.providers[provider_name].capabilities
        ]

    @staticmethod
    def _applies(
        skill: SkillDefinition,
        function: AgentFunction | str | None,
    ) -> bool:
        applies_to = set(skill.manifest.applies_to)
        return not applies_to or function is None or str(function) in applies_to

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._skills)
