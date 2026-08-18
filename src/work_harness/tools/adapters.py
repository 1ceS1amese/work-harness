from __future__ import annotations

from collections.abc import Callable
from importlib.metadata import entry_points
from pathlib import Path

from work_harness.tools.base import ToolHandler

ToolBuilder = Callable[[Path], ToolHandler]


class ToolAdapterRegistry:
    """Build configured tools by name, including installed entry-point plugins."""

    entry_point_group = "work_harness.tools"

    def __init__(self) -> None:
        self._builders: dict[str, ToolBuilder] = {}

    def register(self, name: str, builder: ToolBuilder) -> None:
        if name in self._builders:
            raise ValueError(f"tool adapter already registered: {name}")
        self._builders[name] = builder

    def load_entry_points(self) -> None:
        for entry_point in entry_points(group=self.entry_point_group):
            builder = entry_point.load()
            if not callable(builder):
                raise TypeError(f"tool entry point {entry_point.name} must be callable")
            self.register(entry_point.name, builder)

    def build(self, name: str, project_root: Path) -> ToolHandler:
        try:
            builder = self._builders[name]
        except KeyError as exc:
            raise ValueError(f"configured tool has no registered adapter: {name}") from exc
        handler = builder(project_root)
        if handler.name != name:
            raise TypeError(f"tool adapter {name} returned handler named {handler.name!r}")
        if not callable(getattr(handler, "execute", None)):
            raise TypeError(f"tool adapter {name} is missing execute()")
        return handler

    @property
    def adapters(self) -> tuple[str, ...]:
        return tuple(sorted(self._builders))


def build_default_tool_adapter_registry() -> ToolAdapterRegistry:
    from work_harness.tools.builtin import EchoReadTool, WorkspaceReadTool

    registry = ToolAdapterRegistry()
    registry.register("echo_read", lambda _: EchoReadTool())
    registry.register("workspace_read", WorkspaceReadTool)
    registry.load_entry_points()
    return registry
