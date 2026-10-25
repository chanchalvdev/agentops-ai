"""Tool registry: the only place the harness looks up a tool by name."""

from collections.abc import Iterable
from typing import Any

from app.llm.schemas import ToolSpec
from app.tools.base import Tool

type AnyTool = Tool[Any, Any]


class ToolRegistry:
    def __init__(self, tools: Iterable[AnyTool] = ()) -> None:
        self._tools: dict[str, AnyTool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: AnyTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> AnyTool | None:
        return self._tools.get(name)

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._tools)

    def specs(self, allowed: Iterable[str]) -> list[ToolSpec]:
        """Specs for the allowed subset only: the model never sees tools it may not call."""
        allowed_set = set(allowed)
        return [t.spec() for name, t in sorted(self._tools.items()) if name in allowed_set]
