"""备课素材骨架生成测试。"""

from __future__ import annotations

from core.assignments.templates import (
    build_lesson_material_skeleton,
    build_quiz_skeleton,
)


def test_quiz_skeleton_respects_count_kinds_and_total_points() -> None:
    skeleton, warnings = build_quiz_skeleton(
        knowledge_points=["浮点数", "指针", "梯度下降", "反向传播", "注意力"],
        question_count=5,
        difficulty="mixed",
        objective_ratio=0.6,
        total_points=100,
    )

    assert len(skeleton.questions) == 5
    objective_count = sum(1 for item in skeleton.questions if item.kind == "objective")
    assert objective_count == 3  # round(5 * 0.6)
    assert sum(item.points for item in skeleton.questions) == 100
    assert skeleton.total_points == 100
    assert all(
        len(item.options_placeholder) == 4
        for item in skeleton.questions
        if item.kind == "objective"
    )
    assert warnings == []


def test_quiz_skeleton_round_robins_knowledge_points_and_warns() -> None:
    skeleton, warnings = build_quiz_skeleton(
        knowledge_points=["线性回归"],
        question_count=3,
        difficulty="easy",
        objective_ratio=0.5,
        total_points=30,
    )

    assert [item.knowledge_point for item in skeleton.questions] == [
        "线性回归",
        "线性回归",
        "线性回归",
    ]
    assert any("重复使用" in warning for warning in warnings)


def test_quiz_skeleton_single_difficulty_mode() -> None:
    skeleton, _ = build_quiz_skeleton(
        knowledge_points=["CNN"],
        question_count=4,
        difficulty="hard",
        objective_ratio=0.5,
        total_points=100,
    )

    assert all(item.difficulty == "hard" for item in skeleton.questions)


def test_quiz_skeleton_mixed_difficulty_has_all_tiers() -> None:
    skeleton, _ = build_quiz_skeleton(
        knowledge_points=["CNN"],
        question_count=10,
        difficulty="mixed",
        objective_ratio=0.5,
        total_points=100,
    )

    tiers = {item.difficulty for item in skeleton.questions}
    assert tiers == {"easy", "medium", "hard"}


def test_lesson_material_sections_follow_fixed_order() -> None:
    skeleton = build_lesson_material_skeleton(
        topic="梯度下降",
        knowledge_points=["学习率", "动量"],
        estimated_minutes=45,
        include_exercises=True,
    )

    assert skeleton.topic == "梯度下降"
    assert [item.section_type for item in skeleton.sections] == [
        "warmup",
        "knowledge_points",
        "examples",
        "examples",
        "exercises",
        "summary",
    ]
    assert len(skeleton.objectives) == 3


def test_lesson_material_exercises_optional() -> None:
    with_exercises = build_lesson_material_skeleton(
        topic="CNN",
        knowledge_points=[],
        estimated_minutes=45,
        include_exercises=True,
    )
    without_exercises = build_lesson_material_skeleton(
        topic="CNN",
        knowledge_points=[],
        estimated_minutes=45,
        include_exercises=False,
    )

    assert any(item.section_type == "exercises" for item in with_exercises.sections)
    assert not any(item.section_type == "exercises" for item in without_exercises.sections)
