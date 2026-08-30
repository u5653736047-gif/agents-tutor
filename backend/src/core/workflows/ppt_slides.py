"""PPT 课件制作工作流（ppt-workflow-design，workflow_id=ppt_slides）。

四步状态机：collect（检索+图片盘点）→ outline（严格 JSON 大纲，结构
门禁）→ generate（单次调用 export_workflow_pptx，落盘闸）→ review
（evaluator 只读核对，verdict=revise 回退 outline 一轮）。

设计要点（详见 docs/ppt-workflow-design.md）：
- 大纲是导出的前置条件：坏 JSON 在本步硬失败（output_validator），
  由 retry 重出——与 review 的「按 pass 处理」宽容哲学刻意不同；
- 页数硬边界 [10, 16]：<10 判失败、>16 截断；机械门禁只认硬边界，
  ±2 页容差不构成失败（防回退环空转）；
- 单页字段确定性截断（永不失败）：title[:40]、points[:6]×60 字、
  notes[:150]；layout 非法归一 content；image 仅 content 页保留；
- 例外：任一页 title 收敛后为空 → 整体返回 None（硬失败交 retry）——
  空标题页 review 必判 revise，不如本步重出（ppt-template-theme-plan 2.5）。
"""

from __future__ import annotations

import json
import re
from typing import Any

from core.state import AgentRole
from core.workflows.definition import WorkflowDefinition, WorkflowStepDefinition

PPT_SLIDES_WORKFLOW_ID = "ppt_slides"

REVIEW_VERDICT_PASS = "pass"
REVIEW_VERDICT_REVISE = "revise"

# 页数硬边界与默认值（ppt-workflow-design §四）：[10, 16] 为机械门禁
# 硬边界；page_count 参数默认 12，仅作为 outline 指令里的目标值。
PPT_PAGE_HARD_MIN = 10
PPT_PAGE_HARD_MAX = 16
PPT_PAGE_DEFAULT = 12

# 大纲字段确定性截断上限（ppt-workflow-design §四）。
_OUTLINE_TITLE_MAX = 40
_OUTLINE_POINT_MAX = 60
_OUTLINE_POINTS_MAX = 6
_OUTLINE_NOTES_MAX = 150
_OUTLINE_AUDIENCE_MAX = 30

_VALID_LAYOUTS = ("cover", "section", "content", "closing")

_PPT_REVIEW_INDEX = 3
_PPT_OUTLINE_INDEX = 1

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


def parse_deck_outline(text: str | None) -> dict[str, Any] | None:
    """从 outline 步骤终端输出解析并**收敛**课件大纲。

    宽容提取（首个 JSON 对象）+ 结构校验 + 确定性截断，与
    parse_review_verdict 同哲学但更严格：任何结构不合法（非对象、
    slides 非数组、页数 < PPT_PAGE_HARD_MIN）返回 None——大纲是导出的
    前置条件，硬失败交 retry 重出。收敛后的结果可直接 JSON 序列化。
    """
    if not text or not isinstance(text, str):
        return None
    match = _JSON_OBJECT.search(text)
    if match is None:
        return None
    try:
        raw = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("slides"), list):
        return None
    raw_slides = raw["slides"]
    if len(raw_slides) < PPT_PAGE_HARD_MIN:
        return None
    slides = [_converge_slide(item) for item in raw_slides[:PPT_PAGE_HARD_MAX]]
    # 空标题硬失败（ppt-template-theme-plan 2.5，评估遗留 🟡）：空标题页
    # 在 stats 里记「Slides without title」，review 清单第 3 条必然判
    # revise；不如在本步就 retry 重出（title 必填已在指令中声明）。
    if any(not slide["title"] for slide in slides):
        return None
    return {
        "deck_title": _clean_str(raw.get("deck_title"), _OUTLINE_TITLE_MAX),
        "audience": _clean_str(raw.get("audience"), _OUTLINE_AUDIENCE_MAX),
        "slides": slides,
    }


def _clean_str(value: object, limit: int) -> str:
    return str(value).strip()[:limit] if value is not None else ""


def _converge_slide(item: object) -> dict[str, Any]:
    """单页确定性收敛：永不失败，越界一律截断/归一。"""
    slide = item if isinstance(item, dict) else {}
    layout = slide.get("layout")
    layout = layout if layout in _VALID_LAYOUTS else "content"
    raw_points = slide.get("points")
    points = (
        [_clean_str(point, _OUTLINE_POINT_MAX) for point in raw_points]
        if isinstance(raw_points, list)
        else []
    )
    points = [point for point in points if point][:_OUTLINE_POINTS_MAX]
    converged: dict[str, Any] = {
        "layout": layout,
        "title": _clean_str(slide.get("title"), _OUTLINE_TITLE_MAX),
        "points": points,
    }
    notes = _clean_str(slide.get("notes"), _OUTLINE_NOTES_MAX)
    if notes:
        converged["notes"] = notes
    if layout == "content" and isinstance(slide.get("image"), str) and slide["image"].strip():
        converged["image"] = slide["image"].strip()
    return converged


def _normalize_page_count(params: dict[str, str]) -> dict[str, str]:
    """page_count 确定性规整：非数字/缺省 → "12"；越界截断到 [10, 16]。"""
    raw = str(params.get("page_count", "")).strip()
    if not raw.isdigit():
        normalized = PPT_PAGE_DEFAULT
    else:
        normalized = int(raw)
    normalized = max(PPT_PAGE_HARD_MIN, min(PPT_PAGE_HARD_MAX, normalized))
    return {**params, "page_count": str(normalized)}


# 大纲步骤的结构门禁：坏 JSON 在本步硬失败（retry 重出），不进暂存。
_outline_validator = lambda text: parse_deck_outline(text) is not None

_COLLECT_INSTRUCTION = (
    "【课件工作流 · 步骤 1/4：素材收集】课题：《{topic}》{grade_hint}\n"
    "本步只做素材收集与整理：不要写大纲，不要生成任何文件。\n"
    "1. 用 search_knowledge 从多个角度检索教材内容（2-4 个不同查询，"
    "覆盖概念定义、推导/流程、应用案例）；\n"
    "2. 课件按章节组织：整理出适合演示的章节骨架与每章核心要点；\n"
    "3. 用文件工具列出工作区内 .png/.jpg/.jpeg 图片并各给一句话描述；"
    "只列真实存在的文件，列不出就写明「无可用图片素材」；"
    "**禁止建议联网下载图片**；\n"
    "4. 最终回答输出结构化素材稿：①章节骨架 ②每章核心要点 ③案例/"
    "图示建议 ④引用来源（document/page）⑤可用图片清单或缺失说明。"
)

_OUTLINE_INSTRUCTION = (
    "【课件工作流 · 步骤 2/4：课件大纲】课题：《{topic}》{grade_hint}\n"
    "素材稿已在对话历史——直接使用，**不要重新检索**。\n"
    "输出**且只输出**一个 JSON 对象（无其他文字、无代码块围栏），"
    "Schema 如下：\n"
    '{{"deck_title": "≤40字", "audience": "≤30字可空", "slides": '
    '[{{"layout": "cover|section|content|closing", "title": "≤40字必填", '
    '"points": ["每条≤60字，2~6条；cover/section 可为空数组"], '
    '"notes": "讲稿≤150字可省略", "image": "仅 content 页可省略"}}]}}\n'
    "要求：共 {page_count} 页左右（硬边界 10~16 页，封面页与小结页计入"
    "总数）；第 1 页 layout=cover（deck_title 作标题）、最后 1 页 "
    "layout=closing（小结）；中间以 content 为主、章节转折可用 section；"
    "每页要点直接可讲，内容取材于素材稿；讲稿 notes 写该页口述要点。\n"
    "历史中存在评审修订点时必须逐条落实。"
)

_GENERATE_INSTRUCTION = (
    "【课件工作流 · 步骤 3/4：课件导出】课题：《{topic}》\n"
    "课件大纲（JSON）已由系统暂存——**不要复述内容，不要手写任何 "
    "officecli 命令**。\n"
    "只需调用一次 export_workflow_pptx 工具（无需参数）：它会把大纲"
    "确定性写入产物目录的 pptx 并自动验证页数（产物区自动授权，无需"
    "审批）。\n"
    "返回 ok=false 时如实说明错误原因，不声称成功；成功后最终回答只"
    "报告：文件路径 + 页数 + 主题风格（取返回值 template 字段）。"
)

_REVIEW_INSTRUCTION = (
    "【课件工作流 · 步骤 4/4：质量校验】课题：《{topic}》\n"
    "对话历史包含素材稿与课件大纲；产物可用 officecli_inspect 只读核对"
    "（view stats / view outline / validate）。\n"
    "校验清单：1) 实际页数 ≥ 10；2) 第 1 页为封面、最后 1 页为小结；"
    "3) 每页标题非空、要点 ≤6 条、无整页空白；4) 内容与素材稿一致、"
    "引用可追溯；5) 主内容页讲稿覆盖。\n"
    "最终回答只输出一行 JSON："
    '{{"verdict": "pass"|"revise", '
    '"revision_points": ["仅当 verdict=revise 时给出结构性缺失"], '
    '"summary": "一句话结论"}}\n'
    "判定口径：只对结构性缺失（页数不足、缺封面/小结、整页空白、内容"
    "与素材明显错位）判 revise；文字打磨与版式偏好不构成 revise。"
)

def _ppt_revise_policy(
    step_index: int,
    summary: str | None,
) -> int | None:
    """review 判 revise → 回退 outline 重出大纲（generate 由自身 retry 覆盖）。"""
    if step_index != _PPT_REVIEW_INDEX:
        return None
    parsed = _parse_review_verdict(summary)
    if parsed is None or parsed.get("verdict") != REVIEW_VERDICT_REVISE:
        return None
    return _PPT_OUTLINE_INDEX


def _parse_review_verdict(summary: str | None) -> dict[str, Any] | None:
    if not summary:
        return None
    match = _JSON_OBJECT.search(summary)
    if match is None:
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    if parsed.get("verdict") not in {REVIEW_VERDICT_PASS, REVIEW_VERDICT_REVISE}:
        return None
    return parsed


_PPT_SLIDES = WorkflowDefinition(
    workflow_id=PPT_SLIDES_WORKFLOW_ID,
    title="PPT 课件制作工作流",
    steps=(
        WorkflowStepDefinition(
            step_id="collect",
            worker_role=AgentRole.TEACHING_ASSISTANT,
            instruction_template=_COLLECT_INSTRUCTION,
            iteration_budget=8,
            on_failure="abort",
        ),
        WorkflowStepDefinition(
            step_id="outline",
            worker_role=AgentRole.TEACHING_ASSISTANT,
            instruction_template=_OUTLINE_INSTRUCTION,
            iteration_budget=6,
            on_failure="retry",
            output_validator=_outline_validator,
        ),
        WorkflowStepDefinition(
            step_id="generate",
            worker_role=AgentRole.TEACHING_ASSISTANT,
            instruction_template=_GENERATE_INSTRUCTION,
            iteration_budget=6,
            on_failure="retry",
            requires_artifact=True,
            artifact_filename_template="课件-{topic}.pptx",
        ),
        WorkflowStepDefinition(
            step_id="review",
            worker_role=AgentRole.EVALUATOR,
            instruction_template=_REVIEW_INSTRUCTION,
            # 与教案 review 同一暴露面：读重步骤需要多次 inspect 检视
            # + 结论一轮，5 次预算边界过紧（真实冒烟触顶过），放宽到
            # 8（硬帽 12 内）；on_failure=continue 仍是兜底。
            iteration_budget=8,
            on_failure="continue",
        ),
    ),
    max_revise_rounds=1,
    revise_policy=_ppt_revise_policy,
    extra_params=frozenset({"page_count", "style_hint"}),
    param_normalizer=_normalize_page_count,
)

def ppt_slides_workflow() -> WorkflowDefinition:
    return _PPT_SLIDES


__all__ = [
    "PPT_PAGE_DEFAULT",
    "PPT_PAGE_HARD_MAX",
    "PPT_PAGE_HARD_MIN",
    "PPT_SLIDES_WORKFLOW_ID",
    "parse_deck_outline",
    "ppt_slides_workflow",
]
