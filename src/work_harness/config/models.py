from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from work_harness.domain.enums import RiskLevel, ToolCategory


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CheckpointConfig(ConfigModel):
    backend: Literal["memory", "sqlite"] = "sqlite"
    path: str = "var/checkpoints.sqlite"


class RunStoreConfig(ConfigModel):
    backend: Literal["memory", "sqlite"] = "sqlite"
    path: str = "var/runs.sqlite"


class TraceConfig(ConfigModel):
    enabled: bool = True
    sample_rate: float = Field(default=1.0, ge=0, le=1)
    log_level: str = "INFO"


class RuntimeConfig(ConfigModel):
    schema_version: int = 1
    worker_max_concurrency: int = Field(default=4, ge=1, le=32)
    reviewer_max_concurrency: int = Field(default=3, ge=1, le=32)
    max_planned_subtasks: int = Field(default=4, ge=1, le=32)
    max_repair_rounds: int = Field(default=1, ge=0, le=5)
    max_node_steps: int = Field(default=10, ge=1, le=100)
    node_timeout_seconds: float = Field(default=60, gt=0)
    run_timeout_seconds: float = Field(default=300, gt=0)
    token_budget: int = Field(default=50_000, ge=1)
    cost_budget_usd: float = Field(default=5.0, ge=0)
    max_artifact_chars: int = Field(default=20_000, ge=100)
    max_tool_result_chars: int = Field(default=8_000, ge=100)
    model_retry_attempts: int = Field(default=2, ge=0, le=5)
    recovery_enabled: bool = True
    run_lease_seconds: float = Field(default=30, ge=5, le=3600)
    heartbeat_interval_seconds: float = Field(default=10, ge=1, le=600)
    recovery_scan_interval_seconds: float = Field(default=10, ge=1, le=600)
    recovery_scan_limit: int = Field(default=200, ge=1, le=10_000)
    checkpoint: CheckpointConfig = Field(default_factory=CheckpointConfig)
    run_store: RunStoreConfig = Field(default_factory=RunStoreConfig)
    trace: TraceConfig = Field(default_factory=TraceConfig)

    @model_validator(mode="after")
    def validate_recovery_timing(self) -> RuntimeConfig:
        if self.heartbeat_interval_seconds >= self.run_lease_seconds:
            raise ValueError("heartbeat interval must be shorter than run lease")
        return self


class ProviderConfig(ConfigModel):
    adapter: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    enabled: bool = True
    base_url_env: str | None = None
    api_key_env: str | None = None
    allowed_models: list[str] = Field(default_factory=list)
    capabilities: set[str] = Field(default_factory=set)
    timeout_seconds: float = Field(default=60, gt=0)
    allow_insecure_http: bool = False
    allow_private_network: bool = False
    options: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_provider_contract(self) -> ProviderConfig:
        if self.adapter == "openai_compatible" and self.enabled:
            if not self.base_url_env or not self.api_key_env or not self.allowed_models:
                raise ValueError(
                    "enabled openai_compatible provider requires base_url_env, "
                    "api_key_env, and allowed_models"
                )
        return self


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
    profile: str = "local"
    providers: dict[str, ProviderConfig]
    logical_models: dict[str, LogicalModelConfig]
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
    token_budget: int = Field(ge=1)
    cost_budget_usd: float = Field(ge=0)
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
        if any(
            role.model_dump(exclude={"display_name"}) != reference
            for role in assignable[1:]
        ):
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
                    "enabled providers must declare identical capabilities: "
                    f"{mismatched}"
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
                    missing_capabilities = (
                        logical.required_capabilities - provider.capabilities
                    )
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

        if self.models.profile == "production":
            enabled_mock = any(
                provider.enabled and provider.adapter == "mock"
                for provider in providers.values()
            )
            if enabled_mock:
                raise ValueError("production profile must not enable Mock Provider")
        return self
