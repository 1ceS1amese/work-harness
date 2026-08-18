from pathlib import Path

import pytest

from work_harness.domain.models import SkillSelection
from work_harness.runtime import build_runtime
from work_harness.skills import SkillCompatibilityError

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_skill_registry_resolves_tools_and_provider_preferences() -> None:
    async with build_runtime(PROJECT_ROOT, checkpoint_backend="memory") as runtime:
        registry = runtime.skill_registry
        context = registry.resolve(
            [SkillSelection(skill_id="risk-audit")],
            [],
            function="risk",
        )
        descriptors = registry.descriptors()

    assert {item.id for item in descriptors} == {
        "repository-analysis",
        "risk-audit",
    }
    assert context.skill_ids == ("risk-audit",)
    assert context.allowed_tools == ("workspace_read",)
    assert context.provider_preferences[0] == "mock-secondary"
    assert context.provider_allowlist == frozenset({"mock", "mock-secondary"})


@pytest.mark.asyncio
async def test_skill_rejects_disabled_provider_override() -> None:
    async with build_runtime(PROJECT_ROOT, checkpoint_backend="memory") as runtime:
        with pytest.raises(SkillCompatibilityError, match="cannot run skill"):
            runtime.skill_registry.validate_selections(
                [SkillSelection(skill_id="risk-audit", provider="compatible")]
            )


@pytest.mark.asyncio
async def test_skill_rejects_conflicting_provider_composition() -> None:
    async with build_runtime(PROJECT_ROOT, checkpoint_backend="memory") as runtime:
        with pytest.raises(SkillCompatibilityError, match="different providers"):
            runtime.skill_registry.validate_selections(
                [
                    SkillSelection(skill_id="repository-analysis", provider="mock"),
                    SkillSelection(skill_id="risk-audit", provider="mock-secondary"),
                ]
            )
