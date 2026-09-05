"""智能备课测试（六大功能计划 P6 验收）。

覆盖：
1. TEACHING_ASSISTANT 角色卡的六段教学设计模板与课标对齐约定
   （锚点词断言）；
2. 全链路（替身模型）：lesson_prep 意图 → handoff → 助教先检索
   （含 source 参数限定课标/教材，P0-3）再生成，工具调用顺序断言
   「先检索后生成」；
3. 生成回答包含六段结构锚点（替身响应模拟备课产出）。
"""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.messages import AIMessage, BaseMessage

from core.graph_builder import CollaborativeAgentGraph
from core.knowledge.index import InMemoryKnowledgeIndex
from core.knowledge.models import KnowledgeChunk
from core.knowledge.service import KnowledgeService
from core.knowledge.tools import create_search_knowledge_tool
from core.nodes.prompts import ROLE_PROMPTS
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


def _lesson_prep_search_tool():
    """内存检索工具：含教材 chunk 与课标 chunk。

    source 过滤键匹配 chunk 顶层 source 字段（index.py 契约：逻辑
    来源标识，与 ingest_books.py 注入口径一致），故此处 source 直接
    用逻辑标识（ml-zhouzhihua / cs-ai-curriculum）。
    """
    index = InMemoryKnowledgeIndex()
    index.upsert(
        [
            KnowledgeChunk(
                chunk_id="c-textbook",
                document_id="doc-ml",
                content="梯度下降是迭代优化算法",
                source="ml-zhouzhihua",
                page=None,
                start=0,
                end=11,
                metadata={"difficulty": "intermediate"},
            ),
            KnowledgeChunk(
                chunk_id="c-curriculum",
                document_id="doc-cs",
                content="课程目标：掌握优化算法基础",
                source="cs-ai-curriculum",
                page=None,
                start=0,
                end=13,
                metadata={"difficulty": "intermediate"},
            ),
        ]
    )
    return create_search_knowledge_tool(KnowledgeService(index))


# ── 角色卡约定 ────────────────────────────────────────────


def test_teaching_card_defines_six_section_template() -> None:
    prompt = ROLE_PROMPTS[AgentRole.TEACHING_ASSISTANT]

    # 六段教学设计模板锚点
    for section in ("教学目标", "重难点", "学情假设", "教学过程", "课堂活动", "评价设计"):
        assert section in prompt
    # 课标对齐约定（P6-20）
    assert "课程标准" in prompt
    assert "未对齐课标" in prompt
    # 既有检索约定不变
    assert "search_knowledge" in prompt
    assert "凭空编写" in prompt


# ── 全链路：先检索后生成 ──────────────────────────────────


def test_lesson_prep_searches_curriculum_before_generation() -> None:
    responses = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "detect_intent",
                    "args": {"intent": "lesson_prep", "reason": "备课"},
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
                    "args": {"target": "teaching_assistant"},
                    "id": "handoff-1",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content="转交助教备课。"),
        # 助教第 1 步：检索课标（source 参数限定，P0-3 过滤链路）
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "search_knowledge",
                    "args": {"query": "梯度下降 课程目标", "source": "cs-ai-curriculum"},
                    "id": "search-cs",
                    "type": "tool_call",
                }
            ],
        ),
        # 助教第 2 步：检索教材
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "search_knowledge",
                    "args": {"query": "梯度下降"},
                    "id": "search-tb",
                    "type": "tool_call",
                }
            ],
        ),
        # 助教生成六段教学设计
        AIMessage(
            content=(
                "教学设计：一、教学目标：掌握梯度下降；二、重难点：学习率；"
                "三、学情假设：有微积分基础；四、教学过程：讲解+演练；"
                "五、课堂活动：分组实验；六、评价设计：随堂测验。"
            )
        ),
        AIMessage(content="已生成教学设计。"),
    ]
    graph = CollaborativeAgentGraph(
        model=ScriptedModel(responses),
        tools=[_lesson_prep_search_tool()],
        tool_permissions={
            "search_knowledge": {
                AgentRole.LEARNING_ASSISTANT,
                AgentRole.TEACHING_ASSISTANT,
                AgentRole.EVALUATOR,
            }
        },
    )

    result = graph.run("帮我备一节梯度下降的课", "lesson-prep-flow")

    assert result["run_error"] is None
    assert result["intent"] == "lesson_prep"
    # 先检索后生成：业务工具全部是检索，且课标检索在前
    tool_names = [
        r.tool_name
        for r in result["tool_results"]
        if r.success and r.tool_name not in {"detect_intent", "handoff"}
    ]
    assert tool_names == ["search_knowledge", "search_knowledge"]
    # source 过滤真实生效：课标检索只命中课标 chunk
    search_results = [
        r for r in result["tool_results"] if r.tool_name == "search_knowledge"
    ]
    assert "c-curriculum" in search_results[0].output
    assert "c-textbook" not in search_results[0].output
    # 助教的作答消息包含六段结构（最终消息是 supervisor 聚合回答，
    # 六段正文在助教作答消息上——倒序查找，与 references「引用跟随
    # 作答消息」的同一口径）
    teaching_answers = [
        str(message.content)
        for message in reversed(result["messages"])
        if "教学目标" in str(getattr(message, "content", ""))
    ]
    assert len(teaching_answers) >= 1
    assert "课堂活动" in teaching_answers[0]
    assert "评价设计" in teaching_answers[0]
