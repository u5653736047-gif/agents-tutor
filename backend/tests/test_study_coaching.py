"""学习过程陪伴测试（六大功能计划 P5 验收）。

覆盖：
1. intent 感知动态段：study_coaching 意图追加陪伴约定（锚点词断言：
   学伴/苏格拉底式/归因四分类），其余意图零回归不追加；
2. 错题归因落库：陪伴轮次 record_learning_outcome 记录 incorrect +
   error_tag 归因标签，反哺学情诊断（P3）与路径规划（P4）；
3. 动态段长度上限（coaching 段叠加后仍有界，防无边界膨胀）。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, BaseMessage

from core.graph_builder import CollaborativeAgentGraph
from core.learning import LearningRecordStore
from core.nodes.prompts import learning_assistant_system_prompt


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


def test_coaching_intent_appends_companion_guidance() -> None:
    prompt = learning_assistant_system_prompt("basic", "study_coaching")

    # 陪伴约定锚点词
    assert "学习陪伴模式" in prompt
    assert "学伴" in prompt
    assert "苏格拉底式" in prompt
    # 错题归因四分类
    assert "概念不清" in prompt
    assert "审题偏差" in prompt
    assert "计算失误" in prompt
    assert "方法选择" in prompt
    assert "record_learning_outcome" in prompt
    # 水平段仍在（两段叠加）
    assert "[当前学生水平:basic]" in prompt


def test_other_intents_do_not_append_coaching_guidance() -> None:
    for intent in (None, "answer_question", "learning_path", "evaluation"):
        prompt = learning_assistant_system_prompt("basic", intent)
        assert "学习陪伴模式" not in prompt


def test_coaching_and_path_guidance_can_stack() -> None:
    """两段动态约定互不冲突，可叠加（规划轮处于陪伴会话的场景）。"""
    prompt = learning_assistant_system_prompt("advanced", "study_coaching")
    assert "学习陪伴模式" in prompt
    prompt_path = learning_assistant_system_prompt("advanced", "learning_path")
    assert "学习路径规划" in prompt_path
    assert "学习陪伴模式" not in prompt_path


def test_dynamic_prompt_with_coaching_remains_bounded() -> None:
    """coaching 段叠加后动态提示词仍有界（防无边界膨胀；与
    test_agent_factory 的 620 上限同一哲学，coaching 段较长单独放宽）。"""
    lengths = [
        len(learning_assistant_system_prompt(level, intent))
        for level in (None, "basic", "advanced")
        for intent in ("study_coaching", "learning_path", None)
    ]
    assert max(lengths) <= 1100


# ── 错题归因落库 ──────────────────────────────────────────


def test_coaching_mistake_attribution_persists_error_tag(
    store: LearningRecordStore,
) -> None:
    """陪伴轮次的错题归因记录落库（error_tag 反哺诊断/路径规划）。"""
    responses = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "detect_intent",
                    "args": {"intent": "study_coaching", "reason": "错题分析"},
                    "id": "intent-1",
                    "type": "tool_call",
                }
            ],
        ),
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
        AIMessage(content="转交助学助手陪伴。"),
        # 归因：概念不清 → 记录 incorrect + error_tag
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "record_learning_outcome",
                    "args": {
                        "knowledge_point": "损失函数",
                        "outcome": "incorrect",
                        "error_tag": "概念不清",
                    },
                    "id": "record-1",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content="这道错题的归因是概念不清，我们来巩固一下。"),
        AIMessage(content="已完成错题归因。"),
    ]
    graph = CollaborativeAgentGraph(
        model=ScriptedModel(responses), learning_store=store
    )

    result = graph.run("帮我分析这道错题", "coaching-flow", user_id="student-a")

    assert result["run_error"] is None
    assert result["intent"] == "study_coaching"
    # 归因记录落库：知识点聚合可见 + 未达预警下限（仅 1 次）
    summary = store.summarize("student-a")
    assert summary["total_attempts"] == 1
    point = summary["knowledge_points"][0]
    assert point["knowledge_point"] == "损失函数"
    assert point["attempts"] == 1


def test_coaching_prompt_visible_to_worker_model(store: LearningRecordStore) -> None:
    """陪伴意图下 worker 看到的 system prompt 含陪伴约定（intent 感知
    动态段经 prompt_builder 钩子在真实图执行中生效）。"""
    responses = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "detect_intent",
                    "args": {"intent": "study_coaching", "reason": "陪伴"},
                    "id": "intent-1",
                    "type": "tool_call",
                }
            ],
        ),
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
        AIMessage(content="我们一起巩固。"),
        AIMessage(content="汇总。"),
    ]
    model = ScriptedModel(responses)
    graph = CollaborativeAgentGraph(model=model, learning_store=store)

    graph.run("陪我复习", "coaching-prompt", user_id="student-a")

    worker_call = model.calls[3]
    system_prompt = str(worker_call[0].content)
    assert "学习陪伴模式" in system_prompt
    assert "苏格拉底式" in system_prompt
