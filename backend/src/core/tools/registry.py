"""LangChain 工具的唯一注册与角色授权。"""

from collections.abc import Collection, Sequence

from langchain_core.tools import BaseTool

from ..state import AgentRole


class ToolRegistry:
    """按名称保存工具及允许调用的 Agent 角色。"""

    def __init__(self, tools: Sequence[BaseTool] = ()) -> None:
        self._tools: dict[str, BaseTool] = {}
        self._allowed_roles: dict[str, frozenset[AgentRole]] = {}
        for tool in tools:
            self.register(tool)

    def register(
        self,
        tool: BaseTool,
        *,
        allowed_roles: Collection[AgentRole] | None = None,
    ) -> None:
        """注册工具；默认允许全部角色。"""
        if tool.name in self._tools:
            raise ValueError(f"工具名称已注册：{tool.name}")
        self._tools[tool.name] = tool
        self._allowed_roles[tool.name] = (
            frozenset(AgentRole) if allowed_roles is None else frozenset(allowed_roles)
        )

    def list_tools(self) -> tuple[BaseTool, ...]:
        """按注册顺序返回工具。"""
        return tuple(self._tools.values())

    def get(self, name: str) -> BaseTool | None:
        """按名称查找工具。"""
        return self._tools.get(name)

    def is_authorized(self, name: str, role: AgentRole) -> bool:
        """判断角色是否可调用指定工具。"""
        return role in self._allowed_roles.get(name, frozenset())


__all__ = ["ToolRegistry"]
