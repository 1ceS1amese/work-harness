from __future__ import annotations

from work_harness.config.models import AppConfig
from work_harness.storage.provider_base import ProviderProfileRecord


def apply_provider_profiles(
    config: AppConfig,
    profiles: list[ProviderProfileRecord],
) -> AppConfig:
    """Overlay durable profiles on YAML defaults and rebuild logical candidates."""

    payload = config.model_dump(mode="python")
    models = payload["models"]
    providers = models["providers"]
    logical_models = models["logical_models"]
    agent_routes = models["agent_routes"]
    agents = payload["agents"]["agents"]
    main_agents = {
        name for name, role in agents.items() if not role.get("assignable", False)
    }
    claimed_agents: dict[str, str] = {}

    for profile in sorted(profiles, key=lambda item: item.name):
        unknown = set(profile.model_bindings) - set(logical_models)
        if unknown:
            raise ValueError(
                f"provider {profile.name} binds unknown logical models: {sorted(unknown)}"
            )
        unknown_agents = set(profile.agent_bindings) - set(agents) - {"main"}
        if unknown_agents:
            raise ValueError(
                f"provider {profile.name} binds unknown agents: {sorted(unknown_agents)}"
            )
        providers[profile.name] = profile.config.model_dump(mode="python")
        agent_routes = {
            agent: route
            for agent, route in agent_routes.items()
            if route["provider"] != profile.name
        }
        models["agent_routes"] = agent_routes
        for logical_name, logical in logical_models.items():
            candidates = logical["candidates"]
            previous_indexes = [
                index
                for index, candidate in enumerate(candidates)
                if candidate["provider"] == profile.name
            ]
            logical["candidates"] = [
                candidate
                for candidate in candidates
                if candidate["provider"] != profile.name
            ]
            model = profile.model_bindings.get(logical_name)
            if model is None:
                continue
            insertion = previous_indexes[0] if previous_indexes else len(
                logical["candidates"]
            )
            logical["candidates"].insert(
                min(insertion, len(logical["candidates"])),
                {"provider": profile.name, "model": model},
            )

        if not profile.config.enabled:
            continue
        expanded_bindings = {
            agent: profile.agent_bindings["main"]
            for agent in main_agents
            if "main" in profile.agent_bindings
        }
        expanded_bindings.update(
            {
                agent: model
                for agent, model in profile.agent_bindings.items()
                if agent != "main"
            }
        )
        for agent, model in expanded_bindings.items():
            previous_provider = claimed_agents.get(agent)
            if previous_provider and previous_provider != profile.name:
                raise ValueError(
                    f"agent {agent} is bound by both {previous_provider} and {profile.name}"
                )
            claimed_agents[agent] = profile.name
            agent_routes[agent] = {"provider": profile.name, "model": model}
            logical_name = agents[agent]["logical_model"]
            candidates = logical_models[logical_name]["candidates"]
            if not any(
                candidate["provider"] == profile.name and candidate["model"] == model
                for candidate in candidates
            ):
                candidates.append({"provider": profile.name, "model": model})

    return AppConfig.model_validate(payload)


def provider_model_bindings(config: AppConfig, provider_name: str) -> dict[str, str]:
    return {
        logical_name: candidate.model
        for logical_name, logical in config.models.logical_models.items()
        for candidate in logical.candidates
        if candidate.provider == provider_name
    }


def provider_agent_bindings(config: AppConfig, provider_name: str) -> dict[str, str]:
    return {
        agent_name: route.model
        for agent_name, route in config.models.agent_routes.items()
        if route.provider == provider_name
    }
