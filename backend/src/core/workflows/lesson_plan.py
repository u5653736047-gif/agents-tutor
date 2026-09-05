"""教案制作工作流的首个注册实现（lesson-workflow-design §四）。

步骤设计对应设计稿四步状态机：collect（素材收集）→ draft（六段成稿）
→ generate（docx 生成，产物区自动授权）→ review（evaluator 质量校验）。
步骤指令是分派给 Worker 的 HumanMessage 模板：素材在共享 messages 里
跨步骤累积（对 tool 模式嵌套 ask「失忆」根因的直接修正），因此除
collect 外各步骤明确要求「不要重新检索，直接使用历史素材」。
"""

from __future__ import annotations

import json
import re
from typing import Any

from core.state import AgentRole
from core.workflows.definition import (
    WorkflowDefinition,
    WorkflowStepDefinition,
)

LESSON_PLAN_WORKFLOW_ID = "lesson_plan"

# review 步骤 verdict 允许值（结构化校验结论，dispatch 据此决定回退）。
REVIEW_VERDICT_PASS = "pass"
REVIEW_VERDICT_REVISE = "revise"

_SIX_SECTION_TEMPLATE = (
    "一、教学目标（对齐素材稿中的课标要点；未检索到课标时明确注明"
    "「未对齐课标，按教材设计」）；"
    "二、教学重难点；三、学情假设；四、教学过程（各环节标注时间分配）；"
    "五、课堂活动；六、评价设计"
)

_COLLECT_INSTRUCTION = (
    "【教案工作流 · 步骤 1/4：素材收集】课题：《{topic}》{grade_hint}\n"
    "本步只做素材收集与整理：不要撰写教案，不要生成任何文件。\n"
    "1. 用 search_knowledge 从多个角度检索教材内容（2-4 个不同查询，"
    "覆盖概念定义、推导过程、应用案例）；\n"
    "2. 用 source 过滤尝试检索课程标准材料；检索到课标则整理对齐要点，"
    "未检索到则记录「未检索到课标，按教材设计」；\n"
    "3. 最终回答输出结构化素材稿：①核心概念清单 ②关键推导/案例 "
    "③可用的课堂活动素材 ④课标对齐要点或缺失说明 ⑤引用来源"
    "（document/page 列表）。只输出素材稿本身。"
)

_DRAFT_INSTRUCTION = (
    "【教案工作流 · 步骤 2/4：教案成稿】课题：《{topic}》{grade_hint}\n"
    "素材稿已在对话历史中——直接使用，**不要重新检索**。\n"
    "按六段结构撰写完整教案：" + _SIX_SECTION_TEMPLATE + "。\n"
    "**格式要求（导出依赖）**：使用 Markdown——六段各用 `## ` 标题"
    "（如 `## 一、教学目标`），段落用普通文本，要点用列表；内容取材于"
    "历史素材稿并标注来源（document/page）。\n"
    "最终回答只输出教案全文（Markdown），不要附加解释。"
)

_GENERATE_INSTRUCTION = (
    "【教案工作流 · 步骤 3/4：文档导出】课题：《{topic}》\n"
    "教案全文（Markdown）已由系统暂存——**不要复述正文，不要调用 "
    "officecli_edit 写内容**。\n"
    "只需调用一次 export_workflow_docx 工具（无需参数）：它会把暂存的"
    "教案确定性写入产物目录的 docx 并自动验证段落数（产物区自动授权，"
    "无需审批）。\n"
    "返回 ok=false 时如实说明错误原因；成功后最终回答只报告：文件路径"
    " + 段落数。"
)

_REVIEW_INSTRUCTION = (
    "【教案工作流 · 步骤 4/4：质量校验】课题：《{topic}》\n"
    "对话历史包含素材稿、教案全文与已生成文档的章节清单；可用 "
    "officecli_inspect（view，只读）核对文档实际内容。\n"
    "按清单校验：1) 六段结构完整；2) 教学目标与课标要点（或缺失说明）"
    "对应；3) 教学过程各环节有时间分配；4) 引用来源可追溯。\n"
    "最终回答只输出一行 JSON："
    '{{"verdict": "pass"|"revise", '
    '"revision_points": ["仅当 verdict=revise 时给出结构性缺失"], '
    '"summary": "一句话结论"}}\n'
    "判定口径：只对结构性缺失（缺段、目标与课标/教材错位、过程无时间"
    "分配）判 revise；文字打磨与篇幅偏好不构成 revise。"
)

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)

# 每个工作流允许一次「review 不合格 → 回退成稿/生成」的有界循环
# （lesson-workflow-design §十三-3）。
LESSON_PLAN_REVISE_ROUNDS = 1

# review 步骤在 steps 中的位置与回退目标（draft）的位置。
_LESSON_PLAN_REVIEW_INDEX = 3
_LESSON_PLAN_DRAFT_INDEX = 1


def _lesson_plan_revise_policy(
    step_index: int,
    summary: str | None,
) -> int | None:
    """review 步骤的回退决策：verdict=revise → 回退 draft 重新成稿。"""
    if step_index != _LESSON_PLAN_REVIEW_INDEX:
        return None
    parsed = parse_review_verdict(summary)
    if parsed is None:
        return None
    if parsed.get("verdict") != REVIEW_VERDICT_REVISE:
        return None
    return _LESSON_PLAN_DRAFT_INDEX


_LESSON_PLAN = WorkflowDefinition(
    workflow_id=LESSON_PLAN_WORKFLOW_ID,
    title="教案制作工作流",
    steps=(
        WorkflowStepDefinition(
            step_id="collect",
            worker_role=AgentRole.TEACHING_ASSISTANT,
            instruction_template=_COLLECT_INSTRUCTION,
            iteration_budget=8,
            on_failure="abort",
        ),
        WorkflowStepDefinition(
            step_id="draft",
            worker_role=AgentRole.TEACHING_ASSISTANT,
            instruction_template=_DRAFT_INSTRUCTION,
            iteration_budget=4,
            on_failure="retry",
        ),
        WorkflowStepDefinition(
            step_id="generate",
            worker_role=AgentRole.TEACHING_ASSISTANT,
            instruction_template=_GENERATE_INSTRUCTION,
            iteration_budget=6,
            on_failure="retry",
            artifact_filename_template="教案-{topic}.docx",
        ),
        WorkflowStepDefinition(
            step_id="review",
            worker_role=AgentRole.EVALUATOR,
            instruction_template=_REVIEW_INSTRUCTION,
            # 读重步骤：officecli_inspect 多项检视（outline/text/stats/
            # validate）+ 结论一轮，4 次预算被真实冒烟证实必触顶
            # （稳定性冒烟 2026-08-30：4 条教案 3 条 review 在第 4 轮
            # 触发 react_iteration_limit）；放宽到 8（硬帽 12 内），
            # on_failure=continue 仍是兜底。
            iteration_budget=8,
            on_failure="continue",
        ),
    ),
    max_revise_rounds=LESSON_PLAN_REVISE_ROUNDS,
    revise_policy=_lesson_plan_revise_policy,
)


def parse_review_verdict(summary: str | None) -> dict[str, Any] | None:
    """从 review 步骤终端输出里宽容解析结构化校验结论。

    解析失败（模型未按格式输出）返回 None，调度器按 pass 处理并在
    summary 里保留原文——校验是增强项，不因格式违约阻断工作流。
    """
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
    verdict = parsed.get("verdict")
    if verdict not in {REVIEW_VERDICT_PASS, REVIEW_VERDICT_REVISE}:
        return None
    return parsed


def lesson_plan_workflow() -> WorkflowDefinition:
    """返回教案工作流定义（函数形态便于未来按 env 定制）。"""
    return _LESSON_PLAN


__all__ = [
    "LESSON_PLAN_REVISE_ROUNDS",
    "LESSON_PLAN_WORKFLOW_ID",
    "REVIEW_VERDICT_PASS",
    "REVIEW_VERDICT_REVISE",
    "lesson_plan_workflow",
    "parse_review_verdict",
]
