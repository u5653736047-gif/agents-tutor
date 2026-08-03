"""作业批改领域模型测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.assignments.models import AnswerKey, AnswerKeyItem, GradedQuestion


def test_answer_key_rejects_objective_item_without_correct_answer() -> None:
    with pytest.raises(ValidationError):
        AnswerKey(
            items=[
                AnswerKeyItem(
                    question_id="q1",
                    kind="objective",
                    points=10,
                )
            ]
        )


def test_answer_key_rejects_duplicate_question_ids() -> None:
    with pytest.raises(ValidationError, match="重复"):
        AnswerKey(
            items=[
                AnswerKeyItem(question_id="q1", kind="objective", correct_answer="A", points=5),
                AnswerKeyItem(question_id="q1", kind="subjective", points=5),
            ]
        )


def test_subjective_item_does_not_require_correct_answer() -> None:
    key = AnswerKey(
        items=[
            AnswerKeyItem(
                question_id="q2",
                kind="subjective",
                keyword_hints=["收敛"],
                points=15,
            )
        ]
    )
    assert key.items[0].correct_answer is None


def test_graded_question_rejects_negative_score() -> None:
    with pytest.raises(ValidationError):
        GradedQuestion(
            question_id="q1",
            kind="objective",
            knowledge_points=[],
            points=10,
            score=-1,
        )
