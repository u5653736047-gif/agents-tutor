"""S4-T3 检索事件接线测试：search_knowledge 工具可选接入 + 事件转换。

覆盖 T3 验收清单：
1. 工具层：未注入 adaptive 配置时输出与现状逐项一致（零回归、无
   metadata 键）；注入后输出附带检索元数据（rounds / threshold_met
   / stopped_reason / hit_count / top_score / needed / need_reason），
   且元数据不记查询正文；
2. 阈值未达标：Observation 文本含「知识库检索未达相关性阈值，知识库
   可能未覆盖该问题」提示，事件 threshold_met=False；
3. 多轮 refine：轮数写入元数据与事件，达到上限停止；
4. 必要性判定：needed=False 的 need_reason 出现在元数据与事件；
5. 事件层（图）：core 侧 _wrap 把工具结果元数据转成
   RETRIEVAL_DECISION 事件（agent=调用角色、tool_name=search_knowledge、
   retrieval_* 字段、无查询正文）；未注入 adaptive 的图不发新事件。

零耦合方向：knowledge 包不 import core/events.py——工具输出纯 JSON
元数据，转换全部发生在 core 侧（graph_builder），本文件断言两端各自
的行为（工具层看 JSON，事件层看图 events）。
"""
from __future__ import annotations

import json
from collections.abc import Sequence

import pytest
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.tools import BaseTool, tool

from core.events import EventType, RunEvent
from core.graph_builder import CollaborativeAgentGraph
from core.knowledge.index import InMemoryKnowledgeIndex
from core.knowledge.models import KnowledgeChunk
from core.knowledge.policy import HeuristicRetrievalPolicy
from core.knowledge.retrieval import HeuristicQueryRefiner
from core.knowledge.service import KnowledgeService
from core.knowledge.tools import create_search_knowledge_tool
from core.state import AgentRole
from core.tools import ToolExecutor

# 与工具输出联动的固定文案（断言「阈值提示」与「未命中提示」）。
_THRESHOLD_HINT = "知识库检索未达相关性阈值，知识库可能未覆盖该问题"


# ── 小工具与测试替身 ──────────────────────────────────────────────


def _chunk(
    chunk_id: str,
    content: str,
    *,
    document_id: str = "doc-1",
    source: str | None = None,
) -> KnowledgeChunk:
    """构造一个可直接入库的 chunk（source 用逻辑标识符，非文件路径）。"""
    return KnowledgeChunk(
        chunk_id=chunk_id,
        document_id=document_id,
        content=content,
        source=source or f"{document_id}.txt",
        page=None,
        start=0,
        end=len(content),
        metadata={},
    )


class _Refiner:
    """精化器测试替身：按映射表精化（与 test_knowledge_adaptive 同型）。

    满足 retrieval.QueryRefiner 协议（鸭子类型）：refine(query,
    top_score) -> str。映射表没有对应 key 时原样返回 query（模拟
    「精化无效」——重检同一 query 结果不变，直到上限停止）。
    """

    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = mapping
        self.calls: list[tuple[str, float]] = []

    def refine(self, query: str, top_score: float) -> str:
        self.calls.append((query, top_score))
        return self._mapping.get(query, query)


def _gamma_service(*, max_refine_rounds: int = 2) -> KnowledgeService:
    """只含 "gamma" chunk 的服务：query "alpha" 0 命中，refine 后可命中。"""
    index = InMemoryKnowledgeIndex()
    index.upsert([_chunk("c-gamma", "gamma", document_id="ml-a")])
    return KnowledgeService(index, max_refine_rounds=max_refine_rounds)


def _svm_service() -> KnowledgeService:
    """含 "support vector machine" chunk 的服务：词法 3 词命中 → 3.0。"""
    index = InMemoryKnowledgeIndex()
    index.upsert([_chunk("c1", "support vector machine", document_id="ml-a")])
    return KnowledgeService(index)


class ScriptedModel:
    """按图执行顺序返回预设模型消息。"""

    def __init__(self, responses: Sequence[AIMessage]) -> None:
        self.responses = list(responses)
        self.calls: list[list[BaseMessage]] = []
        self.bound_tool_names: list[str] = []

    def bind_tools(self, tools: Sequence[object]) -> ScriptedModel:
        self.bound_tool_names = [str(getattr(tool, "name", "")) for tool in tools]
        return self

    def invoke(self, messages: list[BaseMessage]) -> AIMessage:
        self.calls.append(list(messages))
        return self.responses.pop(0)


def _handoff_response(target: str = "learning_assistant") -> AIMessage:
    """supervisor 直接分派 worker（不调 detect_intent 的兼容路径）。"""
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "handoff",
                "args": {"target": target},
                "id": "handoff-1",
                "type": "tool_call",
            }
        ],
    )


def _search_response(query: str, call_id: str = "search-1") -> AIMessage:
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


def _worker_search_script(query: str) -> list[AIMessage]:
    """「supervisor 分派 → worker 检索并作答 → supervisor 汇总」脚本。

    响应数量与图执行 invoke 次数严格一一对应（共 5 次）：
    - supervisor 轮：handoff → 无工具回答「任务已分派」；
    - learning_assistant 轮：search_knowledge → 无工具回答；
    - supervisor 返回轮：最终汇总（无工具）。
    """
    return [
        _handoff_response("learning_assistant"),
        AIMessage(content="任务已分派"),
        _search_response(query),
        AIMessage(content="检索作答"),
        AIMessage(content="最终汇总"),
    ]


def _worker_search_graph(
    service: KnowledgeService,
    model: ScriptedModel,
    *,
    policy: object | None = None,
    relevance_threshold: float | None = None,
    refiner: object | None = None,
) -> CollaborativeAgentGraph:
    """构造「worker 可检索、supervisor 不可检索」的图（既有权限约定）。

    自适应装配参数原样传给 create_search_knowledge_tool——与 api 层
    装配方（T2 已把工具装进图）同一接入方式。
    """
    search_tool = create_search_knowledge_tool(
        service,
        policy=policy,  # type: ignore[arg-type]
        relevance_threshold=relevance_threshold,
        refiner=refiner,  # type: ignore[arg-type]
    )
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
    )


def _worker_search_graph_with_tool(
    search_tool: BaseTool,
    model: ScriptedModel,
) -> CollaborativeAgentGraph:
    """用外部工具构造「worker 可检索」的图（容错测试用替身工具）。

    与 _worker_search_graph 的权限约定一致，但工具由调用方提供——
    容错测试需要让 search_knowledge 输出「正常工具不会产生的脏
    metadata」（负数、损坏 JSON、非法类型），验证 _wrap 解析层
    「脏数据不击穿运行」的承诺。
    """
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
    )


def _dirty_search_tool(output: object) -> BaseTool:
    """返回固定输出的 search_knowledge 替身工具（容错测试专用）。

    output 为 dict 时经 ToolExecutor 序列化为 JSON Observation；
    为 str 时原样作为输出（可模拟损坏的 JSON）。工具名必须是
    "search_knowledge"——_wrap 的解析按工具名过滤（_CITATION_
    TOOL_NAMES），替身名字一致才能走进解析路径。
    """

    @tool("search_knowledge")
    def dirty_search(query: str, top_k: int = 5) -> object:
        """返回固定输出的检索替身（容错测试专用，必须有 docstring）。

        langchain 的 @tool 在未显式传 description 时要求函数带
        docstring，否则 StructuredTool.from_function 直接抛
        ValueError——缺 docstring 会导致测试在构造工具时就失败。
        query/top_k 参数只用于满足工具 schema（与真实
        search_knowledge 同形），输出与输入无关。
        """
        return output

    return dirty_search


def _retrieval_events(result: dict[str, object]) -> list[RunEvent]:
    """取出图中发出的全部检索决策事件（按出现顺序）。"""
    return [
        event
        for event in result["events"]  # type: ignore[union-attr]
        if event.event_type is EventType.RETRIEVAL_DECISION
    ]


# ── 1. 工具层：默认零回归 ─────────────────────────────────────────


def test_tool_default_path_matches_legacy_output() -> None:
    """未注入 adaptive 配置：输出与接入前逐项一致（无 metadata / hint）。

    这是零回归的落点：工具走 service.search() 原路径，输出格式不变
    ——旧消费者（引用收集、评价证据）无感，graph_builder 解析不到
    metadata 也不会发新事件。
    """
    service = _svm_service()
    search_tool = create_search_knowledge_tool(service)

    result = search_tool.invoke({"query": "support vector machine", "top_k": 5})

    # 与 service.search 逐项一致（分数、顺序、citation）。
    expected = service.search("support vector machine", top_k=5)
    assert result["found"] is True
    assert [hit["score"] for hit in result["hits"]] == [
        hit.score for hit in expected
    ]
    assert result["hits"][0]["content"] == "support vector machine"
    assert result["hits"][0]["citation"] == expected[0].citation.model_dump(
        mode="json"
    )
    # 无 metadata / hint 键：自适应未启用，事件转换侧不会发事件。
    assert "metadata" not in result
    assert "hint" not in result

    empty = create_search_knowledge_tool(
        KnowledgeService(InMemoryKnowledgeIndex())
    ).invoke({"query": "不存在的知识"})
    assert empty == {
        "found": False,
        "message": "未找到可引用的知识片段",
        "hits": [],
    }


# ── 2. 工具层：自适应元数据输出 ───────────────────────────────────


def test_tool_adaptive_output_carries_metadata_without_query_text() -> None:
    """注入阈值后输出附带元数据，且元数据不记查询正文（脱敏）。

    构造：query "support vector machine" 命中 3 词 → 3.0 ≥ 阈值 2.0
    达标。断言 metadata 七个字段齐全、无 "query" / "refine_history"
    键——查询正文只在工具调用参数与工具结果审计里，元数据只记摘要。
    """
    search_tool = create_search_knowledge_tool(
        _svm_service(), relevance_threshold=2.0
    )

    result = search_tool.invoke({"query": "support vector machine", "top_k": 5})

    assert result["found"] is True
    metadata = result["metadata"]
    assert metadata == {
        "needed": True,
        "need_reason": "默认策略：总是检索",
        "threshold_met": True,
        "stopped_reason": "达到相关性阈值",
        "rounds": 1,
        "hit_count": 1,
        "top_score": 3.0,
    }
    # 脱敏断言：metadata 不含查询正文（无 query / refine_history 键）。
    assert "query" not in metadata
    assert "refine_history" not in metadata
    assert "hint" not in result  # 达标 → 无阈值提示


def test_tool_threshold_miss_adds_hint_to_observation() -> None:
    """未达标：Observation 文本明确提示「知识库可能未覆盖」。

    hits 照常返回（分数/顺序不变），但 metadata.threshold_met=False
    且 hint 直接写进工具输出——模型可见的 Observation 里就有这句
    提示，Agent 应如实说明而非强行作答（S4-T3 验收口径）。
    """
    search_tool = create_search_knowledge_tool(
        _svm_service(), relevance_threshold=5.0
    )

    result = search_tool.invoke({"query": "support vector machine", "top_k": 5})

    assert result["found"] is True  # 结果照常返回
    assert result["metadata"]["threshold_met"] is False
    assert result["metadata"]["stopped_reason"] == "未配置重检器，未达标即停止"
    assert result["hint"] == _THRESHOLD_HINT

    # 经 ToolExecutor 的 Observation（ToolMessage content）也含提示文本
    # ——这是模型实际看到的文本。
    execution = ToolExecutor([search_tool]).execute(
        {
            "name": "search_knowledge",
            "args": {"query": "support vector machine"},
            "id": "search-hint",
        },
        AgentRole.TEACHING_ASSISTANT,
    )
    observation = str(execution.message.content)
    assert _THRESHOLD_HINT in observation
    assert json.loads(observation)["metadata"]["threshold_met"] is False


def test_tool_policy_no_retrieval_reports_reason() -> None:
    """必要性判定 needed=False：need_reason 出现在元数据，无检索。

    注入 HeuristicRetrievalPolicy：寒暄「你好」判定为不需要检索 →
    hits 为空、rounds=0、hit_count=0、top_score=0.0、threshold_met
    =None（未启用阈值判定）、need_reason 说明命中哪条规则——Agent
    直接作答，元数据说明原因（可解释）。
    """
    search_tool = create_search_knowledge_tool(
        _svm_service(), policy=HeuristicRetrievalPolicy()
    )

    result = search_tool.invoke({"query": "你好", "top_k": 5})

    assert result["found"] is False
    assert result["message"] == "未找到可引用的知识片段"
    metadata = result["metadata"]
    assert metadata["needed"] is False
    assert "问候" in metadata["need_reason"]
    assert metadata["threshold_met"] is None
    assert metadata["rounds"] == 0
    assert metadata["hit_count"] == 0
    assert metadata["top_score"] == 0.0
    assert "无需检索" in metadata["stopped_reason"]
    # threshold_met 不是 False → 不触发「知识库未覆盖」提示。
    assert "hint" not in result


def test_tool_refine_loop_reports_rounds() -> None:
    """多轮 refine：轮数 / 命中数 / 最高分写入元数据。

    构造（可手算）：query "alpha" 0 命中 → 0.0 < 阈值 0.5 → 精化器
    改成 "gamma" → 重检命中 1 词 → 1.0 ≥ 0.5 达标。metadata 只记
    轮数与最终轮统计，不记每轮 query（脱敏）。
    """
    refiner = _Refiner({"alpha": "gamma"})
    search_tool = create_search_knowledge_tool(
        _gamma_service(), refiner=refiner, relevance_threshold=0.5
    )

    result = search_tool.invoke({"query": "alpha", "top_k": 5})

    assert result["found"] is True
    assert result["hits"][0]["content"] == "gamma"
    metadata = result["metadata"]
    assert metadata["needed"] is True
    assert metadata["threshold_met"] is True
    assert metadata["stopped_reason"] == "达到相关性阈值"
    assert metadata["rounds"] == 2  # 首轮 + 1 次重检
    assert metadata["hit_count"] == 1
    assert metadata["top_score"] == 1.0
    assert refiner.calls == [("alpha", 0.0)]


def test_tool_refine_limit_reports_max_rounds() -> None:
    """重检达到上限仍未达标：轮数 = 上限 + 1，停止原因写「上限」。

    max_refine_rounds=2（service 构造时配置，工具不暴露该参数）：
    精化器永远返回同一 query（0 命中）→ 首轮 + 2 次重检共 3 轮后
    停止；threshold_met=False → Observation 带「知识库未覆盖」提示。
    """
    refiner = _Refiner({})
    search_tool = create_search_knowledge_tool(
        _gamma_service(max_refine_rounds=2), refiner=refiner, relevance_threshold=1.0
    )

    result = search_tool.invoke({"query": "alpha", "top_k": 5})

    metadata = result["metadata"]
    assert metadata["threshold_met"] is False
    assert "上限" in metadata["stopped_reason"]
    assert metadata["rounds"] == 3
    assert metadata["hit_count"] == 0
    assert metadata["top_score"] == 0.0
    assert result["hint"] == _THRESHOLD_HINT
    assert refiner.calls == [("alpha", 0.0), ("alpha", 0.0)]


# ── 3. 事件层：core 侧 _wrap 转换 ─────────────────────────────────


def test_graph_emits_retrieval_decision_on_threshold_met() -> None:
    """达标检索：_wrap 发 RETRIEVAL_DECISION，字段与元数据一致。

    断言 agent=调用角色（learning_assistant）、tool_name=
    search_knowledge、retrieval_* 字段齐全，且事件载荷不含查询正文
    （need_reason 是固定策略文案，stopped_reason 是固定停止原因——
    查询词 "alpha" 不应出现在事件序列化里）。
    """
    service = _gamma_service()
    graph = _worker_search_graph(
        service,
        ScriptedModel(_worker_search_script("alpha")),
        refiner=_Refiner({"alpha": "gamma"}),
        relevance_threshold=0.5,
    )
    result = graph.run("请用知识库回答 alpha", "graph-events")

    assert result["run_error"] is None
    decisions = _retrieval_events(result)
    assert len(decisions) == 1
    event = decisions[0]
    assert event.agent == "learning_assistant"
    assert event.tool_name == "search_knowledge"
    assert event.retrieval_needed is True
    assert event.retrieval_need_reason == "默认策略：总是检索"
    assert event.retrieval_threshold_met is True
    assert event.retrieval_stopped_reason == "达到相关性阈值"
    assert event.retrieval_rounds == 2  # 首轮 + 1 次重检
    assert event.retrieval_hit_count == 1
    assert event.retrieval_top_score == 1.0
    # 脱敏：事件载荷不记查询正文（"alpha" 是查询词，不应出现在事件里）。
    serialized = json.dumps(event.model_dump(), ensure_ascii=False)
    assert "alpha" not in serialized
    # 事件序列与既有事件自洽（递增、从 0 开始）。
    assert [e.sequence for e in result["events"]] == list(
        range(len(result["events"]))
    )


def test_graph_no_retrieval_event_without_adaptive() -> None:
    """未注入 adaptive：图照常检索，但不发检索决策事件（零回归）。

    工具输出无 metadata → _wrap 解析为空 → events 通道里没有
    RETRIEVAL_DECISION；其余行为（检索成功、引用挂载）不受影响。
    注意：tool_results 是「整轮所有工具结果」——supervisor 轮的
    handoff 与 worker 轮的 search_knowledge 各计一条，共 2 条；
    这里按工具名过滤出 search_knowledge 恰好 1 条且成功，即证明
    「检索确实执行了」（事件层断言才是不发新事件的落点）。
    """
    service = _gamma_service()
    graph = _worker_search_graph(
        service,
        ScriptedModel(_worker_search_script("gamma")),
    )
    result = graph.run("请用知识库回答 gamma", "graph-plain")

    assert result["run_error"] is None
    search_results = [
        item
        for item in result["tool_results"]
        if item.tool_name == "search_knowledge"
    ]
    assert len(search_results) == 1
    assert search_results[0].success is True
    assert _retrieval_events(result) == []
    assert all(
        event.event_type is not EventType.RETRIEVAL_DECISION
        for event in result["events"]
    )


def test_graph_threshold_miss_emits_event_and_observation_hint() -> None:
    """未达标：事件 threshold_met=False，Observation 文本含阈值提示。

    提示文本直接出现在 ToolMessage（模型观察到的工具结果）里——
    Agent 据此如实说明「知识库可能未覆盖」而非强行作答。
    """
    service = _gamma_service()
    graph = _worker_search_graph(
        service,
        ScriptedModel(_worker_search_script("gamma")),
        relevance_threshold=5.0,
    )
    result = graph.run("请用知识库回答 gamma", "graph-miss")

    decisions = _retrieval_events(result)
    assert len(decisions) == 1
    event = decisions[0]
    assert event.retrieval_threshold_met is False
    assert event.retrieval_stopped_reason == "未配置重检器，未达标即停止"
    assert event.retrieval_hit_count == 1  # 命中 1 词但 1.0 < 5.0
    assert event.retrieval_top_score == 1.0
    # Observation 提示文本：模型可见的工具结果消息里含阈值提示。
    observation_texts = [
        str(message.content)
        for message in result["messages"]
        if isinstance(message, ToolMessage)
    ]
    assert observation_texts
    assert any(_THRESHOLD_HINT in text for text in observation_texts)


def test_graph_refine_limit_rounds_event() -> None:
    """上限停止：事件 rounds = 首轮 + 上限次重检（上限生效）。"""
    service = _gamma_service(max_refine_rounds=2)
    graph = _worker_search_graph(
        service,
        ScriptedModel(_worker_search_script("alpha")),
        refiner=_Refiner({}),
        relevance_threshold=1.0,
    )
    result = graph.run("请用知识库回答 alpha", "graph-limit")

    decisions = _retrieval_events(result)
    assert len(decisions) == 1
    assert decisions[0].retrieval_rounds == 3
    assert decisions[0].retrieval_threshold_met is False
    assert "上限" in (decisions[0].retrieval_stopped_reason or "")


def test_graph_policy_no_need_event() -> None:
    """必要性判定 needed=False：事件携带 need_reason（可解释）。

    HeuristicRetrievalPolicy 判定「你好」为寒暄 → 不触发检索：
    事件 retrieval_needed=False、need_reason 含命中规则、rounds=0、
    hit_count=0、top_score=0.0、threshold_met=None——评价 Agent 可
    据此核对「未检索是有意判定而非故障」。
    """
    service = _gamma_service()
    graph = _worker_search_graph(
        service,
        ScriptedModel(_worker_search_script("你好")),
        policy=HeuristicRetrievalPolicy(),
    )
    result = graph.run("你好", "graph-no-need")

    decisions = _retrieval_events(result)
    assert len(decisions) == 1
    event = decisions[0]
    assert event.retrieval_needed is False
    assert "问候" in (event.retrieval_need_reason or "")
    assert event.retrieval_threshold_met is None
    assert event.retrieval_rounds == 0
    assert event.retrieval_hit_count == 0
    assert event.retrieval_top_score == 0.0
    assert "无需检索" in (event.retrieval_stopped_reason or "")


# ── 4. 解析容错：脏 metadata 不击穿运行（I-1 修复 + 既有承诺）─────


def test_graph_negative_metadata_values_are_clamped() -> None:
    """脏 metadata（负数）→ 值域兜底为 0，事件仍发出、不击穿运行。

    I-1 修复：rounds / hit_count / top_score 为负时，若原样透传，
    emit 构造 RunEvent 会因 ge=0 校验抛 ValidationError 击穿 _wrap
    （该 emit 不在 try 内）。解析层先做值域兜底（按 0），与「脏
    数据不击穿」承诺一致；决策字段（needed / threshold_met /
    stopped_reason）不受影响，照常进入事件。
    """
    tool = _dirty_search_tool(
        {
            "found": True,
            "hits": [],
            "metadata": {
                "needed": True,
                "need_reason": "默认策略：总是检索",
                "threshold_met": False,
                "stopped_reason": "达到相关性阈值",
                "rounds": -3,
                "hit_count": -1,
                "top_score": -2.5,
            },
        }
    )
    graph = _worker_search_graph_with_tool(
        tool,
        ScriptedModel(_worker_search_script("gamma")),
    )
    result = graph.run("请用知识库回答 gamma", "graph-clamp")

    assert result["run_error"] is None  # 不击穿
    decisions = _retrieval_events(result)
    assert len(decisions) == 1
    event = decisions[0]
    # 负数按 0 兜底（RunEvent 的 ge=0 校验不会再抛错）。
    assert event.retrieval_rounds == 0
    assert event.retrieval_hit_count == 0
    assert event.retrieval_top_score == 0.0
    # 决策字段不受影响。
    assert event.retrieval_needed is True
    assert event.retrieval_threshold_met is False


@pytest.mark.parametrize(
    "dirty_output",
    [
        "not-json",  # JSON 损坏（json.loads 失败）
        {"found": True, "hits": [], "metadata": "oops"},  # metadata 非 dict
        {
            "found": True,
            "hits": [],
            "metadata": {"needed": "yes", "rounds": 1},
        },  # 核心字段类型非法（needed 非 bool）
    ],
)
def test_graph_malformed_metadata_skips_event(dirty_output: object) -> None:
    """损坏 / 类型非法的 metadata → 跳过该结果，不发事件、不击穿。

    与「写入端严格、读取端宽容」哲学一致：解析失败视为脏数据跳过
    （与 _intent_from_results 同型），图正常走完，不产生
    RETRIEVAL_DECISION 事件。
    """
    graph = _worker_search_graph_with_tool(
        _dirty_search_tool(dirty_output),
        ScriptedModel(_worker_search_script("gamma")),
    )
    result = graph.run("请用知识库回答 gamma", "graph-dirty")

    assert result["run_error"] is None  # 不击穿
    assert _retrieval_events(result) == []


# ── P0-3 过滤参数在 adaptive 分支的透传（六大功能计划，pi 审查
# 🔴2 硬性验收：注入 policy/threshold 后生产走 adaptive 路径，
# 若只透传非 adaptive 分支，难度/课标过滤会在生产静默失效；
# 只测非 adaptive 路径的用例发现不了该缺口）─────────────


def _difficulty_metadata_service() -> KnowledgeService:
    index = InMemoryKnowledgeIndex()
    index.upsert(
        [
            KnowledgeChunk(
                chunk_id="c-basic",
                document_id="doc-ml",
                content="支持向量机的基础概念与直观解释",
                source="ml.txt",
                page=None,
                start=0,
                end=14,
                metadata={"difficulty": "basic"},
            ),
            KnowledgeChunk(
                chunk_id="c-adv",
                document_id="doc-ml",
                content="支持向量机的核方法与对偶问题推导",
                source="ml.txt",
                page=None,
                start=0,
                end=15,
                metadata={"difficulty": "advanced"},
            ),
        ]
    )
    return KnowledgeService(index)


def test_adaptive_branch_still_applies_metadata_filter() -> None:
    """注入 policy/threshold（启用 adaptive）后过滤仍生效。"""
    search_tool = create_search_knowledge_tool(
        _difficulty_metadata_service(),
        policy=HeuristicRetrievalPolicy(),
        relevance_threshold=0.5,
    )

    result = search_tool.invoke(
        {"query": "支持向量机", "difficulty": "basic"}
    )

    # 确认确实走了 adaptive 路径（metadata 键存在），再断言过滤生效
    assert "metadata" in result
    assert result["metadata"]["needed"] is True
    assert [hit["citation"]["chunk_id"] for hit in result["hits"]] == ["c-basic"]


def test_adaptive_branch_without_filter_returns_all() -> None:
    search_tool = create_search_knowledge_tool(
        _difficulty_metadata_service(),
        policy=HeuristicRetrievalPolicy(),
        relevance_threshold=0.5,
    )

    result = search_tool.invoke({"query": "支持向量机", "top_k": 10})

    assert "metadata" in result
    assert {hit["citation"]["chunk_id"] for hit in result["hits"]} == {
        "c-basic",
        "c-adv",
    }


# ── 审查 W5：HeuristicQueryRefiner（P0-2 生产装配的零 LLM 精化器）
# 规则单测 + 与 adaptive 工具的集成（此前零测试守护）──────────


def test_heuristic_query_refiner_denoises_punctuation() -> None:
    """规则 1：含标点/符号的查询 → 去噪后的净化查询。"""
    refiner = HeuristicQueryRefiner()

    refined = refiner.refine("什么是、梯度下降？？？", 0.0)

    assert refined == "什么是 梯度下降"


def test_heuristic_query_refiner_truncates_long_tail() -> None:
    """规则 2：无标点但超长（>32 字符）→ 去尾部 8 字符保留主体。"""
    refiner = HeuristicQueryRefiner()
    long_query = "请帮我详细解释一下反向传播算法在深度神经网络训练过程中到底起了什么作用"
    assert len(long_query) > refiner._LONG_QUERY_CHARS

    refined = refiner.refine(long_query, 0.0)

    assert refined == long_query[: -refiner._TAIL_DROP_CHARS]


def test_heuristic_query_refiner_raises_when_no_opportunity() -> None:
    """规则 3：短且无标点 → 抛 ValueError（由 _safe_refine 兑底为
    「停止重检」，避免用原 query 白耗一轮检索）。"""
    refiner = HeuristicQueryRefiner()

    with pytest.raises(ValueError, match="no refinement opportunity"):
        refiner.refine("梯度下降", 0.0)


def test_heuristic_refiner_integrates_with_adaptive_tool() -> None:
    """集成：未达标触发精化——零命中的短查询无精化空间，
    HeuristicQueryRefiner 抛 ValueError 被 _safe_refine 兑底停止，
    元数据 rounds=1 且 stopped_reason 不含「精化」。"""
    search_tool = create_search_knowledge_tool(
        _gamma_service(),
        policy=HeuristicRetrievalPolicy(),
        relevance_threshold=0.5,
        refiner=HeuristicQueryRefiner(),
    )

    result = search_tool.invoke({"query": "alpha"})

    assert result["metadata"]["threshold_met"] is False
    assert result["metadata"]["rounds"] == 1
    assert result["found"] is False
