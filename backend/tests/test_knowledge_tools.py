"""知识检索工具与 Agent 权限接入测试。"""

from __future__ import annotations

import json
from collections.abc import Sequence

import pytest
from langchain_core.messages import AIMessage, BaseMessage

from core.events import ErrorCode
from core.graph_builder import CollaborativeAgentGraph
from core.knowledge.index import InMemoryKnowledgeIndex
from core.knowledge.models import KnowledgeDocument
from core.knowledge.service import KnowledgeService
from core.knowledge.tools import create_search_knowledge_tool
from core.state import AgentRole
from core.tools import ToolExecutor


class NoopModel:
    """只用于构建图，不发起模型调用。"""

    def bind_tools(self, tools: Sequence[object]) -> NoopModel:
        return self

    def invoke(self, messages: list[BaseMessage]) -> AIMessage:
        return AIMessage(content="unused")


def _service_with_content() -> KnowledgeService:
    service = KnowledgeService(InMemoryKnowledgeIndex(), chunk_size=50, overlap=0)
    service.add_documents(
        [
            KnowledgeDocument(
                document_id="algebra",
                content="一元二次方程可以使用求根公式求解。",
                source="algebra.txt",
            )
        ]
    )
    return service


def test_search_knowledge_tool_returns_content_and_traceable_citation() -> None:
    search_tool = create_search_knowledge_tool(_service_with_content())

    result = search_tool.invoke({"query": "一元二次方程", "top_k": 2})

    assert result["found"] is True
    assert result["hits"][0]["content"] == "一元二次方程可以使用求根公式求解。"
    citation = result["hits"][0]["citation"]
    assert citation["document_id"] == "algebra"
    assert citation["source"] == "algebra.txt"
    assert citation["page"] is None
    assert citation["chunk_id"].startswith("algebra")


def test_search_knowledge_tool_returns_explicit_empty_result() -> None:
    search_tool = create_search_knowledge_tool(
        KnowledgeService(InMemoryKnowledgeIndex())
    )

    result = search_tool.invoke({"query": "不存在的知识"})

    assert result == {
        "found": False,
        "message": "未找到可引用的知识片段",
        "hits": [],
    }


def test_tool_executor_turns_search_dict_into_json_observation() -> None:
    search_tool = create_search_knowledge_tool(_service_with_content())
    execution = ToolExecutor([search_tool]).execute(
        {
            "name": "search_knowledge",
            "args": {"query": "求根公式"},
            "id": "search-1",
        },
        AgentRole.TEACHING_ASSISTANT,
    )

    observation = json.loads(str(execution.message.content))
    assert execution.result.success is True
    assert execution.result.output == execution.message.content
    assert observation["found"] is True
    assert observation["hits"][0]["citation"]["document_id"] == "algebra"


@pytest.mark.parametrize(
    "args",
    [
        {"query": "   ", "top_k": 5},
        {"query": "求根公式", "top_k": 0},
        {"query": "求根公式", "top_k": 11},
    ],
)
def test_search_tool_schema_rejects_invalid_arguments(
    args: dict[str, object],
) -> None:
    search_tool = create_search_knowledge_tool(_service_with_content())

    execution = ToolExecutor([search_tool]).execute(
        {"name": "search_knowledge", "args": args, "id": "invalid-search"},
        AgentRole.TEACHING_ASSISTANT,
    )

    assert execution.result.error_code is ErrorCode.TOOL_INVALID_ARGUMENTS


def test_search_tool_schema_describes_model_visible_limits() -> None:
    search_tool = create_search_knowledge_tool(_service_with_content())

    properties = search_tool.get_input_schema().model_json_schema()["properties"]

    assert properties["query"]["minLength"] == 1
    assert properties["top_k"]["minimum"] == 1
    assert properties["top_k"]["maximum"] == 10


def test_graph_allows_workers_but_rejects_supervisor_search() -> None:
    search_tool = create_search_knowledge_tool(_service_with_content())
    allowed_roles = {
        AgentRole.TEACHING_ASSISTANT,
        AgentRole.LEARNING_ASSISTANT,
        AgentRole.EVALUATOR,
    }
    graph = CollaborativeAgentGraph(
        model=NoopModel(),
        tools=[search_tool],
        tool_permissions={"search_knowledge": allowed_roles},
    )

    assert all(
        graph.registry.is_authorized("search_knowledge", role)
        for role in allowed_roles
    )
    execution = ToolExecutor(graph.registry).execute(
        {
            "name": "search_knowledge",
            "args": {"query": "求根公式"},
            "id": "search-2",
        },
        AgentRole.SUPERVISOR,
    )
    assert execution.result.error_code is ErrorCode.TOOL_UNAUTHORIZED
