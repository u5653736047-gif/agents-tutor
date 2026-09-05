"""学习路径规划测试（六大功能计划 P4 验收）。

覆盖：
1. intent 感知动态段：learning_path 意图追加路径规划约定（锚点词
   断言），其余意图零回归不追加；
2. 全链路（替身模型）：supervisor 识别 learning_path → handoff →
   learning_assistant 按约定顺序调用 get_learning_records →
   search_knowledge（difficulty 过滤，P0-3）→ 输出路径 → 记录
   path_plan 摘要落库；
3. level 画像与路径规划提示词共存（水平段 + 规划段叠加）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, BaseMessage

from core.graph_builder import CollaborativeAgentGraph
from core.knowledge.index import InMemoryKnowledgeIndex
from core.knowledge.models import KnowledgeChunk
from core.knowledge.service import KnowledgeService
from core.knowledge.tools import create_search_knowledge_tool
from core.learning import LearningRecordStore
from core.nodes.prompts import learning_assistant_system_prompt
from core.state import AgentRole


def _search_tool_with_basic_chunks():
    """内存知识检索工具（带 difficulty 元数据，验证 P0-3 过滤链路）。"""
    index = InMemoryKnowledgeIndex()
    index.upsert(
        [
            KnowledgeChunk(
                chunk_id="c-bp-basic",
                document_id="doc-ml",
                content="反向传播的基础概念与直观解释",
                source="ml.txt",
                page=None,
                start=0,
                end=14,
                metadata={"difficulty": "basic"},
            )
        ]
    )
    return create_search_knowledge_tool(KnowledgeService(index))


class ScriptedModel:
    """按图执行顺序返回预设消息。"""

    def __init__(self, responses: Sequence[AIMessage]) -> None:
        self.responses = list(responses)
        self.calls: list[list[BaseMessage]] = []

    def bind_tools(self, tools: Sequence[object]) -> ScriptedModel:
        return self

    def invoke(self, messages: list[BaseMessage]) -> AIMessage:
        self.calls.append(list(messages))
        return self.responses.pop(0)


@pytest.fixture()
def store(tmp_path: Path) -> LearningRecordStore:
    record_store = LearningRecordStore(tmp_path / "learning.db")
    yield record_store
    record_store.close()


# ── intent 感知动态段 ─────────────────────────────────────


def test_learning_path_intent_appends_planning_guidance() -> None:
    prompt = learning_assistant_system_prompt("basic", "learning_path")

    # 规划约定锚点词
    assert "学习路径规划" in prompt
    assert "get_learning_records" in prompt
    assert "difficulty" in prompt
    assert "检验点" in prompt
    assert "path_plan" in prompt
    # 水平段仍在（两段叠加）
    assert "[当前学生水平:basic]" in prompt


def test_other_intents_do_not_append_planning_guidance() -> None:
    for intent in (None, "answer_question", "study_coaching", "evaluation"):
        prompt = learning_assistant_system_prompt("basic", intent)
        assert "学习路径规划" not in prompt


# ── 全链路：记录读取 → 难度检索 → 路径存档 ────────────────


def _intent_response(intent: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "detect_intent",
                "args": {"intent": intent, "reason": "规划学习路径"},
                "id": "intent-1",
                "type": "tool_call",
            }
        ],
    )


def test_learning_path_full_flow_reads_records_searches_and_archives(
    store: LearningRecordStore, tmp_path: Path
) -> None:
    # 预置薄弱点：反向传播连续出错（路径规划应先读这些记录）
    store.append_record(
        "student-a", knowledge_point="反向传播", outcome="incorrect", kind="grading"
    )
    store.append_record(
        "student-a", knowledge_point="反向传播", outcome="incorrect", kind="answer"
    )

    responses = [
        _intent_response("learning_path"),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "handoff",
                    "args": {"target": "learning_assistant"},
                    "id": "handoff-1",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content="转交助学助手规划。"),
        # worker 第 1 步：读学习记录
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "get_learning_records",
                    "args": {},
                    "id": "read-1",
                    "type": "tool_call",
                }
            ],
        ),
        # worker 第 2 步：按难度检索资源（P0-3 过滤参数）
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "search_knowledge",
                    "args": {"query": "反向传播", "difficulty": "basic"},
                    "id": "search-1",
                    "type": "tool_call",
                }
            ],
        ),
        # worker 第 3 步：存档路径摘要
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "record_learning_outcome",
                    "args": {
                        "knowledge_point": "反向传播",
                        "outcome": "partial",
                        "kind": "path_plan",
                    },
                    "id": "record-1",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content="学习路径：阶段一巩固反向传播……"),
        AIMessage(content="已为你规划学习路径。"),
    ]
    graph = CollaborativeAgentGraph(
        model=ScriptedModel(responses),
        learning_store=store,
        tools=[_search_tool_with_basic_chunks()],
        tool_permissions={
            "search_knowledge": {
                AgentRole.LEARNING_ASSISTANT,
                AgentRole.TEACHING_ASSISTANT,
                AgentRole.EVALUATOR,
            }
        },
    )

    result = graph.run("帮我规划学习路径", "path-flow", user_id="student-a")

    assert result["run_error"] is None
    assert result["intent"] == "learning_path"
    # 工具调用顺序：get_learning_records → search_knowledge → record
    #（过滤调度类工具 detect_intent/handoff，只看 worker 业务工具链）
    tool_names = [
        r.tool_name
        for r in result["tool_results"]
        if r.success and r.tool_name not in {"detect_intent", "handoff"}
    ]
    assert tool_names == [
        "get_learning_records",
        "search_knowledge",
        "record_learning_outcome",
    ]
    # 难度过滤参数真实传入检索且命中匹配 chunk（P0-3 透传链路）
    search_calls = [
        call
        for call in result["tool_results"]
        if call.tool_name == "search_knowledge"
    ]
    assert len(search_calls) == 1
    assert search_calls[0].success is True
    assert "c-bp-basic" in search_calls[0].output
    # 作答统计只计真实作答（预置 2 条）；path_plan 存档是路径标记，
    # 不计入作答总量（summarize 的 kind 过滤语义）
    summary = store.summarize("student-a")
    assert summary["total_attempts"] == 2
    # 路径摘要确实落库（kind=path_plan，直接查库验证存档）
    conn = sqlite3.connect(tmp_path / "learning.db")
    try:
        archived = conn.execute(
            "SELECT COUNT(*) FROM learning_records "
            "WHERE user_id = 'student-a' AND kind = 'path_plan'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert archived == 1


def test_planning_guidance_visible_to_worker_model(store: LearningRecordStore) -> None:
    """规划意图下 worker 看到的 system prompt 含规划约定（intent 感知
    动态段经 prompt_builder 钩子在真实图执行中生效）。"""
    responses = [
        _intent_response("learning_path"),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "handoff",
                    "args": {"target": "learning_assistant"},
                    "id": "handoff-1",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content="转交助学助手。"),
        AIMessage(content="路径已规划。"),
        AIMessage(content="汇总。"),
    ]
    model = ScriptedModel(responses)
    graph = CollaborativeAgentGraph(model=model, learning_store=store)

    graph.run("规划路径", "path-prompt", user_id="student-a")

    # worker 的模型调用（第 4 次，index 3）system prompt 含规划段
    worker_call = model.calls[3]
    system_prompt = str(worker_call[0].content)
    assert "学习路径规划" in system_prompt
    assert "get_learning_records" in system_prompt
