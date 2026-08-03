"""学情报告组装测试。"""

from __future__ import annotations

from core.assignments.analysis import build_class_report
from core.assignments.models import ClassSummary, KnowledgePointAggregate


def _aggregate(point: str, accuracy: float) -> KnowledgePointAggregate:
    return KnowledgePointAggregate(
        knowledge_point=point,
        total_questions=10,
        correct=round(accuracy * 10),
        accuracy=accuracy,
    )


def test_marks_weak_points_below_threshold() -> None:
    report = build_class_report(
        class_id="class-1",
        scope=ClassSummary(submission_count=1),
        knowledge_points=[
            _aggregate("指针", 0.4),
            _aggregate("浮点数", 0.8),
            _aggregate("边界情况", 0.6),  # 恰好等于阈值，不算薄弱
        ],
        weak_threshold=0.6,
    )

    assert [item.knowledge_point for item in report.weak_points] == ["指针"]
    by_point = {item.knowledge_point: item for item in report.knowledge_points}
    assert by_point["边界情况"].accuracy == 0.6
    assert by_point["浮点数"].is_weak is False


def test_knowledge_points_sorted_ascending_by_accuracy() -> None:
    report = build_class_report(
        class_id="class-1",
        scope=ClassSummary(submission_count=1),
        knowledge_points=[
            _aggregate("A", 0.9),
            _aggregate("B", 0.3),
            _aggregate("C", 0.6),
        ],
        weak_threshold=0.5,
    )

    assert [item.knowledge_point for item in report.knowledge_points] == ["B", "C", "A"]


def test_empty_class_returns_zeroed_report() -> None:
    report = build_class_report(
        class_id="class-1",
        scope=ClassSummary(),
        knowledge_points=[],
        weak_threshold=0.6,
    )

    assert report.scope.submission_count == 0
    assert report.weak_points == []
    assert report.knowledge_points == []
