from pathlib import Path

import pytest
from pydantic import ValidationError

from work_harness.config.loader import default_project_root, load_app_config
from work_harness.config.models import ProviderConfig


def test_default_project_root_contains_runtime_assets() -> None:
    root = default_project_root()
    assert (root / "config" / "runtime.yaml").is_file()
    assert (root / "prompts" / "planner.md").is_file()
    assert (root / "skills" / "repository-analysis" / "SKILL.md").is_file()

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_configuration_references_are_valid() -> None:
    config = load_app_config(PROJECT_ROOT)
    assert config.models.profile == "local"
    assert "planner-strong" in config.models.logical_models
    assert config.runtime.max_repair_rounds == 1
    assert config.runtime.run_max_concurrency == 4
    assert config.runtime.review_strategy == "adaptive"
    assert config.runtime.deep_review_task_types == {"research", "decision"}
    assert config.runtime.run_store.backend == "sqlite"
    assert config.runtime.run_store.path == "var/runs.sqlite"
    assert config.runtime.provider_store.path == "var/providers.sqlite"
    assert config.models.providers["compatible"].protocol_variant == "openai"
    assert config.tools.tools["echo_read"].idempotent is True
    assignable = [role for role in config.agents.agents.values() if role.assignable]
    assert [role.display_name for role in assignable] == [
        "智能成员一",
        "智能成员二",
        "智能成员三",
        "智能成员四",
    ]
    assert len({role.logical_model for role in assignable}) == 1


def test_production_profile_disables_mock_and_enables_auth(monkeypatch) -> None:
    monkeypatch.setenv("WORK_HARNESS_PROFILE", "production")
    config = load_app_config(PROJECT_ROOT)

    assert config.models.providers["mock"].enabled is False
    assert config.models.providers["compatible"].enabled is True
    assert config.api.auth_enabled is True
    assert config.api.expose_docs is False
    assert config.runtime.trace.sample_rate == 0.1
    assert all(
        logical.candidates[0].provider == "compatible"
        for logical in config.models.logical_models.values()
    )


def test_provider_options_reject_embedded_credentials() -> None:
    with pytest.raises(ValidationError, match="use api_key_env"):
        ProviderConfig(
            adapter="openai_compatible",
            enabled=False,
            options={"headers": {"authorization": "Bearer secret"}},
        )

    with pytest.raises(ValidationError, match="base_url cannot contain credentials"):
        ProviderConfig(
            adapter="openai_compatible",
            enabled=False,
            base_url="https://user:password@api.example.test/v1",
        )
