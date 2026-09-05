"""典型案例预跑脚本（赛前）：确定性替身模型 + 真实链路组件。

用法（在 backend/ 目录下，使用项目 venv）：
    $env:PYTHONPATH="src"; .venv/Scripts/python.exe scripts/prerun_cases.py
    $env:PYTHONPATH="src"; .venv/Scripts/python.exe scripts/prerun_cases.py --json-only

设计说明（面向初学者）：
1. 为什么用 ScriptedModel：真实模型配额有限且输出不确定，预跑的目标
   是验证「系统机制」——工作流调度、落盘闸、批改落库、诊断聚合、引用
   校验链、多轮上下文——这些全部是确定性代码路径，用替身模型可精确
   断言；真实模型冒烟另行安排（见 docs/competition/typical-case-protocol.md）。
2. 六个用例覆盖六大场景：教案工作流 / PPT 工作流 / 作业批改 / 学情诊断
   与学习路径 / 知识库问答与引用真实性 / 多轮上下文保持。
3. 每个用例独立隔离（临时目录 + 独立 store + 独立图），单例失败不影响
   其余用例；结果写 docs/competition/pre-run-results.md 供正式典型案例
   报告复用。
4. 真实语料抽查（用例 5c）：若 data/knowledge.db 已入库新教材，直接对其
   执行英文检索抽查——该子项失败不阻塞其余断言（只读探测）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import traceback
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
if str(BACKEND / "src") not in sys.path:
    sys.path.insert(0, str(BACKEND / "src"))
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from langchain_core.messages import AIMessage
from langchain_core.tools import tool as langchain_tool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel, ConfigDict

from core.events import EventType
from core.graph_builder import (
    _ACTIVE_PARENT_STATE,
    CollaborativeAgentGraph,
    _verify_references,
)
from core.knowledge.index import InMemoryKnowledgeIndex, SqliteKnowledgeIndex
from core.knowledge.models import Citation, KnowledgeDocument
from core.knowledge.service import KnowledgeService
from core.knowledge.tools import create_search_knowledge_tool
from core.learning import LearningRecordStore
from core.state import (
    AgentRole,
    WorkflowState,
    WorkflowStatus,
    WorkflowStepStatus,
    message_references,
)
from tests.test_graph_builder import ScriptedModel

# ── 结果结构 ───────────────────────────────────────────────────


@dataclass
class CaseResult:
    """单个预跑用例的结构化结果。"""

    case_id: str
    scenario: str
    user_input: str
    criteria: list[str]
    status: str = "PENDING"  # PASS / FAIL / ERROR
    evidence: list[str] = field(default_factory=list)
    attribution: str | None = None  # 失败归因（检索未命中/步骤失败/渲染缺失等）

    def ok(self, evidence: str) -> None:
        self.evidence.append(f"[OK] {evidence}")

    def bad(self, evidence: str, attribution: str) -> None:
        self.evidence.append(f"[FAIL] {evidence}")
        self.status = "FAIL"
        if self.attribution is None:
            self.attribution = attribution


def _text(content: str) -> AIMessage:
    return AIMessage(content=content)


def _tool_call(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


# ── 用例 1：教案工作流（智能备课 · 教案生成）──────────────────


def case_lesson_plan_workflow(tmp: Path) -> CaseResult:
    case = CaseResult(
        case_id="C1",
        scenario="智能备课 · 教案工作流",
        user_input="帮我准备《反向传播》的教案，对象是本科二年级",
        criteria=[
            "工作流四步（collect/draft/generate/review）全部 COMPLETED",
            "步骤产出按 step_id 暂存（step_outputs 含 collect 与 draft）",
            "事件流含 WORKFLOW_STARTED/STEP×4/WORKFLOW_COMPLETED",
            "Supervisor 收口说明进入共享历史",
        ],
    )
    model = ScriptedModel(
        [
            _tool_call(
                "start_workflow",
                {"workflow_id": "lesson_plan", "topic": "反向传播"},
                "start-c1",
            ),
            _text("正在启动教案工作流。"),
            _text("素材稿：反向传播核心概念、链式法则推导……"),
            _text("教案全文：一、教学目标……六、评价设计"),
            _text("已导出教案文档。"),
            _text('{"verdict": "pass", "summary": "结构完整"}'),
            _text("教案已生成，请下载查看。"),
        ]
    )
    graph = CollaborativeAgentGraph(
        model=model, orchestration_mode="tool", enable_workflows=True
    )
    state = graph.run(
        case.user_input, session_id="c1-lesson", workspace_root=str(tmp)
    )
    workflow = WorkflowState.model_validate(state["workflow"])
    if workflow.status is WorkflowStatus.COMPLETED:
        case.ok("workflow.status == COMPLETED")
    else:
        case.bad(f"workflow.status == {workflow.status}", "工作流未收口")
    statuses = [step.status for step in workflow.steps]
    if statuses == [WorkflowStepStatus.COMPLETED] * 4:
        case.ok("四步全部 COMPLETED")
    else:
        case.bad(f"步骤状态 {statuses}", "工作流步骤失败或未完成")
    if {"collect", "draft"} <= workflow.step_outputs.keys():
        case.ok(f"step_outputs 暂存：{sorted(workflow.step_outputs)}")
    else:
        case.bad(f"step_outputs 缺键：{sorted(workflow.step_outputs)}", "步骤产出未暂存")
    events = [event.event_type for event in state["events"]]
    if (
        EventType.WORKFLOW_STARTED in events
        and events.count(EventType.WORKFLOW_STEP_COMPLETED) == 4
        and EventType.WORKFLOW_COMPLETED in events
    ):
        case.ok("事件链完整（STARTED + STEP×4 + COMPLETED）")
    else:
        case.bad("工作流事件缺失", "事件通知链路异常")
    joined = "\n".join(str(message.content) for message in state["messages"])
    if "教案已生成" in joined and "素材稿" in joined:
        case.ok("跨步骤上下文累积且收口回答在历史中")
    else:
        case.bad("历史中缺少步骤产物/收口回答", "消息累积或收口路由异常")
    if case.status != "FAIL":
        case.status = "PASS"
    return case


# ── 用例 2：PPT 课件工作流（智能备课 · 课件生成）──────────────


def _fake_export_pptx():
    """确定性替身导出工具：把占位 pptx 写入产物区（真实路径经
    approved 上下文与落盘闸双重校验，与生产同构）。"""

    class _NoArgs(BaseModel):
        model_config = ConfigDict(extra="forbid")

    def export_workflow_pptx() -> str:
        """预跑替身导出工具：把占位 pptx 写入产物区。"""
        parent = _ACTIVE_PARENT_STATE.get()
        raw = None if parent is None else parent.get("workflow")
        if raw is None:
            return json.dumps({"ok": False, "error": "no workflow"}, ensure_ascii=False)
        workflow = (
            raw if isinstance(raw, WorkflowState) else WorkflowState.model_validate(raw)
        )
        root = Path(workflow.artifact_root or "")
        topic = workflow.params.get("topic", "课件")
        target = root / f"课件-{topic}.pptx"
        target.write_bytes(b"PK\x03\x04 pre-run placeholder pptx")
        return json.dumps(
            {"ok": True, "pptx": str(target), "slides": 12, "template": "edu-theme"},
            ensure_ascii=False,
        )

    return langchain_tool("export_workflow_pptx", args_schema=_NoArgs)(
        export_workflow_pptx
    )


def _outline_json() -> str:
    slides = [{"layout": "cover", "title": "反向传播", "points": []}]
    for index in range(2, 12):
        slides.append(
            {
                "layout": "content",
                "title": f"要点 {index - 1}",
                "points": [f"第 {index - 1} 页核心要点"],
                "notes": "口述要点",
            }
        )
    slides.append({"layout": "closing", "title": "小结", "points": ["回顾与练习"]})
    return json.dumps(
        {"deck_title": "反向传播", "audience": "本科二年级", "slides": slides},
        ensure_ascii=False,
    )


def case_ppt_workflow(tmp: Path) -> CaseResult:
    case = CaseResult(
        case_id="C2",
        scenario="智能备课 · PPT 课件工作流",
        user_input="为《反向传播》做一份教学 PPT",
        criteria=[
            "大纲 JSON 通过结构门禁（≥10 页、标题非空）并暂存",
            "generate 步落盘闸：产物区出现非空 课件-反向传播.pptx",
            "四步全部 COMPLETED，workflow.artifacts 登记产物",
            "review 判 pass，工作流收口",
        ],
    )
    model = ScriptedModel(
        [
            _tool_call(
                "start_workflow",
                {"workflow_id": "ppt_slides", "topic": "反向传播"},
                "start-c2",
            ),
            _text("正在启动课件工作流。"),
            _text("素材稿：章节骨架与核心要点……"),
            _text(_outline_json()),
            _tool_call("export_workflow_pptx", {}, "export-c2"),
            _text("课件已生成：12 页，主题 edu-theme。"),
            _text('{"verdict": "pass", "summary": "页数与结构达标"}'),
            _text("课件已完成，请下载查看。"),
        ]
    )
    graph = CollaborativeAgentGraph(
        model=model,
        orchestration_mode="tool",
        enable_workflows=True,
        tools=[_fake_export_pptx()],
        tool_permissions={"export_workflow_pptx": {AgentRole.TEACHING_ASSISTANT}},
    )
    state = graph.run(
        case.user_input, session_id="c2-ppt", workspace_root=str(tmp)
    )
    workflow = WorkflowState.model_validate(state["workflow"])
    if workflow.status is WorkflowStatus.COMPLETED:
        case.ok("workflow.status == COMPLETED")
    else:
        case.bad(
            f"workflow.status == {workflow.status}，error_code={workflow.error_code}",
            "PPT 工作流未收口",
        )
    outline_staged = workflow.step_outputs.get("outline", "")
    if "反向传播" in outline_staged and '"slides"' in outline_staged:
        case.ok("大纲 JSON 已过结构门禁并暂存")
    else:
        case.bad("outline 未暂存或结构异常", "大纲门禁/暂存异常")
    artifact = tmp / ".workflow-artifacts"
    pptx_files = list(artifact.glob("*/课件-反向传播.pptx")) if artifact.exists() else []
    if pptx_files and pptx_files[0].stat().st_size > 0:
        case.ok(f"落盘闸通过：{pptx_files[0].name}（{pptx_files[0].stat().st_size}B）")
    else:
        case.bad("产物区未见非空 课件-反向传播.pptx", "落盘闸/导出链路异常")
    if any(name.endswith("课件-反向传播.pptx") for name in workflow.artifacts):
        case.ok(f"产物登记：{workflow.artifacts}")
    else:
        case.bad(f"artifacts 未登记：{workflow.artifacts}", "产物登记链路异常")
    if case.status != "FAIL":
        case.status = "PASS"
    return case


# ── 用例 3：作业批改（逐题评分 + 确定性落库）──────────────────


def case_grading(tmp: Path) -> tuple[CaseResult, LearningRecordStore]:
    store = LearningRecordStore(tmp / "learning.db")
    case = CaseResult(
        case_id="C3",
        scenario="作业批改 · 逐题评分与学情落库",
        user_input="请批改我的作业（3 题，含知识点与错因标注）",
        criteria=[
            "grading 通道返回逐题结论，总分由核心侧确定性汇总",
            "逐题记录落库 learning_records（复合幂等键，3 题不丢）",
            "错因标签（概念不清）与知识点随记录落库",
        ],
    )
    grading_items = [
        {
            "question_id": "q1",
            "score": 10,
            "max_score": 10,
            "feedback": "解答完整。",
            "knowledge_point": "梯度下降",
        },
        {
            "question_id": "q2",
            "score": 0,
            "max_score": 10,
            "feedback": "概念错误，建议复习。",
            "knowledge_point": "梯度下降",
            "error_tag": "概念不清",
        },
        {
            "question_id": "q3",
            "score": 5,
            "max_score": 10,
            "feedback": "部分正确。",
        },
    ]
    model = ScriptedModel(
        [
            _tool_call("handoff", {"target": "evaluator"}, "handoff-c3"),
            _text("转交评价助手批改。"),
            _tool_call(
                "submit_grading",
                {"items": grading_items, "overall_comment": "整体掌握一般。"},
                "grading-c3",
            ),
            _text("批改完成，请查看评分。"),
            _text("已为你批改本次作业。"),
        ]
    )
    graph = CollaborativeAgentGraph(
        model=model, checkpointer=InMemorySaver(), learning_store=store
    )
    result = graph.run(case.user_input, "c3-grading", user_id="student-a")
    grading = result["grading"]
    if grading is not None and grading.total_score == 15 and grading.max_total_score == 30:
        case.ok(f"grading 通道：{len(grading.items)} 题，总分 15/30（核心侧汇总）")
    else:
        case.bad(f"grading={grading}", "批改通道写入异常")
    summary = store.summarize("student-a")
    if summary["total_attempts"] == 3:
        case.ok("learning_records 落库 3 条（复合幂等键下多题不丢）")
    else:
        case.bad(f"total_attempts == {summary['total_attempts']}", "批改落库链路异常")
    points = {p["knowledge_point"]: p for p in summary["knowledge_points"]}
    gradient = points.get("梯度下降")
    if gradient is not None and gradient["attempts"] == 2 and gradient["accuracy"] == 0.5:
        case.ok("知识点聚合正确：梯度下降 2 次作答，加权正确率 0.5")
    else:
        case.bad(f"梯度下降聚合 {gradient}", "知识点聚合异常")
    tags = store.insights("student-a")["error_tag_counts"]
    if tags.get("概念不清") == 1:
        case.ok("错因标签落库：概念不清 ×1")
    else:
        case.bad(f"error_tag_counts == {tags}", "错因标签落库异常")
    if case.status != "FAIL":
        case.status = "PASS"
    return case, store


# ── 用例 4：学情诊断与学习路径推荐 ────────────────────────────


async def _get_json(app, path: str, user_id: str) -> dict:
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(path, headers={"X-User-Id": user_id})
        return {"status": response.status_code, "body": response.json()}


def case_diagnosis_and_path(
    tmp: Path, store: LearningRecordStore
) -> CaseResult:
    case = CaseResult(
        case_id="C4",
        scenario="学情诊断与学习路径推荐",
        user_input="诊断我的学习情况；并根据薄弱点规划学习路径",
        criteria=[
            "诊断端点输出薄弱知识点（确定性规则：作答≥2 且正确率<0.6）",
            "学习路径规划落库 path_plan 记录（模型经工具显式存档）",
            "洞察端点回显路径记录与错题归因（新功能端到端）",
        ],
    )
    from api.app import create_app

    app = create_app()
    app.state.learning_store = store
    # 4a 诊断端点：C3 数据中「梯度下降」2 次作答正确率 0.5 → 预警
    diagnosis = asyncio.run(_get_json(app, "/learning/diagnosis/summary", "student-a"))
    if diagnosis["status"] == 200 and "梯度下降" in diagnosis["body"].get("weak_points", []):
        case.ok(f"诊断端点：薄弱点 {diagnosis['body']['weak_points']}")
    else:
        case.bad(f"诊断端点返回 {diagnosis}", "诊断聚合/预警规则异常")
    # 4b 学习路径规划（handoff：intent → learning_assistant → 读记录 → 存档 → 收口）
    model = ScriptedModel(
        [
            _tool_call(
                "detect_intent",
                {"intent": "learning_path", "reason": ""},
                "intent-c4",
            ),
            _tool_call("handoff", {"target": "learning_assistant"}, "handoff-c4"),
            _text("任务已分派。"),
            _tool_call("get_learning_records", {}, "records-c4"),
            _tool_call(
                "record_learning_outcome",
                {
                    "knowledge_point": "梯度下降",
                    "outcome": "partial",
                    "kind": "path_plan",
                },
                "plan-c4",
            ),
            _text(
                "学习路径：第一阶段巩固梯度下降（资源：教材第 5 章；检验点：完成 3 题练习）……"
            ),
            _text("学习路径已生成。"),
        ]
    )
    graph = CollaborativeAgentGraph(
        model=model, checkpointer=InMemorySaver(), learning_store=store
    )
    graph.run("根据我的薄弱点规划学习路径", "c4-path", user_id="student-a")
    plans = [
        row
        for row in store.insights("student-a")["recent_path_plans"]
        if row["knowledge_point"] == "梯度下降"
    ]
    if plans:
        case.ok("path_plan 存档成功（record_learning_outcome → learning_records）")
    else:
        case.bad("learning_records 无 path_plan 记录", "学习路径存档链路异常")
    insights = asyncio.run(_get_json(app, "/learning/insights/summary", "student-a"))
    body = insights["body"]
    if (
        insights["status"] == 200
        and any(plan["knowledge_point"] == "梯度下降" for plan in body["recent_path_plans"])
        and body["error_tag_counts"].get("概念不清") == 1
        and len(body["daily_accuracy"]) >= 1
    ):
        case.ok(
            "洞察端点端到端：路径回显 + 错因分布 + 正确率趋势"
            f"（{len(body['daily_accuracy'])} 天）"
        )
    else:
        case.bad(f"洞察端点返回 {insights}", "洞察端点聚合异常")
    if case.status != "FAIL":
        case.status = "PASS"
    return case


# ── 用例 5：知识库问答与引用真实性校验 ────────────────────────


def case_knowledge_qa_and_references(tmp: Path) -> CaseResult:
    case = CaseResult(
        case_id="C5",
        scenario="知识问答 · 检索作答与引用真实性校验",
        user_input="请用知识库解释链式法则在反向传播中的作用",
        criteria=[
            "检索作答的最终回答携带结构化引用，与真实命中一一对应",
            "引用真实性校验：伪造引用被剔除、真实引用通过（纯函数探针）",
            "真实语料抽查：新入库英文教材可被检索命中（探测项）",
        ],
    )
    service = KnowledgeService(InMemoryKnowledgeIndex(), chunk_size=200, overlap=0)
    service.add_documents(
        [
            KnowledgeDocument(
                document_id="bp-notes",
                source="反向传播讲义",
                page=1,
                content=(
                    "反向传播利用链式法则逐层计算梯度：损失对参数的导数"
                    "等于损失对激活的导数乘以激活对参数的导数。"
                ),
            )
        ]
    )
    search_tool = create_search_knowledge_tool(service)
    model = ScriptedModel(
        [
            _tool_call(
                "detect_intent", {"intent": "answer_question", "reason": ""}, "intent-c5"
            ),
            _tool_call("handoff", {"target": "learning_assistant"}, "handoff-c5"),
            _text("任务已分派。"),
            _tool_call(
                "search_knowledge",
                {"query": "链式法则 反向传播 梯度", "top_k": 5},
                "search-c5",
            ),
            _text("反向传播通过链式法则逐层求导，把误差信号传回各层参数。"),
            _text("以上是关于链式法则与反向传播的解答。"),
        ]
    )
    graph = CollaborativeAgentGraph(
        model=model,
        checkpointer=InMemorySaver(),
        tools=[search_tool],
        tool_permissions={
            "search_knowledge": {AgentRole.LEARNING_ASSISTANT},
        },
    )
    result = graph.run(case.user_input, "c5-qa")
    expected = [hit.citation for hit in service.search("链式法则 反向传播 梯度", top_k=5)]
    terminal = [
        message
        for message in result["messages"]
        if isinstance(message, AIMessage) and not message.tool_calls
    ]
    answer = terminal[1] if len(terminal) >= 2 else None
    references = message_references(answer) if answer is not None else None
    if references is not None and references == expected and len(expected) >= 1:
        case.ok(f"检索作答携带 {len(references)} 条引用，与真实命中一致")
    else:
        case.bad(f"references={references}", "引用插入链路异常")
    verification = result.get("reference_verification")
    if verification is not None and verification.verified >= 1 and verification.removed == 0:
        case.ok(
            f"引用校验结论：verified={verification.verified} removed={verification.removed}"
        )
    else:
        case.bad(f"reference_verification={verification}", "引用校验通道异常")
    # 伪造引用探针：声称引用了本轮未命中的片段 → 必须剔除
    ghost = Citation(
        document_id="bp-notes", source="反向传播讲义", page=99, chunk_id="ghost-chunk"
    )
    verified, removed = _verify_references([ghost], expected)
    if not verified and len(removed) == 1:
        case.ok("伪造引用探针：越界引用被确定性剔除")
    else:
        case.bad(f"verified={verified} removed={removed}", "引用校验纯函数异常")
    # 真实语料抽查（探测项，失败不阻塞；入库并发期可能锁库，宽容降级）
    real_db = REPO_ROOT / "data" / "knowledge.db"
    if real_db.exists():
        try:
            index = SqliteKnowledgeIndex(real_db)
        except Exception as exc:  # noqa: BLE001 - 探测项：锁库/损坏只记录不阻塞
            case.evidence.append(f"[INFO] 真实语料抽查跳过（{type(exc).__name__}）")
        else:
            try:
                hits = index.search("SARSA on-policy control algorithm", 5)
                sources = [hit.citation.source for hit in hits]
                if "rl-sutton" in sources:
                    case.ok(f"真实语料抽查命中新教材：{sources[:3]}")
                else:
                    case.evidence.append(
                        f"[INFO] 真实语料抽查未命中 rl-sutton（可能尚未入库完成）：{sources[:3]}"
                    )
            finally:
                index.close()
    else:
        case.evidence.append("[INFO] data/knowledge.db 不存在，跳过真实语料抽查")
    if case.status != "FAIL":
        case.status = "PASS"
    return case


# ── 用例 6：多轮对话上下文保持 ────────────────────────────────


def case_multi_turn_context() -> CaseResult:
    case = CaseResult(
        case_id="C6",
        scenario="多轮对话 · 上下文保持",
        user_input="（第 2 轮）我刚才说我叫什么名字？",
        criteria=[
            "第二轮模型输入包含第一轮用户消息（checkpoint 历史注入）",
            "get_history 恢复两轮完整对话",
        ],
    )
    model = ScriptedModel(
        [
            _text("你好，小明！很高兴陪你备考。"),
            _text("你刚才说你叫小明，目标是考研上岸。"),
        ]
    )
    graph = CollaborativeAgentGraph(
        model=model,
        checkpointer=InMemorySaver(),
        orchestration_mode="tool",
        enable_workflows=True,
    )
    graph.run(
        "我叫小明，正在准备考研，帮我规划一下",
        session_id="c6-context",
        user_id="student-a",
    )
    second = graph.run(
        "我刚才说我叫什么名字？",
        session_id="c6-context",
        user_id="student-a",
    )
    last_call_text = "\n".join(
        str(message.content) for message in model.calls[-1]
    )
    if "小明" in last_call_text and "考研" in last_call_text:
        case.ok("第二轮模型输入包含第一轮完整上下文（小明/考研均在）")
    else:
        case.bad("第二轮模型输入缺少第一轮内容", "上下文注入/裁剪异常")
    history = graph.get_history("c6-context", "student-a")
    user_turns = [
        message for message in history if getattr(message, "type", "") == "human"
    ]
    if len(user_turns) >= 2 and second["run_error"] is None:
        case.ok(f"get_history 恢复 {len(user_turns)} 轮用户输入")
    else:
        case.bad(f"user_turns={len(user_turns)}", "历史恢复异常")
    if case.status != "FAIL":
        case.status = "PASS"
    return case


# ── 报告输出 ──────────────────────────────────────────────────


def render_markdown(results: list[CaseResult]) -> str:
    lines = [
        "# 典型案例预跑结果（ScriptedModel 确定性验证）",
        "",
        "> 生成方式：`backend/scripts/prerun_cases.py`（确定性替身模型 + 真实链路组件）。",
        "> 用途：正式《典型案例测试报告》的机制层证据；真实模型冒烟另行记录。",
        "",
        "## 汇总",
        "",
        "| 用例 | 场景 | 结果 | 失败归因 |",
        "| --- | --- | --- | --- |",
    ]
    for result in results:
        lines.append(
            f"| {result.case_id} | {result.scenario} | {result.status} | "
            f"{result.attribution or '—'} |"
        )
    lines.append("")
    for result in results:
        lines += [
            f"## {result.case_id} {result.scenario}",
            "",
            f"- 用户输入：{result.user_input}",
            "- 验收判据：",
        ]
        lines += [f"  - {criterion}" for criterion in result.criteria]
        lines.append(f"- 结果：**{result.status}**")
        lines.append("- 证据：")
        lines += [f"  - {evidence}" for evidence in result.evidence]
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="典型案例预跑（确定性替身模型）")
    parser.add_argument(
        "--md-out",
        type=Path,
        default=REPO_ROOT / "docs" / "competition" / "pre-run-results.md",
        help="Markdown 报告输出路径",
    )
    parser.add_argument("--json-only", action="store_true", help="只打印 JSON 结果")
    args = parser.parse_args(argv)

    results: list[CaseResult] = []
    runners: list[tuple[str, object]] = [
        ("C1", case_lesson_plan_workflow),
        ("C2", case_ppt_workflow),
        ("C3", case_grading),
        ("C4", case_diagnosis_and_path),
        ("C5", case_knowledge_qa_and_references),
        ("C6", case_multi_turn_context),
    ]
    store: LearningRecordStore | None = None
    with tempfile.TemporaryDirectory(prefix="prerun-cases-") as raw_tmp:
        tmp = Path(raw_tmp)
        for case_id, runner in runners:
            try:
                if case_id == "C3":
                    result, store = runner(tmp)  # type: ignore[operator]
                elif case_id == "C4":
                    assert store is not None
                    result = runner(tmp, store)  # type: ignore[operator]
                elif case_id == "C6":
                    result = runner()  # type: ignore[operator]
                else:
                    result = runner(tmp)  # type: ignore[operator]
            except Exception:  # noqa: BLE001 - 单例失败不阻塞其余用例
                result = CaseResult(
                    case_id=case_id,
                    scenario=f"{case_id}（执行异常）",
                    user_input="—",
                    criteria=[],
                )
                result.status = "ERROR"
                result.attribution = "脚本执行异常（详见 traceback）"
                result.evidence.append(f"[ERROR] {traceback.format_exc(limit=5)}")
            results.append(result)
        if store is not None:
            store.close()

    if args.json_only:
        print(
            json.dumps(
                [
                    {
                        "case_id": result.case_id,
                        "scenario": result.scenario,
                        "status": result.status,
                        "attribution": result.attribution,
                        "evidence": result.evidence,
                    }
                    for result in results
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if all(result.status == "PASS" for result in results) else 1

    args.md_out.parent.mkdir(parents=True, exist_ok=True)
    args.md_out.write_text(render_markdown(results), encoding="utf-8")
    for result in results:
        print(f"[{result.status}] {result.case_id} {result.scenario}")
        for evidence in result.evidence:
            print(f"    {evidence}")
    passed = sum(1 for result in results if result.status == "PASS")
    print(f"\n{passed}/{len(results)} 用例通过；报告已写入 {args.md_out}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
