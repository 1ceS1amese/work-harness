from pathlib import Path

from work_harness.config.loader import load_app_config

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_configuration_references_are_valid() -> None:
    config = load_app_config(PROJECT_ROOT)
    assert config.models.profile == "local"
    assert "planner-strong" in config.models.logical_models
    assert config.runtime.max_repair_rounds == 1
    assert config.runtime.run_store.backend == "sqlite"
    assert config.runtime.run_store.path == "var/runs.sqlite"
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
    assert all(
        logical.candidates[0].provider == "compatible"
        for logical in config.models.logical_models.values()
    )
