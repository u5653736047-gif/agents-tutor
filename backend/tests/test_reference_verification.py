"""S2-T5 引用真实性校验与格式规范化测试。

覆盖验收标准：
1. 自动校验：最终回答消息中的每条引用必须存在于本轮检索结果——注入
   不在本轮命中的伪造引用（chunk_id 越界）被识别并剔除，合法引用
   不受影响；
2. 字段不匹配按伪造处置：chunk_id 真实但 document_id/source/page 被
   篡改的引用同样被剔除（「chunk_id 不在命中集、或字段不匹配」）；
3. 引用格式规范化：同一 document_id 的多个 chunk 引用合并为一条
   （保留首次出现的 chunk），输出稳定可解析；
4. 校验结论在评价结果中体现：evaluator 轮的
   EvaluationResult.reference_verification 携带本轮校验结论，同时
   state["reference_verification"] 写入同一结论；
5. 零引用/无检索场景不破坏：直接回答/检索无命中不写校验结论、不挂
   引用；无检索但消息被注入引用 → 全部剔除并剥离（不留残迹）；
6. 生命周期：校验结论随 checkpoint 持久化（get_state 可读）、新轮
   重置为 None；
7. 向后兼容：旧 checkpoint 的 EvaluationResult 无 reference_verification
   字段时校验为 None；ReferenceVerification 序列化往返不失真。

全部使用确定性替身模型（ScriptedModel）+ 真实知识检索链路
（InMemoryKnowledgeIndex + KnowledgeService + create_search_knowledge_tool），
不依赖真实模型（与 test_references.py 同一模式）。

维护点（M-3）：涉及词法检索得分/排序的断言（test_same_document_
multiple_chunks_merged 的 short-doc 4 分 vs long-doc 3 分等）依赖
InMemoryKnowledgeIndex 词法索引的确定性，Sprint 3 换向量索引后需
复核（与 S2-T4 test_references.py 同一维护点，见 TASKS_M3_CLOSE Sprint5 / SIX_FEATURES §二
S2-T4 备注）。
"""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.messages import AIMessage, BaseMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from core.graph_builder import CollaborativeAgentGraph
from core.knowledge.index import InMemoryKnowledgeIndex
from core.knowledge.models import Citation, KnowledgeDocument
from core.knowledge.service import KnowledgeService
from core.knowledge.tools import create_search_knowledge_tool
from core.state import (
    REFERENCES_METADATA_KEY,
    AgentRole,
    EvaluationResult,
    EvaluationVerdict,
    ReferenceVerification,
    message_references,
)


class ScriptedModel:
    """按图执行顺序返回预设消息（确定性替身，不依赖真实模型）。"""

    def __init__(self, responses: Sequence[AIMessage]) -> None:
        self.responses = list(responses)

    def bind_tools(self, tools: Sequence[object]) -> ScriptedModel:
        return self

    def invoke(self, messages: list[BaseMessage]) -> AIMessage:
        return self.responses.pop(0)


# ── 替身响应构造（与 test_references.py 同一模式） ──────────


def _intent_response(intent: str) -> AIMessage:
    """模型调用 detect_intent 工具并自报意图。"""
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "detect_intent",
                "args": {"intent": intent, "reason": ""},
                "id": f"verify-intent-{intent}",
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
                "id": f"verify-handoff-{target}",
                "type": "tool_call",
            }
        ],
    )


def _search_response(query: str, call_id: str = "verify-search") -> AIMessage:
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
                "id": "verify-submit-evaluation",
                "type": "tool_call",
            }
        ],
    )


def _plan_response() -> AIMessage:
    """模型调用 create_task_plan 创建「讲解 → 检查」两步骤计划。

    与 test_evaluation.py 的计划流程模式一致：teaching_assistant 先
    执行（本测试中检索作答），evaluator 随后执行（本测试中只评价不
    检索）——这是 I-1「worker 轮校验结论在评价结果中体现」的载体。
    """
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "create_task_plan",
                "args": {
                    "steps": [
                        {
                            "sequence": 1,
                            "description": "讲解一元二次方程",
                            "target_agent": "teaching_assistant",
                        },
                        {
                            "sequence": 2,
                            "description": "检查讲解准确性",
                            "target_agent": "evaluator",
                        },
                    ]
                },
                "id": "verify-plan",
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

    响应数量与图执行 invoke 次数严格一一对应（缺一条会在最后一次
    invoke 时 responses 为空，被当作 MODEL_CALL_FAILED）。
    """
    return [
        _intent_response("answer_question"),
        _handoff_response("learning_assistant"),
        AIMessage(content="任务已分派"),
        _search_response("一元二次方程", call_id="verify-search-1"),
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


# ── 伪造引用被识别并剔除（验收核心） ────────────────────────


def test_injected_forged_reference_is_removed() -> None:
    """注入不在本轮命中的伪造引用 → 被识别剔除，消息只保留真实命中。"""
    service = _service_with_documents()
    forged = Citation(
        document_id="forged",
        source="forged.txt",
        chunk_id="forged-chunk-1",
    )
    real = service.search("一元二次方程", top_k=5)[0].citation
    # 模型输出夹带伪造引用 + 一条真实引用（「伪造+合法」混合注入）
    answer = AIMessage(
        content="一元二次方程可以使用求根公式求解。",
        additional_kwargs={
            REFERENCES_METADATA_KEY: [
                forged.model_dump(mode="json"),
                real.model_dump(mode="json"),
            ]
        },
    )
    script = [
        _intent_response("answer_question"),
        _handoff_response("learning_assistant"),
        AIMessage(content="任务已分派"),
        _search_response("一元二次方程", call_id="verify-forged-search"),
        answer,
        AIMessage(content="最终汇总"),
    ]
    graph = _worker_search_graph(service, ScriptedModel(script))

    result = graph.run("请用知识库解释一元二次方程", "verify-forged")

    assert result["run_error"] is None
    ai = _terminal_ai_messages(result["messages"])
    # 伪造被剔除、合法引用保留（=本轮真实命中，一一对应）
    expected = [hit.citation for hit in service.search("一元二次方程", top_k=5)]
    assert message_references(ai[1]) == expected
    verification = result["reference_verification"]
    assert isinstance(verification, ReferenceVerification)
    assert verification.total == len(expected)
    assert verification.verified == len(expected)
    assert verification.removed == 1
    assert verification.removed_chunk_ids == ["forged-chunk-1"]
    assert verification.merged == 0


def test_field_mismatch_treated_as_forged() -> None:
    """chunk_id 真实但字段不匹配（篡改页码/来源）→ 按伪造剔除。"""
    service = _service_with_documents()
    real = service.search("一元二次方程", top_k=5)[0].citation
    tampered = real.model_copy(update={"page": 5, "source": "tampered.txt"})
    answer = AIMessage(
        content="一元二次方程可以使用求根公式求解。",
        additional_kwargs={
            REFERENCES_METADATA_KEY: [tampered.model_dump(mode="json")]
        },
    )
    script = [
        _intent_response("answer_question"),
        _handoff_response("learning_assistant"),
        AIMessage(content="任务已分派"),
        _search_response("一元二次方程", call_id="verify-tampered-search"),
        answer,
        AIMessage(content="最终汇总"),
    ]
    graph = _worker_search_graph(service, ScriptedModel(script))

    result = graph.run("请用知识库解释一元二次方程", "verify-tampered")

    assert result["run_error"] is None
    ai = _terminal_ai_messages(result["messages"])
    # 篡改版剔除，真实命中全量挂载。注意：词法索引下「一元二次方程」除
    # algebra（高分）外还会低分命中 calculus（共享「方」字），挂载的是
    # ground_truth 全集（2 条），因此必须与完整 expected 比对而非单条。
    expected = [hit.citation for hit in service.search("一元二次方程", top_k=5)]
    assert message_references(ai[1]) == expected
    verification = result["reference_verification"]
    assert isinstance(verification, ReferenceVerification)
    assert verification.removed == 1
    assert verification.removed_chunk_ids == [real.chunk_id]
    assert verification.total == len(expected)


def test_legitimate_references_are_not_affected() -> None:
    """正常检索作答：引用全部保留（无注入时校验不误伤合法引用）。"""
    service = _service_with_documents()
    graph = _worker_search_graph(
        service,
        ScriptedModel(_search_answer_script()),
    )

    result = graph.run("请用知识库解释一元二次方程", "verify-legit")

    assert result["run_error"] is None
    ai = _terminal_ai_messages(result["messages"])
    expected = [hit.citation for hit in service.search("一元二次方程", top_k=5)]
    assert message_references(ai[1]) == expected
    verification = result["reference_verification"]
    assert isinstance(verification, ReferenceVerification)
    assert verification.total == len(expected)
    assert verification.verified == len(expected)
    assert verification.removed == 0
    assert verification.merged == 0
    assert verification.removed_chunk_ids == []


# ── 文档级合并（格式规范化，验收核心） ──────────────────────


def test_same_document_multiple_chunks_merged() -> None:
    """同一文档多个 chunk 命中 → 合并为一条，与其他文档引用并列、编号稳定。"""
    service = KnowledgeService(InMemoryKnowledgeIndex(), chunk_size=34, overlap=0)
    chunks = service.add_documents(
        [
            # 句子 17 字符 ×6 = 102 字符，chunk_size=34（=2 句）整除 → 恰好
            # 3 个 chunk 且每个 chunk 都含「方程」（词法命中可预期）
            KnowledgeDocument(
                document_id="long-doc",
                content="一元二次方程可以使用求根公式求解。" * 6,
                source="long.txt",
            ),
            # 单 chunk 文档（不参与合并，验证「合并只作用于同文档」）
            KnowledgeDocument(
                document_id="short-doc",
                content="梯度下降沿负梯度方向迭代更新参数。",
                source="short.txt",
            ),
        ]
    )
    # 前提显式化：102 = 34×3 → long-doc 恰 3 个 chunk、short-doc 1 个 chunk，
    # 且每个 chunk 都含 query 关键词（词法命中可预期）。前提若不成立，
    # 先在此失败，避免把「环境假设错误」误报为「合并逻辑错误」。
    assert [chunk.document_id for chunk in chunks] == [
        "long-doc",
        "long-doc",
        "long-doc",
        "short-doc",
    ]
    assert all(
        "方程" in chunk.content
        for chunk in chunks
        if chunk.document_id == "long-doc"
    )
    assert "梯度" in chunks[3].content
    script = [
        _intent_response("answer_question"),
        _handoff_response("learning_assistant"),
        AIMessage(content="任务已分派"),
        _search_response("方程梯度", call_id="verify-merge-search"),
        AIMessage(content="长文档与短文档都讲解。"),
        AIMessage(content="最终汇总"),
    ]
    graph = _worker_search_graph(service, ScriptedModel(script))

    result = graph.run("讲解两个文档", "verify-merge")

    assert result["run_error"] is None
    # 检索排序（词法索引确定性的关键，注释锁定依据）：query「方程梯度」
    # 的 term 为单字{方,程,梯,度} + 双字{方程,程梯,梯度}。short-doc 同时
    # 命中单字「梯/度/方」（「方向」里的「方」也是 query 单字）与双字
    # 「梯度」→ 4 分排第一；long-doc 三个 chunk 各命中单字「方/程」+ 双字
    # 「方程」→ 3 分同分，按 chunk_id 升序排在其后。
    ground_truth = [hit.citation for hit in service.search("方程梯度", top_k=5)]
    assert len(ground_truth) == 4
    assert [hit.document_id for hit in ground_truth].count("long-doc") == 3
    assert [hit.document_id for hit in ground_truth].count("short-doc") == 1
    assert ground_truth[0].document_id == "short-doc"
    # 合并时保留「首次出现」的 chunk：short-doc 首条 + long-doc 首个命中
    short_citation = ground_truth[0]
    long_first = next(
        hit for hit in ground_truth if hit.document_id == "long-doc"
    )
    ai = _terminal_ai_messages(result["messages"])
    references = message_references(ai[1])
    assert references is not None
    # 文档级合并：long-doc 3 个 chunk → 1 条（保留首次出现的 chunk，即
    # long_first），short-doc 原样保留 → 共 2 条，编号 = 列表下标（文档
    # 首次出现顺序，short-doc 因得分最高先出现），输出稳定可解析。
    assert [ref.document_id for ref in references] == ["short-doc", "long-doc"]
    assert references[0] == short_citation
    assert references[1] == long_first
    verification = result["reference_verification"]
    assert isinstance(verification, ReferenceVerification)
    assert verification.total == 2
    # M-1 语义：verified 是 chunk 级通过校验的条数（4 个命中 chunk 全部
    # 真实），total 是文档级合并后挂载条数（2 条），merged = verified-total
    assert verification.verified == 4
    assert verification.merged == 2
    assert verification.merged_document_ids == ["long-doc"]  # 每个文档只记一次
    assert verification.removed == 0
    # 维护点（M-3）：本测试的得分与排序依赖词法索引确定性（short-doc
    # 4 分 vs long-doc 3 分），Sprint 3 换向量索引后需复核（与 S2-T4
    # test_references.py 同一维护点，见本文件 docstring）。


# ── 校验结论在评价结果中体现（验收核心） ────────────────────


def test_verification_reflected_in_evaluation_result() -> None:
    """校验结论在评价结果中体现：evaluation.reference_verification 携带本轮结论。"""
    service = _service_with_documents()
    script = [
        _intent_response("evaluation"),
        _handoff_response("evaluator"),
        AIMessage(content="任务已分派"),
        _search_response("一元二次方程", call_id="verify-eval-search"),
        _submit_response("pass", "pass", "pass", "依据检索证据，回答准确且引用完整"),
        AIMessage(content="评价完成。"),
        AIMessage(content="最终汇总"),
    ]
    graph = _worker_search_graph(service, ScriptedModel(script))

    result = graph.run("请评价这段回答", "verify-eval")

    assert result["run_error"] is None
    evaluation = result["evaluation"]
    assert isinstance(evaluation, EvaluationResult)
    assert evaluation.verdict == EvaluationVerdict.PASS
    # 评价结果内嵌本轮引用校验结论（核心层组装，模型不可填写）
    embedded = evaluation.reference_verification
    assert embedded is not None
    assert embedded.total >= 1
    assert embedded.verified == embedded.total
    assert embedded.removed == 0
    # state 通道与评价内嵌同源（同一轮同一结论）
    state_verification = result["reference_verification"]
    assert isinstance(state_verification, ReferenceVerification)
    assert state_verification.total == embedded.total
    assert state_verification.removed == embedded.removed
    assert state_verification.merged == embedded.merged


def test_worker_verification_reflected_in_evaluator_result() -> None:
    """I-1 回归锁：计划流程 worker 轮剔除伪造 → evaluator 只评价不检索时，
    评价结果内嵌 worker 轮的校验结论（removed 明细），伪造剔除真正
    「在评价结果中体现」（evaluation 组装回退读取 state 中本用户轮结论）。
    """
    service = _service_with_documents()
    forged = Citation(
        document_id="forged",
        source="forged.txt",
        chunk_id="forged-worker-1",
    )
    # teaching_assistant 轮的回答夹带伪造引用（worker 轮校验层应剔除）
    worker_answer = AIMessage(
        content="一元二次方程可以使用求根公式求解。",
        additional_kwargs={
            REFERENCES_METADATA_KEY: [forged.model_dump(mode="json")]
        },
    )
    script = [
        _plan_response(),
        AIMessage(content="计划已创建"),
        _search_response("一元二次方程", call_id="verify-i1-search"),
        worker_answer,
        _submit_response("pass", "pass", "pass", "讲解准确且引用完整"),
        AIMessage(content="评价完成。"),
        AIMessage(content="最终汇总"),
    ]
    graph = _worker_search_graph(service, ScriptedModel(script))

    result = graph.run("请先讲解一元二次方程，再检查讲解是否准确", "verify-i1")

    assert result["run_error"] is None
    # worker（teaching_assistant）轮校验结论：注入伪造被剔除，state 通道保留
    state_verification = result["reference_verification"]
    assert isinstance(state_verification, ReferenceVerification)
    assert state_verification.removed == 1
    assert state_verification.removed_chunk_ids == ["forged-worker-1"]
    # evaluator 轮只评价不检索（本轮无校验结论）→ 评价结果回退并入
    # state 中 worker 轮的结论（evaluation 组装处的兜底逻辑，见
    # graph_builder.py _wrap 注释）：这是 I-1 的核心验收场景
    evaluation = result["evaluation"]
    assert isinstance(evaluation, EvaluationResult)
    embedded = evaluation.reference_verification
    assert embedded is not None
    assert embedded.removed == 1
    assert embedded.removed_chunk_ids == ["forged-worker-1"]
    assert embedded.total == state_verification.total
    # worker 回答消息挂载真实命中（无合并：algebra/calculus 不同文档）、
    # 不携带伪造引用
    ai = _terminal_ai_messages(result["messages"])
    expected = [hit.citation for hit in service.search("一元二次方程", top_k=5)]
    assert message_references(ai[1]) == expected


# ── 零引用/无检索场景不破坏 ─────────────────────────────────


def test_no_search_and_no_hits_keep_state_clean() -> None:
    """无检索/无命中：不写校验结论（None）、不挂引用、运行正常。"""
    # 直接回答（未调用检索工具）
    graph = CollaborativeAgentGraph(
        model=ScriptedModel([AIMessage(content="直接回答")])
    )
    result = graph.run("你好", "verify-nosearch")
    assert result["run_error"] is None
    assert result["reference_verification"] is None
    ai = _terminal_ai_messages(result["messages"])
    assert message_references(ai[0]) is None

    # 检索无命中（found=False）
    empty_service = KnowledgeService(InMemoryKnowledgeIndex())
    script = [
        _intent_response("answer_question"),
        _handoff_response("learning_assistant"),
        AIMessage(content="任务已分派"),
        _search_response("不存在的知识", call_id="verify-nohits-search"),
        AIMessage(content="未找到相关内容。"),
        AIMessage(content="最终汇总"),
    ]
    graph2 = _worker_search_graph(empty_service, ScriptedModel(script))
    result2 = graph2.run("查一下不存在的知识", "verify-nohits")
    assert result2["run_error"] is None
    assert result2["reference_verification"] is None
    ai2 = _terminal_ai_messages(result2["messages"])
    assert message_references(ai2[1]) is None
    assert REFERENCES_METADATA_KEY not in ai2[1].additional_kwargs


def test_injected_references_without_search_all_removed() -> None:
    """无检索但消息被注入引用：全部判定伪造、剔除并剥离，不留残迹。"""
    forged = Citation(
        document_id="forged",
        source="forged.txt",
        chunk_id="forged-1",
    )
    graph = CollaborativeAgentGraph(
        model=ScriptedModel(
            [
                AIMessage(
                    content="直接回答但夹带伪造引用。",
                    additional_kwargs={
                        REFERENCES_METADATA_KEY: [forged.model_dump(mode="json")]
                    },
                )
            ]
        )
    )

    result = graph.run("你好", "verify-injected-nosearch")

    assert result["run_error"] is None
    ai = _terminal_ai_messages(result["messages"])
    # 伪造引用被剥离：消息不再携带 references 键（不随 checkpoint 持久化）
    assert message_references(ai[0]) is None
    assert REFERENCES_METADATA_KEY not in ai[0].additional_kwargs
    verification = result["reference_verification"]
    assert isinstance(verification, ReferenceVerification)
    assert verification.total == 0
    assert verification.verified == 0
    assert verification.removed == 1
    assert verification.removed_chunk_ids == ["forged-1"]


# ── 生命周期：持久化与每轮重置 ──────────────────────────────


def test_verification_persists_in_checkpoint_and_resets_per_turn() -> None:
    """校验结论随 checkpoint 持久化（get_state 可读），新轮重置为 None。"""
    service = _service_with_documents()
    model = ScriptedModel(
        [
            *_search_answer_script(),
            AIMessage(content="第二轮的普通回答"),
        ]
    )
    graph = _worker_search_graph(service, model, checkpointer=InMemorySaver())
    session_id = "verify-lifecycle"
    user_id = "user-1"

    first = graph.run("请用知识库解释一元二次方程", session_id, user_id)

    assert first["reference_verification"] is not None
    persisted = graph.get_state(session_id, user_id)
    assert persisted is not None
    # checkpoint 反序列化后可能是 dict 或 ReferenceVerification 实例
    # （视序列化器而定），统一归一为模型再断言，两种形式都兼容
    persisted_raw = persisted["reference_verification"]
    if isinstance(persisted_raw, ReferenceVerification):
        persisted_raw = persisted_raw.model_dump()
    persisted_verification = ReferenceVerification.model_validate(persisted_raw)
    assert persisted_verification.removed == 0
    assert persisted_verification.total >= 1

    second = graph.run("你好", session_id, user_id)

    # 新轮开始 reference_verification 被重置为 None（与 evaluation 同构：
    # 校验结论是本轮事实记录，旧轮结论只属于旧轮）
    assert second["reference_verification"] is None
    assert second["messages"][-1].content == "第二轮的普通回答"


# ── 向后兼容：旧数据无校验标记时行为合理 ────────────────────


def test_legacy_evaluation_without_verification_field_validates() -> None:
    """旧 checkpoint 的 EvaluationResult 无 reference_verification 字段 → 默认 None。"""
    legacy = EvaluationResult.model_validate(
        {
            "verdict": "pass",
            "fact_accuracy": "pass",
            "citation_completeness": "pass",
            "reason": "旧数据",
            "evidence_tool_names": ["search_knowledge"],
        }
    )
    assert legacy.reference_verification is None
    # 模型实例构造（不传新字段）同样默认 None
    fresh = EvaluationResult(
        verdict=EvaluationVerdict.PASS,
        fact_accuracy=EvaluationVerdict.PASS,
        citation_completeness=EvaluationVerdict.PASS,
    )
    assert fresh.reference_verification is None


def test_reference_verification_serialization_round_trip() -> None:
    """ReferenceVerification 序列化往返不失真（checkpoint 持久化前提）。"""
    verification = ReferenceVerification(
        total=2,
        verified=2,
        removed=1,
        merged=1,
        removed_chunk_ids=["forged-1"],
        merged_document_ids=["long-doc"],
    )
    restored = ReferenceVerification.model_validate(
        verification.model_dump(mode="json")
    )
    assert restored.total == 2
    assert restored.verified == 2
    assert restored.removed == 1
    assert restored.merged == 1
    assert restored.removed_chunk_ids == ["forged-1"]
    assert restored.merged_document_ids == ["long-doc"]
    # 脱敏契约：结论模型不携带任何正文/内容字段
    dumped = verification.model_dump()
    assert "content" not in dumped
