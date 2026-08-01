"""用同一套运行时创建所有角色 Agent。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from langchain_core.tools import BaseTool

from ..state import AgentRole
from ..tools import ToolExecutor, ToolRegistry
from .prompts import ROLE_PROMPTS
from .react_agent import ChatModel, ReActAgentNode


def create_agent_nodes(
    *,
    model: ChatModel,
    tools: Sequence[BaseTool] = (),
    registry: ToolRegistry | None = None,
    max_iterations: int = 5,
) -> dict[AgentRole, ReActAgentNode]:
    """共享模型、工具和循环配置，仅为每个角色替换 Prompt。"""
    tool_executor = ToolExecutor(tools, registry=registry)
    prepared_model = _bind_tools(model, tool_executor.registry.list_tools())
    return {
        role: ReActAgentNode(
            role=role,
            system_prompt=prompt,
            model=prepared_model,
            tool_executor=tool_executor,
            max_iterations=max_iterations,
        )
        for role, prompt in ROLE_PROMPTS.items()
    }


def _bind_tools(model: ChatModel, tools: Sequence[BaseTool]) -> ChatModel:
    """模型支持 bind_tools 时只绑定一次，所有 Agent 共享结果。"""
    bind_tools = getattr(model, "bind_tools", None)
    if not tools or not callable(bind_tools):
        return model
    return cast(ChatModel, bind_tools(tools))


__all__ = ["create_agent_nodes"]
