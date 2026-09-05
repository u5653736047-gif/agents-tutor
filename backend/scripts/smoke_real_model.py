"""真实模型端到端冒烟（赛前）：.env DeepSeek 配置 × 完整图执行链路。

用法（在 backend/ 目录下，使用项目 venv）：
    $env:PYTHONPATH="src"; .venv/Scripts/python.exe scripts/smoke_real_model.py
    $env:PYTHONPATH="src"; .venv/Scripts/python.exe scripts/smoke_real_model.py --cases C1,C5

设计说明（面向初学者）：
1. 与预跑脚本（prerun_cases.py，ScriptedModel 确定性替身）互补：本脚本
   用 .env 的真实 DeepSeek 配置（DeepSeekSettings.from_env +
   create_deepseek_model，与 verify_deepseek_react.py 同一模式）驱动
   CollaborativeAgentGraph.run()——与用户经前端对话完全相同的链路：
   意图识别 → 角色调度 → 工具调用 → 最终回答，全程不绕过图。
2. 验收判据聚焦系统机制而非模型措辞（真实模型输出有不确定性）：
   工作流是否收口、产物是否落盘、落库是否成功、引用是否经校验等。
3. 每用例最多 2 次尝试（失败重试 1 次）；仍失败如实记录并归因。
4. 配额控制：6 个用例各跑一遍（工作流用例步骤多属正常开销），
   --cases 支持选择性执行。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
if str(BACKEND / "src") not in sys.path:
    sys.path.insert(0, str(BACKEND / "src"))

# fastembed（向量路/重排器）在本环境经镜像下载/缓存加载；先设环境变量
# 再导入任何可能触发 huggingface_hub 的模块。
import os

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from core.filesystem import WorkspaceFileSystem
from core.knowledge.hybrid import (
    HybridKnowledgeIndex,
    open_vector_index_if_available,
)
from core.knowledge.index import SqliteKnowledgeIndex
from core.knowledge.service import KnowledgeService
from core.knowledge.tools import create_search_knowledge_tool
from core.learning import LearningRecordStore
from core.models import DeepSeekSettings, create_deepseek_model
from core.state import AgentRole, WorkflowStatus, message_references
from core.tools.office_tools import (
    OfficeCliSettings,
    create_office_tools,
    resolve_officecli_binary,
)
from core.workflows.ppt_slides import parse_deck_outline

USER_ID = "smoke-student"


# ── 结果结构 ───────────────────────────────────────────────────


@dataclass
class SmokeCase:
    """单个真实模型冒烟用例的结果。"""

    case_id: str
    scenario: str
    user_input: str
    criteria: list[str]
    status: str = "PENDING"  # PASS / FAIL / ERROR
    evidence: list[str] = field(default_factory=list)
    output_summary: str = ""
    attribution: str | None = None
    attempts_used: int = 0
    duration_s: float = 0.0

    def ok(self, evidence: str) -> None:
        self.evidence.append(f"[OK] {evidence}")

    def bad(self, evidence: str, attribution: str) -> None:
        self.evidence.append(f"[FAIL] {evidence}")
        self.status = "FAIL"
        if self.attribution is None:
            self.attribution = attribution


# ── 装配：与生产（api/app.py）同一「可用才开」接线 ─────────────


def _build_knowledge_tool():
    """真实知识库检索工具：词法库必开，向量库可用才合流（生产同一语义）。"""
    lexical = SqliteKnowledgeIndex(REPO_ROOT / "data" / "knowledge.db")
    vector = open_vector_index_if_available(REPO_ROOT / "data" / "vector_knowledge.db")
    service = KnowledgeService(HybridKnowledgeIndex(lexical, vector))
    return create_search_knowledge_tool(service)


def _build_office_tools(workspace: Path):
    """officecli 工具对（inspect/edit）；二进制缺失返回空（降级不阻塞）。"""
    try:
        binary = resolve_officecli_binary()
    except RuntimeError as exc:
        print(f"[WARN] officecli 不可用，工作流导出类判据将降级：{exc}")
        return []
    filesystem = WorkspaceFileSystem(workspace)
    settings = OfficeCliSettings(binary=binary)
    return list(create_office_tools(filesystem, settings))


def _build_graph(workspace: Path, store: LearningRecordStore):
    """按用例构造一张全新图：真实模型 + 真实知识库 + office 工具 + 学情库。"""
    settings = DeepSeekSettings.from_env()
    model = create_deepseek_model(settings, timeout=180, max_retries=1)
    tools = [_build_knowledge_tool(), *_build_office_tools(workspace)]
    permissions = {
        "search_knowledge": {
            AgentRole.TEACHING_ASSISTANT,
            AgentRole.LEARNING_ASSISTANT,
            AgentRole.EVALUATOR,
        },
        "officecli_inspect": {
            AgentRole.SUPERVISOR,
            AgentRole.TEACHING_ASSISTANT,
            AgentRole.LEARNING_ASSISTANT,
            AgentRole.EVALUATOR,
        },
        "officecli_edit": {
            AgentRole.SUPERVISOR,
            AgentRole.TEACHING_ASSISTANT,
            AgentRole.EVALUATOR,
        },
    }
    from core.graph_builder import CollaborativeAgentGraph

    return CollaborativeAgentGraph(
        model=model,
        checkpointer=InMemorySaver(),
        orchestration_mode="tool",
        enable_workflows=True,
        tools=tools,
        tool_permissions=permissions,
        learning_store=store,
    )


def _final_text(state: dict) -> str:
    """取最后一条无工具调用的助手消息正文（截断摘要用）。"""
    for message in reversed(state.get("messages", [])):
        if (
            isinstance(message, AIMessage)
            and not message.tool_calls
            and isinstance(message.content, str)
            and message.content.strip()
        ):
            return message.content
    return ""


def _workflow_summary(state: dict) -> str:
    workflow = state.get("workflow")
    if workflow is None:
        return "workflow=None（Supervisor 未触发工作流）"
    steps = "、".join(
        f"{step.step_id}:{step.status.value}" for step in workflow.steps
    )
    return f"status={workflow.status.value} steps=[{steps}]"


def _artifact_files(workspace: Path, suffix: str) -> list[Path]:
    zone = workspace / ".workflow-artifacts"
    if not zone.exists():
        return []
    return [p for p in zone.glob(f"*/{suffix}") if p.stat().st_size > 0]


# ── 用例判据（机制导向）────────────────────────────────────────


def check_lesson_plan(state: dict, workspace: Path, case: SmokeCase) -> None:
    workflow = state.get("workflow")
    if workflow is None:
        case.bad(_workflow_summary(state), "Supervisor 未触发 lesson_plan 工作流")
        return
    if workflow.status is WorkflowStatus.COMPLETED:
        case.ok("workflow.status == COMPLETED")
    else:
        case.bad(
            f"workflow.status == {workflow.status.value}，error={workflow.error_code}",
            "工作流未收口（步骤失败或预算耗尽）",
        )
    if {"collect", "draft"} <= workflow.step_outputs.keys():
        case.ok(f"步骤暂存：{sorted(workflow.step_outputs)}")
    else:
        case.bad(f"step_outputs={sorted(workflow.step_outputs)}", "步骤产出未暂存")
    docx = _artifact_files(workspace, "教案-*.docx")
    if docx:
        case.ok(f"确定性导出产物：{docx[0].name}（{docx[0].stat().st_size}B）")
    else:
        case.bad("产物区未见非空 教案-*.docx", "确定性导出链路异常")


def check_ppt(state: dict, workspace: Path, case: SmokeCase) -> None:
    workflow = state.get("workflow")
    if workflow is None:
        case.bad(_workflow_summary(state), "Supervisor 未触发 ppt_slides 工作流")
        return
    if workflow.status is WorkflowStatus.COMPLETED:
        case.ok("workflow.status == COMPLETED")
    else:
        case.bad(
            f"workflow.status == {workflow.status.value}，error={workflow.error_code}",
            "PPT 工作流未收口",
        )
    outline = parse_deck_outline(workflow.step_outputs.get("outline"))
    if outline is not None and len(outline["slides"]) >= 10:
        case.ok(f"大纲暂存且过结构门禁：{len(outline['slides'])} 页")
    else:
        case.bad("outline 未暂存或未过结构门禁", "大纲门禁/暂存异常")
    pptx = _artifact_files(workspace, "课件-*.pptx")
    if pptx:
        case.ok(f"确定性导出产物：{pptx[0].name}（{pptx[0].stat().st_size}B）")
    else:
        case.bad("产物区未见非空 课件-*.pptx", "确定性导出/落盘闸异常")


def check_grading(state: dict, store: LearningRecordStore, case: SmokeCase) -> None:
    grading = state.get("grading")
    if grading is not None and len(grading.items) >= 2:
        case.ok(
            f"grading 通道：{len(grading.items)} 题，总分 "
            f"{grading.total_score:g}/{grading.max_total_score:g}"
        )
    else:
        case.bad(f"grading={grading}", "批改通道未写入（模型未调用 submit_grading？）")
    summary = store.summarize(USER_ID)
    if summary["total_attempts"] >= 2:
        case.ok(f"learning_records 落库 {summary['total_attempts']} 条")
    else:
        case.bad(f"total_attempts={summary['total_attempts']}", "批改落库链路异常")
    points = [p for p in summary["knowledge_points"] if p["knowledge_point"]]
    if points:
        case.ok(f"知识点维度落库：{[p['knowledge_point'] for p in points][:5]}")
    else:
        case.bad("无知识点维度记录", "知识点落库缺失（软性：模型未标注知识点）")


async def _get_diagnosis(store: LearningRecordStore) -> dict:
    from httpx import ASGITransport, AsyncClient

    from api.app import create_app

    app = create_app()
    app.state.learning_store = store
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(
            "/learning/diagnosis/summary", headers={"X-User-Id": USER_ID}
        )
        return {"status": response.status_code, "body": response.json()}


def check_diagnosis_and_path(
    state: dict, store: LearningRecordStore, case: SmokeCase
) -> None:
    if state.get("run_error") is not None:
        case.bad(f"run_error={state['run_error']}", "图执行异常")
    answer = _final_text(state)
    if answer.strip():
        case.ok(f"诊断/路径回答已产出（{len(answer)} 字）")
    else:
        # 非系统缺陷归因：模型以工具调用收尾未出汇总文本（机制判据不受影响）
        case.evidence.append("[INFO] 无终局文本（模型以工具调用收尾，机制判据照常）")
    diagnosis = asyncio.run(_get_diagnosis(store))
    body = diagnosis["body"]
    if diagnosis["status"] == 200 and body.get("total_attempts", 0) > 0:
        weak = body.get("weak_points", [])
        case.ok(
            f"诊断端点结构正常：total_attempts={body['total_attempts']}"
            f"，weak_points={weak}"
        )
    else:
        case.bad(f"诊断端点返回 {diagnosis}", "诊断聚合端点异常")
    plans = store.insights(USER_ID)["recent_path_plans"]
    if plans:
        case.ok(
            f"path_plan 存档 {len(plans)} 条："
            f"{[p['knowledge_point'] for p in plans][:5]}"
        )
    else:
        case.bad("learning_records 无 path_plan 记录", "模型未调用路径存档工具")


def check_knowledge_qa(state: dict, case: SmokeCase) -> None:
    tool_names = [
        result.tool_name
        for result in state.get("tool_results", [])
        if result.success and result.tool_name == "search_knowledge"
    ]
    if tool_names:
        case.ok(f"search_knowledge 成功调用 ×{len(tool_names)}")
    else:
        case.evidence.append(
            f"[INFO] 终态 tool_results 中未见 search_knowledge"
            f"（共 {len(state.get('tool_results', []))} 条），改用引用校验链判定"
        )
    referenced = [
        message
        for message in state.get("messages", [])
        if isinstance(message, AIMessage) and message_references(message)
    ]
    if referenced:
        total = sum(len(message_references(message) or []) for message in referenced)
        case.ok(f"{len(referenced)} 条回答携带结构化引用（共 {total} 条）")
    else:
        case.bad("无回答携带引用", "引用插入链路异常或检索零命中")
    verification = state.get("reference_verification")
    if verification is not None:
        if verification.verified >= 1 and verification.removed == 0:
            # verified≥1 即证明本轮存在真实检索命中（校验链的 ground truth
            # 来自检索工具结果）——检索触发与否以此为准，不依赖终态通道快照。
            case.ok(
                f"检索命中经校验链确认：verified={verification.verified} "
                f"removed={verification.removed} total={verification.total}"
            )
        else:
            case.bad(
                f"reference_verification: verified={verification.verified} "
                f"removed={verification.removed}",
                "引用校验剔除/零验证",
            )
    else:
        case.bad("reference_verification 为空", "引用校验通道未产出")


def check_multi_turn(state: dict, case: SmokeCase) -> None:
    answer = _final_text(state)
    if "林晓" in answer:
        case.ok("第二轮正确复述姓名「林晓」")
    else:
        case.bad(f"回答未含姓名：{answer[:80]}", "上下文保持失败（姓名丢失）")
    if "90" in answer:
        case.ok("第二轮正确复述目标「90 分」")
    else:
        case.bad(f"回答未含目标：{answer[:80]}", "上下文保持失败（目标丢失）")
    human_turns = [
        message
        for message in state.get("messages", [])
        if getattr(message, "type", "") == "human"
    ]
    if len(human_turns) >= 2:
        case.ok(f"历史包含 {len(human_turns)} 轮用户输入")
    else:
        case.bad(f"human 轮数={len(human_turns)}", "会话历史异常")


# ── 用例执行 ──────────────────────────────────────────────────


def _run_once(case: SmokeCase, workspace: Path, store: LearningRecordStore) -> None:
    graph = _build_graph(workspace, store)
    session = f"smoke-{case.case_id.lower()}"
    if case.case_id == "C1":
        state = graph.run(case.user_input, session, user_id=USER_ID, workspace_root=str(workspace))
        check_lesson_plan(state, workspace, case)
    elif case.case_id == "C2":
        state = graph.run(case.user_input, session, user_id=USER_ID, workspace_root=str(workspace))
        check_ppt(state, workspace, case)
    elif case.case_id == "C3":
        state = graph.run(case.user_input, session, user_id=USER_ID)
        check_grading(state, store, case)
    elif case.case_id == "C4":
        state = graph.run(case.user_input, session, user_id=USER_ID)
        check_diagnosis_and_path(state, store, case)
    elif case.case_id == "C5":
        state = graph.run(case.user_input, session, user_id=USER_ID)
        check_knowledge_qa(state, case)
    elif case.case_id == "C6":
        graph.run(
            "我叫林晓，是人工智能专业大三学生，我的目标是机器学习期末考到 90 分以上",
            session,
            user_id=USER_ID,
        )
        state = graph.run("你还记得我叫什么名字、我的目标是多少吗？请复述。", session, user_id=USER_ID)
        check_multi_turn(state, case)
        return
    else:
        raise ValueError(f"未知用例 {case.case_id}")
    case.output_summary = _final_text(state)[:300]


def run_case(case: SmokeCase, workspace: Path, store: LearningRecordStore) -> None:
    """执行用例（失败重试 1 次），记录尝试次数与耗时。"""
    start = time.monotonic()
    for attempt in range(1, 3):
        case.attempts_used = attempt
        case.evidence = []
        case.status = "PENDING"
        case.attribution = None
        try:
            _run_once(case, workspace, store)
        except Exception:  # noqa: BLE001 - 记录异常并归因
            case.status = "ERROR"
            case.attribution = f"执行异常：{traceback.format_exc(limit=3).strip()[-300:]}"
        if case.status == "PENDING":
            case.status = "PASS"
        if case.status == "PASS":
            break
        print(
            f"    [retry] {case.case_id} 第 {attempt} 次未通过"
            f"（{case.attribution or '判据未满足'}），重试…"
        )
    case.duration_s = time.monotonic() - start


def build_cases() -> list[SmokeCase]:
    return [
        SmokeCase(
            case_id="C1",
            scenario="智能备课 · 教案工作流（真实模型触发与执行）",
            user_input="帮我准备《反向传播》的教案，授课对象是本科二年级学生",
            criteria=[
                "Supervisor 触发 lesson_plan 工作流且四步收口",
                "步骤产出按 step_id 暂存",
                "确定性导出产物 教案-*.docx 存在且非空",
            ],
        ),
        SmokeCase(
            case_id="C2",
            scenario="智能备课 · PPT 课件工作流（真实模型触发与执行）",
            user_input="为《反向传播》制作一份教学 PPT，大约 12 页",
            criteria=[
                "Supervisor 触发 ppt_slides 工作流且四步收口",
                "大纲 JSON 暂存且过结构门禁（≥10 页）",
                "确定性导出产物 课件-*.pptx 存在且非空",
            ],
        ),
        SmokeCase(
            case_id="C3",
            scenario="作业批改 · 逐题评分与学情落库（真实模型）",
            user_input=(
                "请批改下面这份作业，逐题给出得分、满分与反馈，并标注知识点"
                "和错因：\n第1题（知识点：梯度下降，满分10分）：题目：写出梯度"
                "下降的参数更新公式。学生作答：θ ← θ − α∇J(θ)。\n第2题（知识"
                "点：反向传播，满分10分）：题目：反向传播利用什么法则逐层求导？"
                "学生作答：利用乘法法则。\n第3题（知识点：激活函数，满分10分）："
                "题目：ReLU 的表达式是什么？学生作答：f(x)=max(0,x)。"
            ),
            criteria=[
                "grading 通道写入 ≥2 题结论",
                "逐题记录落库 learning_records",
                "知识点维度随记录落库",
            ],
        ),
        SmokeCase(
            case_id="C4",
            scenario="学情诊断与学习路径推荐（真实模型）",
            user_input=(
                "请先根据我的作答记录诊断我的学习情况并指出薄弱点；然后针对"
                "薄弱点为我规划一条学习路径，并把路径存档记录下来。"
            ),
            criteria=[
                "诊断端点结构正常且读取到作答数据",
                "学习路径存档（path_plan）落库",
            ],
        ),
        SmokeCase(
            case_id="C5",
            scenario="知识问答 · 检索作答与引用真实性校验（真实模型）",
            user_input="请基于知识库解释梯度下降与反向传播的关系",
            criteria=[
                "本轮存在真实检索命中（引用校验链 verified≥1）",
                "回答携带结构化引用",
                "引用校验链无伪造剔除（removed=0）",
            ],
        ),
        SmokeCase(
            case_id="C6",
            scenario="多轮对话 · 上下文保持（真实模型）",
            user_input="（第 2 轮）你还记得我叫什么名字、我的目标是多少吗？",
            criteria=[
                "第二轮复述姓名「林晓」",
                "第二轮复述目标「90 分」",
                "会话历史包含两轮用户输入",
            ],
        ),
    ]


# ── 报告 ──────────────────────────────────────────────────────


def render_markdown(cases: list[SmokeCase], model_name: str) -> str:
    lines = [
        "# 真实模型端到端冒烟报告（DeepSeek）",
        "",
        f"> 模型：`{model_name}`；链路：CollaborativeAgentGraph.run（与前端对话同链路）。",
        "> 生成方式：`backend/scripts/smoke_real_model.py`；判据聚焦系统机制（非模型措辞）。",
        "",
        "## 汇总",
        "",
        "| 用例 | 场景 | 结果 | 尝试次数 | 耗时 | 归因 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for case in cases:
        lines.append(
            f"| {case.case_id} | {case.scenario} | {case.status} | "
            f"{case.attempts_used} | {case.duration_s:.0f}s | {case.attribution or '—'} |"
        )
    lines.append("")
    for case in cases:
        lines += [
            f"## {case.case_id} {case.scenario}",
            "",
            f"- 用户输入：{case.user_input[:120]}",
            "- 验收判据：",
        ]
        lines += [f"  - {criterion}" for criterion in case.criteria]
        lines += [
            f"- 结果：**{case.status}**（尝试 {case.attempts_used} 次，{case.duration_s:.0f}s）"
        ]
        if case.output_summary:
            lines.append(f"- 模型输出摘要：{case.output_summary}")
        lines.append("- 证据：")
        lines += [f"  - {evidence}" for evidence in case.evidence]
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="真实模型端到端冒烟（配额敏感，按需选择用例）")
    parser.add_argument(
        "--cases",
        default=None,
        help="逗号分隔的用例子集（如 C1,C5）；默认全部 6 个",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=REPO_ROOT / "data" / "smoke-workspace",
        help="冒烟工作区（产物区与学情库落在此下，可复看）",
    )
    parser.add_argument(
        "--md-out",
        type=Path,
        default=REPO_ROOT / "docs" / "competition" / "real-model-smoke.md",
        help="Markdown 报告输出路径",
    )
    args = parser.parse_args(argv)

    settings = DeepSeekSettings.from_env()  # 缺配置尽早失败（提示补齐 .env）
    print(f"model={settings.model} base_url={settings.base_url}")

    workspace = args.workspace
    workspace.mkdir(parents=True, exist_ok=True)
    store = LearningRecordStore(workspace / "smoke-learning.db")

    cases = build_cases()
    if args.cases:
        wanted = {item.strip().upper() for item in args.cases.split(",") if item.strip()}
        cases = [case for case in cases if case.case_id in wanted]
        if not cases:
            print("错误：--cases 无可用用例（可用：C1..C6）")
            return 2

    try:
        for case in cases:
            print(f"[RUN] {case.case_id} {case.scenario}")
            run_case(case, workspace, store)
            print(f"      → {case.status}（{case.attempts_used} 次，{case.duration_s:.0f}s）")
    finally:
        store.close()

    args.md_out.parent.mkdir(parents=True, exist_ok=True)
    args.md_out.write_text(render_markdown(cases, settings.model), encoding="utf-8")
    print(
        json.dumps(
            [
                {
                    "case_id": case.case_id,
                    "status": case.status,
                    "attempts": case.attempts_used,
                    "attribution": case.attribution,
                }
                for case in cases
            ],
            ensure_ascii=False,
        )
    )
    passed = sum(1 for case in cases if case.status == "PASS")
    print(f"{passed}/{len(cases)} 用例通过；报告已写入 {args.md_out}")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
