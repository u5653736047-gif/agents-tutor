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
            self.register(tool)  # 逐个注册，重复名称会在这里报错

    def register(
        self,
        tool: BaseTool,
        *,
        allowed_roles: Collection[AgentRole] | None = None,
    ) -> None:
        """注册工具；默认允许全部角色。"""
        if tool.name in self._tools:  # 名称即唯一标识，重复注册直接拒绝
            raise ValueError(f"工具名称已注册：{tool.name}")
        self._tools[tool.name] = tool
        self._allowed_roles[tool.name] = (
            # 没指定角色范围就默认所有角色都能调用
            frozenset(AgentRole) if allowed_roles is None else frozenset(allowed_roles)
        )

    def list_tools(self) -> tuple[BaseTool, ...]:
        """按注册顺序返回工具。"""
        return tuple(self._tools.values())

    def get(self, name: str) -> BaseTool | None:
        """按名称查找工具。"""
        return self._tools.get(name)  # 没注册过的返回 None，由调用方自行判断

    def is_authorized(self, name: str, role: AgentRole) -> bool:
        """判断角色是否可调用指定工具。"""
        return role in self._allowed_roles.get(name, frozenset())  # 未注册工具默认谁都不能调


__all__ = ["ToolRegistry"]
