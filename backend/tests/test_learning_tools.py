"""学习记录工具图接入测试（六大功能计划 P0-5 验收）。

覆盖：
1. 条件注册：注入 store 时两工具注册且角色授权正确；未注入时
   registry 工具清单与现状一致（零回归红线，pi 三轮审查 🔴2）；
2. scope 注入落库：模型工具参数不含 user_id，落库的用户标识来自
   图执行上下文（learning_scope），模型不可见不可控；
3. 用户隔离：A 用户的记录对 B 用户不可见（同一 store 实例）。

全部使用确定性替身模型（ScriptedModel），不依赖真实模型。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, BaseMessage

from core.graph_builder import CollaborativeAgentGraph
from core.learning import LearningRecordStore
from core.state import AgentRole


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


def _tool_names(graph: CollaborativeAgentGraph) -> list[str]:
    return [tool.name for tool in graph.registry.list_tools()]


def test_learning_tools_registered_only_when_store_injected(
    store: LearningRecordStore,
) -> None:
    with_store = CollaborativeAgentGraph(
        model=ScriptedModel([]), learning_store=store
    )
    without_store = CollaborativeAgentGraph(model=ScriptedModel([]))

    names_with = _tool_names(with_store)
    names_without = _tool_names(without_store)

    assert "record_learning_outcome" in names_with
    assert "get_learning_records" in names_with
    # 条件注册红线：无 store 时学习工具不出现（清单口径同
    # test_graph_accepts_empty_tools_and_permissions：调度/协议工具 +
    # P2-9 的两个批改工具是模块级注册，恒在清单里）。
    assert names_without == [
        "handoff",
        "create_task_plan",
        "detect_intent",
        "detect_level",
        "submit_evaluation",
        "grade_objective_answers",
        "submit_grading",
    ]


def test_learning_tools_role_authorization(store: LearningRecordStore) -> None:
    graph = CollaborativeAgentGraph(model=ScriptedModel([]), learning_store=store)

    for tool_name in ("record_learning_outcome", "get_learning_records"):
        assert graph.registry.is_authorized(tool_name, AgentRole.LEARNING_ASSISTANT)
        assert graph.registry.is_authorized(tool_name, AgentRole.EVALUATOR)
        assert not graph.registry.is_authorized(tool_name, AgentRole.SUPERVISOR)
        assert not graph.registry.is_authorized(tool_name, AgentRole.TEACHING_ASSISTANT)


def _record_flow_responses() -> list[AIMessage]:
    """supervisor handoff → supervisor 收尾 → worker 记录工具调用 →
    worker 作答 → supervisor 聚合（handoff 后 supervisor 需一条无
    工具调用的响应结束节点，图才路由到 worker，同
    test_intent_recognition 的脚本构造惯例）。"""
    return [
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
        AIMessage(content="转交助学助手处理。"),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "record_learning_outcome",
                    "args": {
                        "knowledge_point": "梯度下降",
                        "outcome": "incorrect",
                        "error_tag": "概念不清",
                    },
                    "id": "record-1",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content="已记录你的练习情况，我们继续讲解。"),
        AIMessage(content="好的，已为你记录本次练习结果。"),
    ]


def test_scope_injected_user_id_is_persisted_not_model_argument(
    store: LearningRecordStore,
) -> None:
    """验收：落库的 user_id/session_id 来自图执行上下文（scope），
    模型工具参数里没有任何用户标识字段（schema 也不暴露）。"""
    graph = CollaborativeAgentGraph(
        model=ScriptedModel(_record_flow_responses()), learning_store=store
    )

    graph.run("我刚做错了梯度下降的题", "learning-session", user_id="student-a")

    summary = store.summarize("student-a")
    assert summary["total_attempts"] == 1
    point = summary["knowledge_points"][0]
    assert point["knowledge_point"] == "梯度下降"
    # 工具输入 schema 不暴露 user_id（模型不可见不可控）
    record_tool = graph.registry.get("record_learning_outcome")
    assert record_tool is not None
    assert record_tool.args_schema is not None
    schema_properties = record_tool.args_schema.model_json_schema()["properties"]
    assert "user_id" not in schema_properties
    assert "session_id" not in schema_properties


def test_records_are_isolated_between_users(store: LearningRecordStore) -> None:
    graph_a = CollaborativeAgentGraph(
        model=ScriptedModel(_record_flow_responses()), learning_store=store
    )
    graph_a.run("我错了", "session-a", user_id="student-a")

    # 另一用户读聚合：看不到 student-a 的记录
    summary_b = store.summarize("student-b")
    assert summary_b["total_attempts"] == 0


def test_get_learning_records_returns_scope_summary(
    store: LearningRecordStore,
) -> None:
    store.append_record(
        "student-a", knowledge_point="反向传播", outcome="incorrect", kind="answer"
    )
    store.append_record(
        "student-a", knowledge_point="反向传播", outcome="incorrect", kind="grading"
    )
    responses = [
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
        AIMessage(content="转交助学助手处理。"),
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
        AIMessage(content="你在反向传播上连续出错，需要重点巩固。"),
        AIMessage(content="已分析你的学习记录。"),
    ]
    graph = CollaborativeAgentGraph(
        model=ScriptedModel(responses), learning_store=store
    )

    result = graph.run("帮我看看学习情况", "session-read", user_id="student-a")

    # 聚合经工具观察到达 worker：ToolMessage 携带 scope 用户的
    # 聚合（含 weak_points 预警）与 user_id，模型据此写诊断叙述。
    tool_messages = [
        message
        for message in result["messages"]
        if getattr(message, "tool_call_id", None) == "read-1"
    ]
    assert len(tool_messages) == 1
    observation = json.loads(str(tool_messages[0].content))
    assert observation["user_id"] == "student-a"
    assert observation["total_attempts"] == 2
    assert observation["weak_points"] == ["反向传播"]


def test_record_tool_observation_reports_insertion(
    store: LearningRecordStore,
) -> None:
    """工具观察 JSON 携带 recorded 标记，模型可据此向学生确认。"""
    graph = CollaborativeAgentGraph(
        model=ScriptedModel(_record_flow_responses()), learning_store=store
    )
    result = graph.run("我错了", "session-obs", user_id="student-a")

    tool_messages = [
        message
        for message in result["messages"]
        if getattr(message, "tool_call_id", None) == "record-1"
    ]
    assert len(tool_messages) == 1
    observation = json.loads(str(tool_messages[0].content))
    assert observation["recorded"] is True
    assert observation["knowledge_point"] == "梯度下降"
