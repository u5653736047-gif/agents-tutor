"""工具注册表测试。"""

import pytest
from langchain_core.tools import tool

from core.state import AgentRole
from core.tools.registry import ToolRegistry


@tool
def lookup(topic: str) -> str:
    """查询主题。"""
    return topic


def test_registry_rejects_duplicate_tool_names() -> None:
    registry = ToolRegistry([lookup])

    with pytest.raises(ValueError, match="lookup"):
        registry.register(lookup)


def test_registry_lists_and_finds_tools_by_name() -> None:
    registry = ToolRegistry([lookup])

    assert registry.list_tools() == (lookup,)
    assert registry.get("lookup") is lookup
    assert registry.get("missing") is None


def test_registry_defaults_to_all_roles() -> None:
    registry = ToolRegistry([lookup])

    assert all(registry.is_authorized("lookup", role) for role in AgentRole)


def test_registry_honors_explicit_allowed_roles() -> None:
    registry = ToolRegistry()
    registry.register(lookup, allowed_roles={AgentRole.EVALUATOR})

    assert registry.is_authorized("lookup", AgentRole.EVALUATOR) is True
    assert registry.is_authorized("lookup", AgentRole.TEACHING_ASSISTANT) is False
