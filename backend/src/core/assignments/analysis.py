"""学情诊断报告组装纯函数。"""

from __future__ import annotations

from datetime import UTC, datetime

from .models import (
    ClassPerformanceReport,
    ClassSummary,
    KnowledgePointAggregate,
    WeakPoint,
)


def build_class_report(
    *,
    class_id: str,
    scope: ClassSummary,
    knowledge_points: list[KnowledgePointAggregate],
    weak_threshold: float,
    generated_at: datetime | None = None,
) -> ClassPerformanceReport:
    """组装学情报告：按准确率升序排列，标记并抽取薄弱点（严格小于阈值）。"""
    ordered = sorted(knowledge_points, key=lambda item: item.accuracy)
    marked = [
        item.model_copy(
            update={"is_weak": item.accuracy < weak_threshold}
        )
        for item in ordered
    ]
    weak_points = [
        WeakPoint(
            knowledge_point=item.knowledge_point,
            total_questions=item.total_questions,
            correct=item.correct,
            accuracy=item.accuracy,
        )
        for item in marked
        if item.is_weak
    ]
    return ClassPerformanceReport(
        class_id=class_id,
        scope=scope,
        knowledge_points=marked,
        weak_points=weak_points,
        weak_threshold=weak_threshold,
        generated_at=generated_at or datetime.now(UTC),
    )


__all__ = ["build_class_report"]
