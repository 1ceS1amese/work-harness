from pathlib import Path

import pytest
from pydantic import ValidationError

from work_harness.config.loader import default_project_root, load_app_config
from work_harness.config.models import AppConfig, ProviderConfig, RuntimeConfig


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


def test_unconfigured_default_uses_safe_setup_profile(monkeypatch) -> None:
    monkeypatch.delenv("WORK_HARNESS_PROFILE", raising=False)
    monkeypatch.delenv("MODEL_ID", raising=False)

    config = load_app_config(PROJECT_ROOT)

    assert config.models.profile == "setup"
    assert config.models.providers["mock"].enabled is False
    assert config.models.providers["mock-secondary"].enabled is False
    assert config.models.providers["compatible"].enabled is True
    assert config.api.auth_enabled is False
    assert all(
        role.max_output_tokens == 20_000
        for role in config.agents.agents.values()
        if role.assignable
    )
    assert config.runtime.model_dump()["token_budget"] is None
    assert all(
        len(logical.candidates) == 1
        and logical.candidates[0].provider == "compatible"
        and logical.candidates[0].model == "configured-model"
        for logical in config.models.logical_models.values()
    )


def test_environment_model_id_replaces_first_run_placeholder(monkeypatch) -> None:
    monkeypatch.setenv("WORK_HARNESS_PROFILE", "setup")
    monkeypatch.setenv("MODEL_ID", "deployment-model")

    config = load_app_config(PROJECT_ROOT)

    assert config.models.providers["compatible"].allowed_models == ["deployment-model"]
    assert all(
        logical.candidates[0].model == "deployment-model"
        for logical in config.models.logical_models.values()
    )


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


def test_distributed_profile_pairs_postgres_queue_checkpoint_and_real_provider(
    monkeypatch,
) -> None:
    monkeypatch.setenv("WORK_HARNESS_PROFILE", "distributed")
    monkeypatch.setenv("WORK_HARNESS_EXECUTION_ROLE", "worker")

    config = load_app_config(PROJECT_ROOT)

    assert config.runtime.execution.role == "worker"
    assert config.runtime.run_store.backend == "postgres"
    assert config.runtime.checkpoint.backend == "postgres"
    assert config.runtime.run_store.dsn_env == "WORK_HARNESS_DATABASE_URL"
    assert config.runtime.provider_store.backend == "memory"
    assert config.models.providers["compatible"].enabled is True
    assert config.models.providers["mock"].enabled is False
    assert config.api.auth_enabled is True


def test_distributed_scheduling_environment_is_typed_and_validated(monkeypatch) -> None:
    monkeypatch.setenv("WORK_HARNESS_PROFILE", "distributed")
    monkeypatch.setenv("WORK_HARNESS_RUN_MAX_CONCURRENCY", "7")
    monkeypatch.setenv("WORK_HARNESS_RUN_QUEUE_CAPACITY", "41")
    monkeypatch.setenv("WORK_HARNESS_JOB_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("WORK_HARNESS_EXECUTION_POLL_INTERVAL_SECONDS", "0.75")

    config = load_app_config(PROJECT_ROOT)

    assert config.runtime.run_max_concurrency == 7
    assert config.runtime.run_queue_capacity == 41
    assert config.runtime.execution.job_max_attempts == 5
    assert config.runtime.execution.poll_interval_seconds == 0.75

    monkeypatch.setenv("WORK_HARNESS_HEARTBEAT_INTERVAL_SECONDS", "30")
    monkeypatch.setenv("WORK_HARNESS_RUN_LEASE_SECONDS", "10")
    with pytest.raises(ValidationError, match="heartbeat interval must be shorter"):
        load_app_config(PROJECT_ROOT)


def test_distributed_role_rejects_non_shared_persistence() -> None:
    config = load_app_config(PROJECT_ROOT)
    payload = config.runtime.model_dump(mode="python")
    payload["execution"]["role"] = "api"

    with pytest.raises(ValidationError, match="require PostgreSQL persistence"):
        RuntimeConfig.model_validate(payload)


def test_distributed_profile_rejects_non_idempotent_tools(monkeypatch) -> None:
    monkeypatch.setenv("WORK_HARNESS_PROFILE", "distributed")
    config = load_app_config(PROJECT_ROOT)
    payload = config.model_dump(mode="python")
    payload["tools"]["tools"]["workspace_read"]["idempotent"] = False

    with pytest.raises(ValidationError, match="requires idempotent tools"):
        AppConfig.model_validate(payload)


def test_unknown_profile_is_rejected_instead_of_falling_back_to_mock(monkeypatch) -> None:
    monkeypatch.setenv("WORK_HARNESS_PROFILE", "typo-profile")

    with pytest.raises(ValueError, match="unsupported WORK_HARNESS_PROFILE"):
        load_app_config(PROJECT_ROOT)


def test_provider_options_reject_embedded_credentials() -> None:
    with pytest.raises(ValidationError, match="credential channel"):
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


def test_stored_provider_credential_does_not_require_environment_name() -> None:
    config = ProviderConfig(
        adapter="openai_compatible",
        enabled=True,
        base_url="https://models.example.test/v1",
        credential_source="stored",
        allowed_models=["configured-model"],
        capabilities={"chat", "structured_output", "tool_calling"},
    )

    assert config.api_key_env is None
    assert config.runtime_api_key() == ""
    assert "runtime_api_key" not in config.model_dump()
