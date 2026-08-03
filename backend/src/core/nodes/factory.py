"""用同一套运行时创建所有角色 Agent。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast

from langchain_core.tools import BaseTool

from ..context import MessageTokenCounter
from ..state import AgentRole
from ..tools import DEFAULT_TOOL_TIMEOUT_SECONDS, ToolExecutor, ToolRegistry
from .prompts import ROLE_PROMPTS, learning_assistant_system_prompt
from .react_agent import ChatModel, ReActAgentNode


def create_agent_nodes(
    *,
    model: ChatModel,
    tools: Sequence[BaseTool] = (),
    registry: ToolRegistry | None = None,
    max_iterations: int = 5,
    max_context_messages: int | None = None,
    max_context_tokens: int | None = None,
    context_token_counter: MessageTokenCounter | None = None,
    tool_timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS,
    tool_timeouts: Mapping[str, float] | None = None,
) -> dict[AgentRole, ReActAgentNode]:
    """共享模型、工具和循环配置，仅为每个角色替换 Prompt。"""
    tool_executor = ToolExecutor(
        tools,
        registry=registry,
        tool_timeout_seconds=tool_timeout_seconds,
        tool_timeouts=tool_timeouts,
    )
    prepared_model = _bind_tools(model, tool_executor.registry.list_tools())
    return {
        role: ReActAgentNode(
            role=role,
            system_prompt=prompt,
            model=prepared_model,
            tool_executor=tool_executor,
            max_iterations=max_iterations,
            max_context_messages=max_context_messages,
            max_context_tokens=max_context_tokens,
            context_token_counter=context_token_counter,
            # S2-T2 分层讲解：只有助学 Agent 需要按状态动态调整提示词
            # （按 state["level"] 学生水平分层，见 prompts.py）；其余角色
            # 传 None，沿用静态 system_prompt，行为与改动前完全一致。
            prompt_builder=(
                (lambda state: learning_assistant_system_prompt(state.get("level")))
                if role is AgentRole.LEARNING_ASSISTANT
                else None
            ),
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
