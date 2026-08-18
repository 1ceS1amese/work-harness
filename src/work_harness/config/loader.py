from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from work_harness.config.models import (
    AgentsConfig,
    ApiConfig,
    AppConfig,
    ModelsConfig,
    RuntimeConfig,
    ToolsConfig,
)


def default_project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"required configuration file does not exist: {path}")
    with path.open("r", encoding="utf-8") as stream:
        value = yaml.safe_load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"configuration root must be a mapping: {path}")
    return value


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _profiled(config_dir: Path, name: str, profile: str) -> dict[str, Any]:
    base = _load_yaml(config_dir / f"{name}.yaml")
    override_path = config_dir / "profiles" / profile / f"{name}.yaml"
    if override_path.is_file():
        return _deep_merge(base, _load_yaml(override_path))
    return base


def load_app_config(project_root: Path | str | None = None) -> AppConfig:
    root = Path(project_root or default_project_root()).resolve()
    config_dir = root / "config"
    profile = os.getenv("WORK_HARNESS_PROFILE", "local").strip() or "local"

    runtime_data = _profiled(config_dir, "runtime", profile)
    checkpoint_override = os.getenv("WORK_HARNESS_CHECKPOINT_PATH")
    if checkpoint_override:
        runtime_data = _deep_merge(
            runtime_data,
            {"checkpoint": {"path": checkpoint_override}},
        )
    run_store_override = os.getenv("WORK_HARNESS_RUN_STORE_PATH")
    if run_store_override:
        runtime_data = _deep_merge(
            runtime_data,
            {"run_store": {"path": run_store_override}},
        )
    log_level = os.getenv("WORK_HARNESS_LOG_LEVEL")
    if log_level:
        runtime_data = _deep_merge(runtime_data, {"trace": {"log_level": log_level}})

    models_data = _profiled(config_dir, "models", profile)
    models_data["profile"] = profile

    return AppConfig(
        project_root=root,
        agents=AgentsConfig.model_validate(_profiled(config_dir, "agents", profile)),
        models=ModelsConfig.model_validate(models_data),
        tools=ToolsConfig.model_validate(_profiled(config_dir, "tools", profile)),
        runtime=RuntimeConfig.model_validate(runtime_data),
        api=ApiConfig.model_validate(_profiled(config_dir, "api", profile)),
    )
