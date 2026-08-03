"""作业批改工具层测试：校验、身份绑定、落库闭环与权限接入。"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, BaseMessage
from pdf_fixture import write_pdf

from core.assignments import AssignmentStore, create_assignment_tools
from core.events import ErrorCode
from core.graph_builder import CollaborativeAgentGraph
from core.state import AgentRole
from core.tools import ToolExecutor

_ANSWER_KEY = {
    "version": "1",
    "items": [
        {
            "question_id": "q1",
            "kind": "objective",
            "correct_answer": "B",
            "points": 10,
            "knowledge_points": ["浮点数"],
        },
        {
            "question_id": "q2",
            "kind": "objective",
            "correct_answer": "0.5",
            "points": 10,
            "knowledge_points": ["浮点数"],
        },
        {
            "question_id": "q3",
            "kind": "subjective",
            "keyword_hints": ["收敛", "学习率"],
            "points": 20,
            "knowledge_points": ["梯度下降"],
        },
    ],
}


class NoopModel:
    """只用于构建图，不发起模型调用。"""

    def bind_tools(self, tools: Sequence[object]) -> NoopModel:
        return self

    def invoke(self, messages: list[BaseMessage]) -> AIMessage:
        return AIMessage(content="unused")


def test_grade_tool_rejects_invalid_answer_key_via_schema(tmp_path: Path) -> None:
    store = AssignmentStore(tmp_path / "assignments.sqlite")
    try:
        tools = create_assignment_tools(store, user_id="u1", session_id="s1")
        grade_tool = tools[2]
        executor = ToolExecutor([grade_tool])

        execution = executor.execute(
            {
                "name": "grade_submission",
                "args": {
                    "questions": [{"question_id": "q1", "answer": "B"}],
                    "answer_key": {
                        "items": [
                            {
                                "question_id": "q1",
                                "kind": "objective",
                                "points": 10,
                            }
                        ]
                    },
                },
            },
            AgentRole.EVALUATOR,
        )

        assert execution.result.success is False
        assert execution.result.error_code is ErrorCode.TOOL_INVALID_ARGUMENTS
    finally:
        store.close()


def test_grade_then_analyze_is_a_closed_loop(tmp_path: Path) -> None:
    store = AssignmentStore(tmp_path / "assignments.sqlite")
    try:
        tools = create_assignment_tools(store, user_id="u1", session_id="s1")
        grade_tool, analyze_tool = tools[2], tools[3]

        graded = grade_tool.invoke(
            {
                "questions": [
                    {"question_id": "q1", "answer": "B"},
                    {"question_id": "q2", "answer": "0.5"},
                    {"question_id": "q3", "answer": "学习率决定收敛速度，需要仔细调参。"},
                ],
                "answer_key": _ANSWER_KEY,
                "title": "浮点数测验",
            }
        )
        report = analyze_tool.invoke(
            {"class_id": "class-1", "user_ids": ["u1"], "weak_threshold": 0.8}
        )

        assert graded["saved"] is True
        assert graded["total_score"] == 20 + graded["questions"][2]["score"]
        assert report["scope"]["submission_count"] == 1
        assert report["scope"]["student_count"] == 1
        by_point = {
            item["knowledge_point"]: item
            for item in report["knowledge_points"]
        }
        assert by_point["浮点数"]["accuracy"] == 1.0
        assert by_point["浮点数"]["is_weak"] is False
        # 主观题不参与准确率聚合，但薄弱点列表按阈值标记缺失知识点不计
        assert any(item["knowledge_point"] == "浮点数" for item in report["knowledge_points"])
    finally:
        store.close()


def test_parse_tool_classifies_missing_file_as_environment_error(tmp_path: Path) -> None:
    store = AssignmentStore(tmp_path / "assignments.sqlite")
    try:
        tools = create_assignment_tools(store, user_id="u1", session_id="s1")
        parse_tool = tools[0]
        executor = ToolExecutor([parse_tool])

        execution = executor.execute(
            {"name": "parse_upload", "args": {"path": str(tmp_path / "missing.pdf")}},
            AgentRole.EVALUATOR,
        )

        assert execution.result.success is False
        assert execution.result.error_code is ErrorCode.TOOL_EXECUTION_FAILED
    finally:
        store.close()


def test_parse_tool_returns_structured_result_for_text_pdf(tmp_path: Path) -> None:
    store = AssignmentStore(tmp_path / "assignments.sqlite")
    try:
        source = tmp_path / "homework.pdf"
        write_pdf(source, ["Question 1"])
        tools = create_assignment_tools(
            store,
            user_id="u1",
            session_id="s1",
            upload_root=tmp_path,
        )

        result = tools[0].invoke({"path": str(source)})

        assert result["ok"] is True
        assert result["pdf_type"] == "text_based"
    finally:
        store.close()


def test_graph_requires_permissions_for_assignment_tools() -> None:
    store = AssignmentStore(":memory:")
    try:
        tools = create_assignment_tools(store, user_id="u1", session_id="s1")

        with pytest.raises(ValueError, match="缺少业务工具"):
            CollaborativeAgentGraph(
                model=NoopModel(),
                tools=list(tools),
            )
    finally:
        store.close()


def test_graph_accepts_assignment_tools_with_full_permissions() -> None:
    store = AssignmentStore(":memory:")
    try:
        tools = create_assignment_tools(store, user_id="u1", session_id="s1")
        names = [item.name for item in tools]
        permissions = {
            name: {AgentRole.TEACHING_ASSISTANT, AgentRole.LEARNING_ASSISTANT, AgentRole.EVALUATOR}
            for name in names
        }

        graph = CollaborativeAgentGraph(
            model=NoopModel(),
            tools=list(tools),
            tool_permissions=permissions,
        )

        registered = {item.name for item in graph.registry.list_tools()}
        assert set(names) <= registered  # 额外包含自动注册的 handoff
    finally:
        store.close()
