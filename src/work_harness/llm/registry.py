from __future__ import annotations

from collections.abc import Callable
from importlib.metadata import entry_points

from work_harness.config.models import ProviderConfig
from work_harness.llm.base import ModelProvider

ProviderBuilder = Callable[[str, ProviderConfig], ModelProvider]


class ProviderAdapterRegistry:
    """Build providers by adapter name, including installed entry-point plugins."""

    entry_point_group = "work_harness.providers"

    def __init__(self) -> None:
        self._builders: dict[str, ProviderBuilder] = {}

    def register(self, adapter: str, builder: ProviderBuilder) -> None:
        if adapter in self._builders:
            raise ValueError(f"provider adapter already registered: {adapter}")
        self._builders[adapter] = builder

    def load_entry_points(self) -> None:
        for entry_point in entry_points(group=self.entry_point_group):
            builder = entry_point.load()
            if not callable(builder):
                raise TypeError(f"provider entry point {entry_point.name} must be callable")
            self.register(entry_point.name, builder)

    def build(self, name: str, config: ProviderConfig) -> ModelProvider:
        try:
            builder = self._builders[config.adapter]
        except KeyError as exc:
            raise ValueError(
                f"provider {name} uses an unregistered adapter: {config.adapter}"
            ) from exc
        provider = builder(name, config)
        for attribute in ("name", "protocol_version", "capabilities"):
            if not hasattr(provider, attribute):
                raise TypeError(f"provider adapter {config.adapter} is missing {attribute}")
        for method in ("complete", "healthcheck"):
            if not callable(getattr(provider, method, None)):
                raise TypeError(f"provider adapter {config.adapter} is missing {method}()")
        return provider

    @property
    def adapters(self) -> tuple[str, ...]:
        return tuple(sorted(self._builders))


def build_default_provider_registry() -> ProviderAdapterRegistry:
    from work_harness.llm.anthropic import AnthropicProvider
    from work_harness.llm.mock import MockProvider
    from work_harness.llm.openai_compatible import OpenAICompatibleProvider

    registry = ProviderAdapterRegistry()
    registry.register("mock", lambda name, _: MockProvider(name))
    registry.register(
        "openai_compatible",
        lambda name, config: OpenAICompatibleProvider(name, config),
    )
    registry.register("anthropic", lambda name, config: AnthropicProvider(name, config))
    registry.load_entry_points()
    return registry
