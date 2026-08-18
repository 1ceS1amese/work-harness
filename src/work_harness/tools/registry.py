from __future__ import annotations

from work_harness.config.models import ToolsConfig
from work_harness.tools.base import ToolHandler, ToolNotFoundError


class ToolRegistry:
    def __init__(self, config: ToolsConfig) -> None:
        self.config = config
        self._handlers: dict[str, ToolHandler] = {}

    def register(self, handler: ToolHandler) -> None:
        if handler.name not in self.config.tools:
            raise ValueError(f"tool handler is not declared in configuration: {handler.name}")
        if handler.name in self._handlers:
            raise ValueError(f"tool handler already registered: {handler.name}")
        self._handlers[handler.name] = handler

    def get(self, name: str) -> ToolHandler:
        try:
            return self._handlers[name]
        except KeyError as exc:
            raise ToolNotFoundError(f"tool handler not registered: {name}") from exc

    def validate_complete(self) -> None:
        missing = set(self.config.tools) - set(self._handlers)
        if missing:
            raise ValueError(f"configured tools have no handlers: {sorted(missing)}")

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._handlers)

    def definitions(self, *, role: str, names: list[str]) -> list[dict[str, object]]:
        definitions: list[dict[str, object]] = []
        for name in names:
            handler = self.get(name)
            config = self.config.tools[name]
            if role not in config.allowed_roles:
                raise ValueError(f"role {role} is not allowed to advertise {name}")
            definitions.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": config.description,
                        "parameters": handler.input_model.model_json_schema(),
                    },
                }
            )
        return definitions
