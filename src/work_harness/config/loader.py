from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from work_harness.app_paths import frozen_bundle_root
from work_harness.config.models import (
    AgentsConfig,
    ApiConfig,
    AppConfig,
    ModelsConfig,
    RuntimeConfig,
    ToolsConfig,
)


def default_project_root() -> Path:
    bundle_root = frozen_bundle_root()
    if bundle_root and (bundle_root / "config" / "runtime.yaml").is_file():
        return bundle_root
    source_root = Path(__file__).resolve().parents[3]
    package_root = Path(__file__).resolve().parents[1]
    candidates = (source_root, Path.cwd(), package_root / "_runtime")
    for candidate in candidates:
        if (candidate / "config" / "runtime.yaml").is_file():
            return candidate.resolve()
    return source_root


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"required configuration file does not exist: {path}")
    with path.open("r", encoding="utf-8") as stream:
        value = yaml.safe_load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"configuration root must be a mapping: {path}")
    # Normalize before merging so old profile overrides still override the new base key.
    if path.name == "agents.yaml":
        for role in value.get("agents", {}).values():
            if isinstance(role, dict) and "token_budget" in role:
                old = role.pop("token_budget")
                role.setdefault("max_output_tokens", old)
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
    profile = os.getenv("WORK_HARNESS_PROFILE", "setup").strip() or "setup"
    supported_profiles = {"setup", "local", "secure", "production", "distributed"}
    if profile not in supported_profiles:
        raise ValueError(
            f"unsupported WORK_HARNESS_PROFILE {profile!r}; "
            f"expected one of {sorted(supported_profiles)}"
        )

    runtime_data = _profiled(config_dir, "runtime", profile)
    for environment_name, key in (
        ("WORK_HARNESS_WORKFLOW_ENGINE", "engine"),
        ("WORK_HARNESS_ACCEPT_NEW_BLACKBOARD_RUNS", "accept_new_blackboard_runs"),
        ("WORK_HARNESS_BLACKBOARD_PATH", "blackboard_path"),
    ):
        value = os.getenv(environment_name, "").strip()
        if value:
            workflow = runtime_data.setdefault("workflow", {})
            if key == "blackboard_path":
                workflow.setdefault("blackboard", {})["path"] = value
            else:
                workflow[key] = value
    execution_role = os.getenv("WORK_HARNESS_EXECUTION_ROLE", "").strip()
    if execution_role:
        runtime_data = _deep_merge(
            runtime_data,
            {"execution": {"role": execution_role}},
        )
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
    provider_store_override = os.getenv("WORK_HARNESS_PROVIDER_STORE_PATH")
    if provider_store_override:
        runtime_data = _deep_merge(
            runtime_data,
            {"provider_store": {"path": provider_store_override}},
        )
    log_level = os.getenv("WORK_HARNESS_LOG_LEVEL")
    if log_level:
        runtime_data = _deep_merge(runtime_data, {"trace": {"log_level": log_level}})
    runtime_environment = {
        "WORK_HARNESS_RUN_MAX_CONCURRENCY": "run_max_concurrency",
        "WORK_HARNESS_RUN_QUEUE_CAPACITY": "run_queue_capacity",
        "WORK_HARNESS_RUN_QUEUE_RETRY_AFTER_SECONDS": ("run_queue_retry_after_seconds"),
        "WORK_HARNESS_RUN_TIMEOUT_SECONDS": "run_timeout_seconds",
        "WORK_HARNESS_RUN_LEASE_SECONDS": "run_lease_seconds",
        "WORK_HARNESS_HEARTBEAT_INTERVAL_SECONDS": "heartbeat_interval_seconds",
    }
    for environment_name, field_name in runtime_environment.items():
        value = os.getenv(environment_name, "").strip()
        if value:
            runtime_data[field_name] = value
    execution_environment = {
        "WORK_HARNESS_EXECUTION_POLL_INTERVAL_SECONDS": "poll_interval_seconds",
        "WORK_HARNESS_WORKER_STALE_SECONDS": "worker_stale_seconds",
        "WORK_HARNESS_JOB_MAX_ATTEMPTS": "job_max_attempts",
        "WORK_HARNESS_RETRY_BASE_SECONDS": "retry_base_seconds",
        "WORK_HARNESS_RETRY_MAX_SECONDS": "retry_max_seconds",
    }
    for environment_name, field_name in execution_environment.items():
        value = os.getenv(environment_name, "").strip()
        if value:
            runtime_data.setdefault("execution", {})[field_name] = value

    models_data = _profiled(config_dir, "models", profile)
    models_data["profile"] = profile
    default_model_id = os.getenv("MODEL_ID", "").strip()
    if default_model_id:
        compatible = models_data.get("providers", {}).get("compatible")
        if isinstance(compatible, dict):
            compatible["allowed_models"] = [default_model_id]
        for logical in models_data.get("logical_models", {}).values():
            if not isinstance(logical, dict):
                continue
            for candidate in logical.get("candidates", []):
                if isinstance(candidate, dict) and candidate.get("provider") == "compatible":
                    candidate["model"] = default_model_id

    return AppConfig(
        project_root=root,
        agents=AgentsConfig.model_validate(_profiled(config_dir, "agents", profile)),
        models=ModelsConfig.model_validate(models_data),
        tools=ToolsConfig.model_validate(_profiled(config_dir, "tools", profile)),
        runtime=RuntimeConfig.model_validate(runtime_data),
        api=ApiConfig.model_validate(_profiled(config_dir, "api", profile)),
    )
