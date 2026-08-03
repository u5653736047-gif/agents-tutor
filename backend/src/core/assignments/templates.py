"""备课素材骨架生成的纯函数。"""

from __future__ import annotations

from typing import Literal

from .models import LessonMaterialSkeleton, LessonSection, QuizQuestionSkeleton, QuizSkeleton

_DIFFICULTY_ORDER: tuple[Literal["easy", "medium", "hard"], ...] = (
    "easy",
    "medium",
    "hard",
)


def build_quiz_skeleton(
    *,
    knowledge_points: list[str],
    question_count: int,
    difficulty: Literal["easy", "medium", "hard", "mixed"],
    objective_ratio: float,
    total_points: int,
) -> tuple[QuizSkeleton, list[str]]:
    """按确定性规则生成测验骨架；内容占位由 Agent 模型填充。

    题型：前 round(N * objective_ratio) 题为客观题（4 选项占位）；
    知识点轮询分配；分值 base=total//N，余数均摊到前几题，总和恒等。
    """
    warnings: list[str] = []
    count = question_count
    kps = [item.strip() for item in knowledge_points]

    objective_count = round(count * objective_ratio)
    base_points = total_points // count
    remainder = total_points % count

    difficulties: list[Literal["easy", "medium", "hard"]]
    if difficulty == "mixed":
        easy_count = round(count * 0.3)
        hard_count = round(count * 0.3)
        medium_count = count - easy_count - hard_count
        difficulties = (
            [_DIFFICULTY_ORDER[0]] * easy_count
            + [_DIFFICULTY_ORDER[1]] * medium_count
            + [_DIFFICULTY_ORDER[2]] * hard_count
        )
    else:
        difficulties = [difficulty] * count

    if len(kps) < count:
        warnings.append(
            f"知识点数量({len(kps)})少于题目数({count})，部分知识点将重复使用"
        )

    questions: list[QuizQuestionSkeleton] = []
    for index in range(count):
        points = base_points + (1 if index < remainder else 0)
        knowledge_point = kps[index % len(kps)]
        kind: Literal["objective", "subjective"] = (
            "objective" if index < objective_count else "subjective"
        )
        questions.append(
            QuizQuestionSkeleton(
                question_id=f"q{index + 1}",
                kind=kind,
                difficulty=difficulties[index],
                knowledge_point=knowledge_point,
                points=points,
                stem_placeholder=(
                    f"【题干占位】请围绕知识点“{knowledge_point}”补充题干"
                ),
                options_placeholder=(
                    ["A. 【选项占位】", "B. 【选项占位】", "C. 【选项占位】", "D. 【选项占位】"]
                    if kind == "objective"
                    else None
                ),
                answer_placeholder=(
                    "【答案占位】" if kind == "objective" else "【参考答案与评分要点占位】"
                ),
            )
        )

    skeleton = QuizSkeleton(
        title_placeholder=f"《{'、'.join(kps)} 综合测验》—— 请补充完整标题与说明",
        knowledge_points=kps,
        total_points=float(total_points),
        questions=questions,
    )
    return skeleton, warnings


def build_lesson_material_skeleton(
    *,
    topic: str,
    knowledge_points: list[str],
    estimated_minutes: int,
    include_exercises: bool,
) -> LessonMaterialSkeleton:
    """按固定章节顺序生成教案素材骨架。"""
    kps = [item.strip() for item in knowledge_points]

    sections: list[LessonSection] = []
    section_id = 1

    def add_section(
        section_type: Literal["warmup", "knowledge_points", "examples", "exercises", "summary"],
        title: str,
        content: str,
    ) -> None:
        nonlocal section_id
        sections.append(
            LessonSection(
                section_id=f"s{section_id}",
                section_type=section_type,
                title_placeholder=title,
                content_placeholder=content,
            )
        )
        section_id += 1

    add_section(
        "warmup",
        "【导入】复习与情境导入",
        "【占位】设计 5-10 分钟导入：回顾前置知识，引出本节课问题情境",
    )
    add_section(
        "knowledge_points",
        f"【新授】知识点讲解：{'、'.join(kps) if kps else '待补充知识点'}",
        "【占位】逐知识点讲解核心概念、公式推导与易错点",
    )

    examples = kps[:3] if kps else []
    if not examples:
        examples = ["通用示例一", "通用示例二"]
    for example in examples:
        add_section(
            "examples",
            f"【例题】{example}",
            "【占位】给出例题题干、逐步求解过程与讲解要点",
        )

    if include_exercises:
        add_section(
            "exercises",
            "【练习】随堂练习",
            "【占位】每知识点 1-2 道练习题，注明参考答案",
        )

    add_section(
        "summary",
        "【小结】课堂小结",
        "【占位】总结核心结论，回顾常见错误与易混淆点",
    )

    return LessonMaterialSkeleton(
        topic=topic,
        estimated_minutes=estimated_minutes,
        objectives=[
            "【目标占位】掌握本课核心概念与原理",
            "【目标占位】能够运用所学方法解决典型问题",
            "【目标占位】理解易错点并完成迁移应用",
        ],
        knowledge_points=kps,
        sections=sections,
    )


__all__ = ["build_lesson_material_skeleton", "build_quiz_skeleton"]
