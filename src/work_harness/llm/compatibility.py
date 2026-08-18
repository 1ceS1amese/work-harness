from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from work_harness.config.models import ModelsConfig
from work_harness.llm.base import ModelProvider


class CompatibilityModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProviderCompatibilityItem(CompatibilityModel):
    provider: str
    adapter: str
    protocol_variant: str
    api_family: str
    protocol_version: str
    capabilities: list[str]
    configured_capabilities: list[str]
    baseline_capabilities: list[str]
    compatible: bool
    issues: list[str] = Field(default_factory=list)


class ProviderCompatibilityReport(CompatibilityModel):
    protocol_version: str
    baseline_capabilities: list[str]
    compatible: bool
    providers: list[ProviderCompatibilityItem]


def inspect_provider_compatibility(
    config: ModelsConfig,
    providers: dict[str, ModelProvider],
) -> ProviderCompatibilityReport:
    expected_protocol = config.compatibility.protocol_version
    baseline = config.compatibility.baseline_capabilities
    rows: list[ProviderCompatibilityItem] = []
    for name, provider_config in config.providers.items():
        if not provider_config.enabled:
            continue
        provider = providers.get(name)
        issues: list[str] = []
        if provider is None:
            issues.append("enabled provider was not constructed")
            actual_protocol = "missing"
            actual_capabilities: set[str] = set()
        else:
            actual_protocol = provider.protocol_version
            actual_capabilities = set(provider.capabilities)
            if provider.name != name:
                issues.append(f"provider reports name {provider.name!r} instead of {name!r}")
            if actual_protocol != expected_protocol:
                issues.append(f"protocol {actual_protocol!r} does not match {expected_protocol!r}")
            missing_baseline = baseline - actual_capabilities
            if config.compatibility.enforce_baseline and missing_baseline:
                issues.append(f"missing baseline capabilities: {sorted(missing_baseline)}")
            if (
                config.compatibility.require_truthful_declaration
                and actual_capabilities != provider_config.capabilities
            ):
                issues.append("runtime capabilities do not exactly match configured capabilities")
        rows.append(
            ProviderCompatibilityItem(
                provider=name,
                adapter=provider_config.adapter,
                protocol_variant=provider_config.protocol_variant,
                api_family=provider_config.api_family,
                protocol_version=actual_protocol,
                capabilities=sorted(actual_capabilities),
                configured_capabilities=sorted(provider_config.capabilities),
                baseline_capabilities=sorted(baseline),
                compatible=not issues,
                issues=issues,
            )
        )
    return ProviderCompatibilityReport(
        protocol_version=expected_protocol,
        baseline_capabilities=sorted(baseline),
        compatible=all(row.compatible for row in rows),
        providers=rows,
    )
