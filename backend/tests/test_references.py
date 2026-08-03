"""S2-T4 最终回答结构化引用插入测试。

覆盖验收标准：
1. Agent 使用检索工具（search_knowledge）证据作答时，最终回答消息的
   additional_kwargs["references"] 携带结构化引用列表（document_id、
   source、page、chunk_id，复用 core.knowledge.models.Citation），与
   本轮真实 SearchHit 的 Citation 一一对应、字段正确；
2. 引用只来自本轮真实命中：未调用检索工具不携带引用、检索无命中
   （found=False）不携带引用（不伪造）；
3. 引用按 chunk_id 去重、编号稳定（列表顺序 = 工具结果出现顺序）；
4. 引用随消息元数据经 SQLite checkpointer 持久化，get_history() 可恢复
   （与 S2-T1 角色元数据同机制）；
5. 与 S2-T3 评价/意图协同不回归：检索工具仍被纳入
   EvaluationResult.evidence_tool_names，引用与角色元数据并存，中间
   带 tool_calls 的助手消息不挂引用。

全部使用确定性替身模型（ScriptedModel）+ 真实知识检索链路
（InMemoryKnowledgeIndex + KnowledgeService + create_search_knowledge_tool），
不依赖真实模型。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from langchain_core.messages import AIMessage, BaseMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from core.graph_builder import CollaborativeAgentGraph
from core.knowledge.index import InMemoryKnowledgeIndex
from core.knowledge.models import Citation, KnowledgeDocument
from core.knowledge.service import KnowledgeService
from core.knowledge.tools import create_search_knowledge_tool
from core.persistence import open_sqlite_checkpointer
from core.state import (
    REFERENCES_METADATA_KEY,
    AgentRole,
    EvaluationResult,
    EvaluationVerdict,
    Intent,
    message_agent_role,
    message_references,
    with_references,
)


class ScriptedModel:
    """按图执行顺序返回预设消息（确定性替身，不依赖真实模型）。"""

    def __init__(self, responses: Sequence[AIMessage]) -> None:
        self.responses = list(responses)

    def bind_tools(self, tools: Sequence[object]) -> ScriptedModel:
        return self

    def invoke(self, messages: list[BaseMessage]) -> AIMessage:
        return self.responses.pop(0)


# ── 替身响应构造 ───────────────────────────────────────────


def _intent_response(intent: str) -> AIMessage:
    """模型调用 detect_intent 工具并自报意图。"""
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "detect_intent",
                "args": {"intent": intent, "reason": ""},
                "id": f"refs-intent-{intent}",
                "type": "tool_call",
            }
        ],
    )


def _handoff_response(target: str) -> AIMessage:
    """模型调用 handoff 工具请求分派到指定 Worker。"""
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "handoff",
                "args": {"target": target},
                "id": f"refs-handoff-{target}",
                "type": "tool_call",
            }
        ],
    )


def _search_response(query: str, call_id: str = "refs-search") -> AIMessage:
    """模型调用真实检索工具 search_knowledge 获取证据。"""
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "search_knowledge",
                "args": {"query": query, "top_k": 5},
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def _submit_response(
    verdict: str,
    fact_accuracy: str,
    citation_completeness: str,
    reason: str = "",
) -> AIMessage:
    """模型调用 submit_evaluation 工具提交结构化评价。"""
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "submit_evaluation",
                "args": {
                    "verdict": verdict,
                    "fact_accuracy": fact_accuracy,
                    "citation_completeness": citation_completeness,
                    "reason": reason,
                },
                "id": "refs-submit-evaluation",
                "type": "tool_call",
            }
        ],
    )


# ── 测试基础设施 ───────────────────────────────────────────


def _service_with_documents() -> KnowledgeService:
    """真实知识服务：两个文档各一个 chunk（词法索引，命中可预期）。"""
    service = KnowledgeService(InMemoryKnowledgeIndex(), chunk_size=200, overlap=0)
    service.add_documents(
        [
            KnowledgeDocument(
                document_id="algebra",
                content="一元二次方程可以使用求根公式求解。",
                source="algebra.txt",
            ),
            KnowledgeDocument(
                document_id="calculus",
                content="梯度下降沿负梯度方向迭代更新参数。",
                source="calculus.txt",
            ),
        ]
    )
    return service


def _search_answer_script() -> list[AIMessage]:
    """「答疑意图 → learning_assistant 检索并作答 → supervisor 汇总」脚本。

    响应数量与图执行 invoke 次数严格一一对应：
    - supervisor 轮：detect_intent → handoff → 无工具收尾回答；
    - learning_assistant 轮：search_knowledge → 无工具回答；
    - supervisor 返回轮：最终汇总（无工具）。
    """
    return [
        _intent_response("answer_question"),
        _handoff_response("learning_assistant"),
        AIMessage(content="任务已分派"),
        _search_response("一元二次方程", call_id="refs-search-1"),
        AIMessage(content="一元二次方程可以使用求根公式求解。"),
        AIMessage(content="最终汇总"),
    ]


def _terminal_ai_messages(messages: Sequence[BaseMessage]) -> list[AIMessage]:
    """按出现顺序取出无工具调用的助手消息（含中间轮与最终回答）。"""
    return [
        message
        for message in messages
        if isinstance(message, AIMessage) and not message.tool_calls
    ]


def _worker_search_graph(
    service: KnowledgeService,
    model: ScriptedModel,
    *,
    checkpointer: BaseCheckpointSaver[str] | None = None,
) -> CollaborativeAgentGraph:
    """构造「worker 可检索、supervisor 不可检索」的图（与既有权限约定一致）。"""
    search_tool = create_search_knowledge_tool(service)
    return CollaborativeAgentGraph(
        model=model,
        tools=[search_tool],
        tool_permissions={
            "search_knowledge": {
                AgentRole.TEACHING_ASSISTANT,
                AgentRole.LEARNING_ASSISTANT,
                AgentRole.EVALUATOR,
            }
        },
        checkpointer=checkpointer,
    )


# ── 有检索必带引用（验收核心） ──────────────────────────────


def test_search_backed_answer_carries_references() -> None:
    """验收：检索作答的最终回答携带结构化引用，与 SearchHit 一一对应。"""
    service = _service_with_documents()
    graph = _worker_search_graph(
        service,
        ScriptedModel(_search_answer_script()),
    )

    result = graph.run("请用知识库解释一元二次方程", "refs-answer")

    assert result["run_error"] is None
    # 引用唯一事实来源：本轮真实 SearchHit 的 Citation（重新检索对照）
    expected = [hit.citation for hit in service.search("一元二次方程", top_k=5)]
    assert len(expected) >= 1
    # 无工具助手消息依次为：任务已分派(supervisor)、检索回答(la)、
    # 最终汇总(supervisor)——检索回答是「使用检索证据作答」的那条消息
    ai = _terminal_ai_messages(result["messages"])
    answer = ai[1]
    references = message_references(answer)
    assert references == expected
    # 字段逐一验证（document_id / source / page / chunk_id）
    assert references[0].document_id == "algebra"
    assert references[0].source == "algebra.txt"
    assert references[0].page is None
    assert references[0].chunk_id == expected[0].chunk_id
    # 元数据存 dict 列表（msgpack 原生类型，保证 checkpoint 往返）
    raw = answer.additional_kwargs[REFERENCES_METADATA_KEY]
    assert isinstance(raw, list)
    assert all(isinstance(item, dict) for item in raw)
    # 未检索的 Supervisor 汇总回答不携带引用（引用跟随使用证据作答的消息）
    assert message_references(ai[2]) is None
    assert REFERENCES_METADATA_KEY not in ai[2].additional_kwargs
    # 内容本身不被引用注入改动
    assert answer.content == "一元二次方程可以使用求根公式求解。"


def test_tool_call_messages_carry_no_references() -> None:
    """中间带 tool_calls 的助手消息（工具调用请求）不挂引用。"""
    service = _service_with_documents()
    graph = _worker_search_graph(
        service,
        ScriptedModel(_search_answer_script()),
    )

    result = graph.run("请用知识库解释一元二次方程", "refs-tool-calls")

    for message in result["messages"]:
        if isinstance(message, AIMessage) and message.tool_calls:
            assert REFERENCES_METADATA_KEY not in message.additional_kwargs


# ── 零命中不伪造引用 ───────────────────────────────────────


def test_answer_without_search_has_no_references() -> None:
    """未使用检索证据的回答不携带引用（直接回答场景）。"""
    graph = CollaborativeAgentGraph(
        model=ScriptedModel([AIMessage(content="直接回答")])
    )

    result = graph.run("你好", "refs-no-search")

    ai = _terminal_ai_messages(result["messages"])
    assert message_references(ai[0]) is None
    assert REFERENCES_METADATA_KEY not in ai[0].additional_kwargs


def test_search_without_hits_produces_no_references() -> None:
    """检索无命中（found=False）不伪造引用。"""
    empty_service = KnowledgeService(InMemoryKnowledgeIndex())
    script = [
        _intent_response("answer_question"),
        _handoff_response("learning_assistant"),
        AIMessage(content="任务已分派"),
        _search_response("不存在的知识", call_id="refs-search-empty"),
        AIMessage(content="未找到相关内容。"),
        AIMessage(content="最终汇总"),
    ]
    graph = _worker_search_graph(empty_service, ScriptedModel(script))

    result = graph.run("查一下不存在的知识", "refs-no-hits")

    ai = _terminal_ai_messages(result["messages"])
    assert message_references(ai[1]) is None
    assert REFERENCES_METADATA_KEY not in ai[1].additional_kwargs


# ── 去重与编号稳定 ─────────────────────────────────────────


def test_references_deduplicated_by_chunk_id_and_ordered() -> None:
    """引用按 chunk_id 去重，编号稳定（顺序 = 工具结果出现顺序）。"""
    service = _service_with_documents()
    script = [
        _intent_response("answer_question"),
        _handoff_response("learning_assistant"),
        AIMessage(content="任务已分派"),
        # 第一次检索命中 algebra chunk
        _search_response("一元二次方程", call_id="refs-search-1"),
        # 第二次检索命中 calculus chunk（不同 chunk_id，应保留）
        _search_response("梯度下降", call_id="refs-search-2"),
        # 第三次检索与第一次同 query，命中同一 chunk（应去重）
        _search_response("求根公式", call_id="refs-search-3"),
        AIMessage(content="两个主题都介绍一下。"),
        AIMessage(content="最终汇总"),
    ]
    graph = _worker_search_graph(service, ScriptedModel(script))

    result = graph.run("解释方程并介绍梯度下降", "refs-dedupe")

    assert result["run_error"] is None
    ai = _terminal_ai_messages(result["messages"])
    references = message_references(ai[1])
    assert references is not None
    algebra_hit = service.search("一元二次方程", top_k=5)[0].citation
    calculus_hit = service.search("梯度下降", top_k=5)[0].citation
    assert algebra_hit.chunk_id != calculus_hit.chunk_id
    # 去重后与两次不同命中的顺序一致（编号稳定可预期）
    assert [ref.chunk_id for ref in references] == [
        algebra_hit.chunk_id,
        calculus_hit.chunk_id,
    ]
    assert len(references) == 2


# ── checkpoint 持久化（消息元数据可恢复） ───────────────────


def test_references_persist_in_sqlite_checkpoint(tmp_path: Path) -> None:
    """SQLite checkpointer 持久化 → 新连接/新实例 get_history 恢复引用。

    与角色元数据的验证方式一致（test_agent_role_metadata）：模拟进程
    重建（连接关闭后以新连接、新图实例重载），验证 additional_kwargs
    的引用列表经 msgpack 序列化往返不失真。
    """
    checkpoint_path = tmp_path / "nested" / "refs-checkpoints.sqlite"
    session_id = "refs-round-trip"
    user_id = "user-1"
    service = _service_with_documents()

    # 第一段：写入并持久化
    with open_sqlite_checkpointer(checkpoint_path) as first_saver:
        first_graph = _worker_search_graph(
            service,
            ScriptedModel(_search_answer_script()),
            checkpointer=first_saver,
        )
        first_graph.run("请用知识库解释一元二次方程", session_id, user_id)

    # 第二段：连接已关闭，以新连接 + 全新图实例重载历史（进程重建模拟）
    with open_sqlite_checkpointer(checkpoint_path) as second_saver:
        second_graph = _worker_search_graph(
            service,
            ScriptedModel([AIMessage(content="第二轮回答")]),
            checkpointer=second_saver,
        )
        history = second_graph.get_history(session_id, user_id)

    ai = _terminal_ai_messages(history)
    assert [message.content for message in ai] == [
        "任务已分派",
        "一元二次方程可以使用求根公式求解。",
        "最终汇总",
    ]
    expected = [hit.citation for hit in service.search("一元二次方程", top_k=5)]
    assert message_references(ai[1]) == expected


def test_references_restored_with_in_memory_checkpointer() -> None:
    """InMemory checkpointer 下 get_history 同样恢复引用（快速持久化验证）。"""
    service = _service_with_documents()
    graph = _worker_search_graph(
        service,
        ScriptedModel(_search_answer_script()),
        checkpointer=InMemorySaver(),
    )
    session_id = "refs-inmemory"
    graph.run("请用知识库解释一元二次方程", session_id, "user-1")

    history = graph.get_history(session_id, "user-1")

    ai = _terminal_ai_messages(history)
    expected = [hit.citation for hit in service.search("一元二次方程", top_k=5)]
    assert message_references(ai[1]) == expected


# ── 与 S2-T3 评价 / S2-T1 意图协同不回归 ───────────────────


def test_references_coexist_with_evaluation_and_intent() -> None:
    """检索 + 评价：引用挂在评价回答上，检索工具纳入证据名，意图正常。"""
    service = _service_with_documents()
    script = [
        _intent_response("evaluation"),
        _handoff_response("evaluator"),
        AIMessage(content="任务已分派"),
        _search_response("一元二次方程", call_id="refs-eval-search"),
        _submit_response("pass", "pass", "pass", "依据检索证据，回答准确且引用完整"),
        AIMessage(content="评价完成。"),
        AIMessage(content="最终汇总"),
    ]
    graph = _worker_search_graph(service, ScriptedModel(script))

    result = graph.run("请评价这段回答", "refs-with-evaluation")

    assert result["run_error"] is None
    assert result["intent"] == Intent.EVALUATION
    # S2-T3 协同：检索工具已天然纳入本轮评价证据（向后兼容不回归）
    evaluation = result["evaluation"]
    assert isinstance(evaluation, EvaluationResult)
    assert evaluation.verdict == EvaluationVerdict.PASS
    assert evaluation.evidence_tool_names == ["search_knowledge"]
    # 评价回答（无工具助手消息第 2 条）携带引用与角色元数据（并存）
    ai = _terminal_ai_messages(result["messages"])
    expected = [hit.citation for hit in service.search("一元二次方程", top_k=5)]
    assert message_references(ai[1]) == expected
    assert message_agent_role(ai[1]) == AgentRole.EVALUATOR
    assert ai[1].content == "评价完成。"


# ── 写入/读取端契约（与 message_agent_role 同哲学） ────────


def test_with_references_returns_copy_and_preserves_existing_kwargs() -> None:
    """注入函数不改原对象，且保留既有 additional_kwargs 与内容。"""
    original = AIMessage(content="回答", additional_kwargs={"provider": "x"})
    citation = Citation(document_id="algebra", source="algebra.txt", chunk_id="c1")

    tagged = with_references(original, [citation])

    assert tagged.additional_kwargs["provider"] == "x"
    assert tagged.additional_kwargs[REFERENCES_METADATA_KEY] == [
        citation.model_dump(mode="json")
    ]
    assert tagged.content == "回答"
    # 原对象未被就地修改（副本语义，避免污染模型返回对象）
    assert REFERENCES_METADATA_KEY not in original.additional_kwargs
    # 空序列防御：不注入空的 references 键（零命中语义，接口契约完整性）
    empty = with_references(original, [])
    assert empty is original
    assert REFERENCES_METADATA_KEY not in empty.additional_kwargs


def test_message_references_tolerates_missing_or_invalid_metadata() -> None:
    """读取端宽容：缺失/非法元数据返回 None，脏数据项跳过不崩溃。"""
    assert message_references(AIMessage(content="hi")) is None
    non_list = AIMessage(
        content="hi",
        additional_kwargs={REFERENCES_METADATA_KEY: "not-a-list"},
    )
    assert message_references(non_list) is None
    mixed = AIMessage(
        content="hi",
        additional_kwargs={
            REFERENCES_METADATA_KEY: [
                {"document_id": "d", "source": "s.txt", "chunk_id": "c1"},
                {"document_id": "d", "source": "s.txt"},  # 缺 chunk_id → 跳过
                "not-a-dict",  # 非 dict → 跳过
                {"document_id": "d", "source": "s.txt", "chunk_id": "c2"},
            ]
        },
    )
    references = message_references(mixed)
    assert references is not None
    assert [ref.chunk_id for ref in references] == ["c1", "c2"]
