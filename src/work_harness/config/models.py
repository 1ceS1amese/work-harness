from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urlparse

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
    model_validator,
)

from work_harness.domain.enums import RiskLevel, ToolCategory


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CheckpointConfig(ConfigModel):
    backend: Literal["memory", "sqlite", "postgres"] = "sqlite"
    path: str = "var/checkpoints.sqlite"
    dsn_env: str = Field(
        default="WORK_HARNESS_DATABASE_URL",
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
    )


class RunStoreConfig(ConfigModel):
    backend: Literal["memory", "sqlite", "postgres"] = "sqlite"
    path: str = "var/runs.sqlite"
    dsn_env: str = Field(
        default="WORK_HARNESS_DATABASE_URL",
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
    )
    pool_min_size: int = Field(default=1, ge=1, le=100)
    pool_max_size: int = Field(default=10, ge=1, le=500)

    @model_validator(mode="after")
    def validate_pool_size(self) -> RunStoreConfig:
        if self.pool_min_size > self.pool_max_size:
            raise ValueError("run store pool_min_size cannot exceed pool_max_size")
        return self


class ProviderStoreConfig(ConfigModel):
    backend: Literal["memory", "sqlite"] = "sqlite"
    path: str = "var/providers.sqlite"


class TraceConfig(ConfigModel):
    enabled: bool = True
    sample_rate: float = Field(default=1.0, ge=0, le=1)
    log_level: str = "INFO"


class ExecutionConfig(ConfigModel):
    role: Literal["embedded", "api", "worker"] = "embedded"
    poll_interval_seconds: float = Field(default=0.5, ge=0.05, le=30)
    worker_stale_seconds: float = Field(default=30, ge=5, le=600)
    job_max_attempts: int = Field(default=3, ge=1, le=100)
    retry_base_seconds: float = Field(default=2, ge=0, le=3600)
    retry_max_seconds: float = Field(default=60, ge=0, le=86_400)

    @model_validator(mode="after")
    def validate_retry_range(self) -> ExecutionConfig:
        if self.retry_base_seconds > self.retry_max_seconds:
            raise ValueError("retry_base_seconds cannot exceed retry_max_seconds")
        return self


class BlackboardWorkflowConfig(ConfigModel):
    path: str = "var/blackboard.sqlite"
    agent_roles: list[str] = Field(default_factory=lambda: ["worker-01"], max_length=8)
    max_activations: int = Field(default=40, ge=1, le=1000)
    max_no_progress: int = Field(default=5, ge=1, le=100)
    max_explorations: int = Field(default=4, ge=0, le=64)
    max_objects: int = Field(default=256, ge=1, le=10000)
    max_child_intents: int = Field(default=16, ge=1, le=64)
    max_per_intent: int = Field(default=2, ge=1, le=64)
    max_wait_batches: int = Field(default=3, ge=1, le=100)

    @field_validator("agent_roles")
    @classmethod
    def unique_roles(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("blackboard agent roles must be unique")
        return value


class WorkflowConfig(ConfigModel):
    engine: Literal["legacy", "blackboard"] = "blackboard"
    accept_new_blackboard_runs: bool = True
    blackboard: BlackboardWorkflowConfig = Field(default_factory=BlackboardWorkflowConfig)


class RuntimeConfig(ConfigModel):
    schema_version: int = 1
    run_max_concurrency: int = Field(default=4, ge=1, le=64)
    run_queue_capacity: int = Field(default=8, ge=0, le=10_000)
    run_queue_retry_after_seconds: int = Field(default=5, ge=1, le=300)
    worker_max_concurrency: int = Field(default=4, ge=1, le=32)
    reviewer_max_concurrency: int = Field(default=3, ge=1, le=32)
    review_strategy: Literal["adaptive", "comprehensive", "three_dimensional"] = "adaptive"
    deep_review_risk_levels: set[RiskLevel] = Field(
        default_factory=lambda: {RiskLevel.HIGH, RiskLevel.CRITICAL}
    )
    deep_review_task_types: set[str] = Field(default_factory=lambda: {"research", "decision"})
    max_planned_subtasks: int = Field(default=4, ge=1, le=32)
    max_repair_rounds: int = Field(default=1, ge=0, le=5)
    max_node_steps: int = Field(default=10, ge=1, le=100)
    node_timeout_seconds: float = Field(default=60, gt=0)
    run_timeout_seconds: float = Field(default=300, gt=0)
    token_budget: int | None = Field(default=None, ge=0, deprecated=True)
    cost_budget_usd: float | None = Field(default=None, ge=0, deprecated=True)
    max_artifact_chars: int = Field(default=20_000, ge=100)
    max_tool_result_chars: int = Field(default=8_000, ge=100)
    tool_cache_max_entries: int = Field(default=256, ge=0, le=100_000)
    model_retry_attempts: int = Field(default=2, ge=0, le=5)
    sse_keepalive_seconds: float = Field(default=15, ge=1, le=120)
    recovery_enabled: bool = True
    run_lease_seconds: float = Field(default=30, ge=5, le=3600)
    heartbeat_interval_seconds: float = Field(default=10, ge=1, le=600)
    recovery_scan_interval_seconds: float = Field(default=10, ge=1, le=600)
    recovery_scan_limit: int = Field(default=200, ge=1, le=10_000)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    workflow: WorkflowConfig = Field(default_factory=WorkflowConfig)
    checkpoint: CheckpointConfig = Field(default_factory=CheckpointConfig)
    run_store: RunStoreConfig = Field(default_factory=RunStoreConfig)
    provider_store: ProviderStoreConfig = Field(default_factory=ProviderStoreConfig)
    trace: TraceConfig = Field(default_factory=TraceConfig)

    @model_validator(mode="after")
    def validate_recovery_timing(self) -> RuntimeConfig:
        if self.heartbeat_interval_seconds >= self.run_lease_seconds:
            raise ValueError("heartbeat interval must be shorter than run lease")
        if (self.checkpoint.backend == "postgres") != (self.run_store.backend == "postgres"):
            raise ValueError("PostgreSQL checkpoint and run store must be enabled together")
        if (
            self.checkpoint.backend == "postgres"
            and self.checkpoint.dsn_env != self.run_store.dsn_env
        ):
            raise ValueError(
                "PostgreSQL checkpoint and run store must use the same DSN environment"
            )
        if self.execution.role != "embedded":
            if self.run_store.backend != "postgres":
                raise ValueError("api/worker execution roles require PostgreSQL persistence")
            if self.run_queue_capacity < 1:
                raise ValueError("distributed execution requires run_queue_capacity >= 1")
        if self.workflow.engine == "blackboard" and (
            self.execution.role == "embedded" and self.run_store.backend == "postgres"
        ):
            raise ValueError("PostgreSQL blackboard execution requires API/Worker roles")
        return self


class ProviderConfig(ConfigModel):
    _runtime_api_key: str | None = PrivateAttr(default=None)

    adapter: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    protocol_variant: Literal["openai", "deepseek"] = "openai"
    api_family: Literal["chat_completions", "responses"] = "chat_completions"
    structured_output_mode: Literal["json_schema", "json_object"] | None = None
    chat_token_parameter: Literal["max_completion_tokens", "max_tokens"] | None = None
    store_responses: bool = False
    enabled: bool = True
    base_url: str | None = None
    base_url_env: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    credential_source: Literal["environment", "stored"] = "environment"
    api_key_env: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    allowed_models: list[str] = Field(default_factory=list)
    capabilities: set[str] = Field(default_factory=set)
    timeout_seconds: float = Field(default=60, gt=0)
    max_concurrency: int = Field(default=8, ge=1, le=256)
    allow_insecure_http: bool = False
    allow_private_network: bool = False
    options: dict[str, Any] = Field(default_factory=dict)

    @field_validator("base_url")
    @classmethod
    def reject_credentials_in_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlparse(value)
        if parsed.username or parsed.password:
            raise ValueError("provider base_url cannot contain credentials")
        for key, _ in parse_qsl(parsed.query, keep_blank_values=True):
            normalized = key.strip().lower().replace("-", "_")
            if normalized == "authorization" or normalized.endswith(
                ("api_key", "token", "secret", "password")
            ):
                raise ValueError("provider base_url cannot contain credential query fields")
        return value

    @field_validator("options")
    @classmethod
    def reject_embedded_secrets(cls, value: dict[str, Any]) -> dict[str, Any]:
        sensitive_suffixes = ("api_key", "token", "secret", "password")

        def inspect(item: Any) -> None:
            if isinstance(item, dict):
                for key, nested in item.items():
                    normalized = str(key).strip().lower().replace("-", "_")
                    if normalized == "authorization" or normalized.endswith(sensitive_suffixes):
                        raise ValueError(
                            "provider options cannot contain credentials; use a credential channel"
                        )
                    inspect(nested)
            elif isinstance(item, list):
                for nested in item:
                    inspect(nested)

        inspect(value)
        return value

    @model_validator(mode="after")
    def validate_provider_contract(self) -> ProviderConfig:
        if (
            self.adapter == "openai_compatible"
            and self.credential_source == "stored"
            and self.api_key_env
        ):
            raise ValueError("stored credentials cannot also configure api_key_env")
        if self.adapter == "openai_compatible" and self.enabled:
            if not (self.base_url or self.base_url_env) or not self.allowed_models:
                raise ValueError(
                    "enabled openai_compatible provider requires base_url or "
                    "base_url_env and allowed_models"
                )
            if self.credential_source == "environment" and not self.api_key_env:
                raise ValueError("environment credentials require api_key_env")
        return self

    def set_runtime_api_key(self, value: str | None) -> None:
        """Attach a decrypted credential for this process without serializing it."""

        self._runtime_api_key = value

    def runtime_api_key(self) -> str:
        if self.credential_source == "stored":
            return self._runtime_api_key or ""
        return os.getenv(self.api_key_env or "", "")


class ProviderCompatibilityConfig(ConfigModel):
    protocol_version: str = "1.0"
    baseline_capabilities: set[str] = Field(
        default_factory=lambda: {"chat", "structured_output", "tool_calling"}
    )
    enforce_baseline: bool = True
    require_capability_parity: bool = True
    require_truthful_declaration: bool = True


class ModelCandidateConfig(ConfigModel):
    provider: str
    model: str


class LogicalModelConfig(ConfigModel):
    required_capabilities: set[str] = Field(default_factory=lambda: {"chat"})
    candidates: list[ModelCandidateConfig] = Field(min_length=1)


class ModelsConfig(ConfigModel):
    schema_version: int = 1
    profile: str = "setup"
    providers: dict[str, ProviderConfig]
    logical_models: dict[str, LogicalModelConfig]
    agent_routes: dict[str, ModelCandidateConfig] = Field(default_factory=dict)
    compatibility: ProviderCompatibilityConfig = Field(default_factory=ProviderCompatibilityConfig)


class AgentRoleConfig(ConfigModel):
    version: str
    responsibility: str
    display_name: str | None = None
    specialty: str | None = None
    assignable: bool = False
    logical_model: str
    allowed_tools: list[str] = Field(default_factory=list)
    input_schema: str
    output_schema: str
    max_steps: int = Field(ge=1)
    timeout_seconds: float = Field(gt=0)
    max_output_tokens: int = Field(
        ge=1, validation_alias=AliasChoices("max_output_tokens", "token_budget")
    )
    cost_budget_usd: float = Field(default=0, ge=0, deprecated=True)
    reviewers: list[str] = Field(default_factory=list)
    repairable: bool = False


class AgentsConfig(ConfigModel):
    schema_version: int = 1
    agents: dict[str, AgentRoleConfig]

    @model_validator(mode="after")
    def validate_assignable_agents_are_homogeneous(self) -> AgentsConfig:
        assignable = [role for role in self.agents.values() if role.assignable]
        if not assignable:
            raise ValueError("at least one assignable agent is required")
        reference = assignable[0].model_dump(exclude={"display_name"})
        if any(role.model_dump(exclude={"display_name"}) != reference for role in assignable[1:]):
            raise ValueError(
                "assignable agents must share one execution profile; "
                "task functions belong in AgentAssignment"
            )
        return self


class ToolConfig(ConfigModel):
    version: str
    description: str
    category: ToolCategory
    risk_level: RiskLevel
    allowed_roles: list[str] = Field(min_length=1)
    timeout_seconds: float = Field(gt=0)
    max_result_chars: int = Field(ge=100)
    retryable: bool = False
    idempotent: bool = False
    requires_approval: bool = False

    @model_validator(mode="after")
    def validate_high_risk_tool(self) -> ToolConfig:
        mutating = {
            ToolCategory.WRITE,
            ToolCategory.EXECUTE,
            ToolCategory.EXTERNAL_COMMUNICATION,
        }
        if self.category in mutating and not self.requires_approval:
            raise ValueError("mutating tools must require approval")
        return self


class ToolsConfig(ConfigModel):
    schema_version: int = 1
    tools: dict[str, ToolConfig]


class ApiConfig(ConfigModel):
    schema_version: int = 1
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    auth_enabled: bool = False
    api_key_env: str = "WORK_HARNESS_API_KEY"
    request_max_bytes: int = Field(default=1_048_576, ge=1_024)
    rate_limit_per_minute: int = Field(default=60, ge=1)
    expose_docs: bool = True


class AppConfig(ConfigModel):
    project_root: Path
    agents: AgentsConfig
    models: ModelsConfig
    tools: ToolsConfig
    runtime: RuntimeConfig
    api: ApiConfig

    @model_validator(mode="after")
    def validate_references(self) -> AppConfig:
        logical_models = self.models.logical_models
        providers = self.models.providers
        tools = self.tools.tools

        if self.models.compatibility.enforce_baseline:
            for provider_name, provider in providers.items():
                if not provider.enabled:
                    continue
                missing = self.models.compatibility.baseline_capabilities - provider.capabilities
                if missing:
                    raise ValueError(
                        f"provider {provider_name} is missing baseline capabilities: "
                        f"{sorted(missing)}"
                    )

        if self.models.compatibility.require_capability_parity:
            capability_sets = {
                provider_name: provider.capabilities
                for provider_name, provider in providers.items()
                if provider.enabled
            }
            reference = next(iter(capability_sets.values()), set())
            mismatched = {
                provider_name: sorted(capabilities)
                for provider_name, capabilities in capability_sets.items()
                if capabilities != reference
            }
            if mismatched:
                raise ValueError(
                    f"enabled providers must declare identical capabilities: {mismatched}"
                )

        for role_name, role in self.agents.agents.items():
            if role.logical_model not in logical_models:
                raise ValueError(
                    f"agent {role_name} references unknown logical model {role.logical_model}"
                )
            unknown_tools = set(role.allowed_tools) - set(tools)
            if unknown_tools:
                raise ValueError(f"agent {role_name} references unknown tools: {unknown_tools}")

        for logical_name, logical in logical_models.items():
            enabled_candidates = 0
            for candidate in logical.candidates:
                provider = providers.get(candidate.provider)
                if provider is None:
                    raise ValueError(
                        f"logical model {logical_name} references unknown provider "
                        f"{candidate.provider}"
                    )
                if provider.enabled:
                    enabled_candidates += 1
                    missing_capabilities = logical.required_capabilities - provider.capabilities
                    if missing_capabilities:
                        raise ValueError(
                            f"provider {candidate.provider} cannot satisfy logical model "
                            f"{logical_name}: {sorted(missing_capabilities)}"
                        )
                if provider.allowed_models and candidate.model not in provider.allowed_models:
                    raise ValueError(
                        f"model {candidate.model} is not allowlisted for provider "
                        f"{candidate.provider}"
                    )
            if enabled_candidates == 0:
                raise ValueError(f"logical model {logical_name} has no enabled candidates")

        for agent_name, route in self.models.agent_routes.items():
            role = self.agents.agents.get(agent_name)
            if role is None:
                raise ValueError(f"agent route references unknown agent {agent_name}")
            provider = providers.get(route.provider)
            if provider is None:
                raise ValueError(
                    f"agent route {agent_name} references unknown provider {route.provider}"
                )
            if not provider.enabled:
                raise ValueError(
                    f"agent route {agent_name} references disabled provider {route.provider}"
                )
            if provider.allowed_models and route.model not in provider.allowed_models:
                raise ValueError(
                    f"agent route {agent_name} model {route.model} is not allowlisted "
                    f"for provider {route.provider}"
                )
            logical = logical_models[role.logical_model]
            missing = logical.required_capabilities - provider.capabilities
            if missing:
                raise ValueError(
                    f"agent route {agent_name} provider {route.provider} lacks "
                    f"capabilities: {sorted(missing)}"
                )

        if self.models.profile in {"setup", "production", "distributed"}:
            enabled_mock = any(
                provider.enabled and provider.adapter == "mock" for provider in providers.values()
            )
            if enabled_mock:
                raise ValueError(f"{self.models.profile} profile must not enable Mock Provider")
        if self.models.profile == "distributed":
            unsafe_tools = sorted(name for name, tool in tools.items() if not tool.idempotent)
            if unsafe_tools:
                raise ValueError(
                    f"distributed at-least-once execution requires idempotent tools: {unsafe_tools}"
                )
        return self
