"""批改记录存储与学情聚合测试。"""

from __future__ import annotations

from pathlib import Path

from core.assignments.grading import grade_questions
from core.assignments.models import AnswerKey, AnswerKeyItem, GradingDraft, QuestionAnswer
from core.assignments.store import AssignmentStore


def _graded_draft() -> GradingDraft:
    return grade_questions(
        [
            QuestionAnswer(question_id="q1", answer="B"),
            QuestionAnswer(question_id="q2", answer="wrong"),
            QuestionAnswer(question_id="q3", answer="学习率决定收敛速度，需要仔细调参。"),
        ],
        AnswerKey(
            items=[
                AnswerKeyItem(
                    question_id="q1",
                    kind="objective",
                    correct_answer="B",
                    points=10,
                    knowledge_points=["浮点数"],
                ),
                AnswerKeyItem(
                    question_id="q2",
                    kind="objective",
                    correct_answer="0.5",
                    points=10,
                    knowledge_points=["浮点数"],
                ),
                AnswerKeyItem(
                    question_id="q3",
                    kind="subjective",
                    keyword_hints=["收敛", "学习率"],
                    points=20,
                    knowledge_points=["梯度下降"],
                ),
            ]
        ),
        title="浮点数测验",
    )


def test_store_persists_submission_and_questions_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "assignments.sqlite"
    with AssignmentStore(path) as store:
        result = store.add_grading_result(
            _graded_draft(),
            user_id="u1",
            session_id="s1",
        )

    with AssignmentStore(path) as reopened:
        records = reopened.list_submissions(user_id="u1")

    assert len(records) == 1
    assert records[0].submission_id == result.submission_id
    assert records[0].total_points == 40
    assert records[0].objective_correct == 1
    assert len(records[0].questions) == 3
    assert records[0].questions[0].is_correct is True
    assert records[0].questions[2].knowledge_points == ["梯度下降"]


def test_store_isolates_by_user_key(tmp_path: Path) -> None:
    with AssignmentStore(tmp_path / "assignments.sqlite") as store:
        store.add_grading_result(_graded_draft(), user_id="u1", session_id="s1")
        store.add_grading_result(_graded_draft(), user_id="u2", session_id="s1")

        assert len(store.list_submissions(user_id="u1")) == 1
        assert len(store.list_submissions(user_id="u2")) == 1
        assert len(store.list_submissions()) == 0  # 匿名租户独立


def test_aggregate_accuracy_counts_only_objective_questions() -> None:
    with AssignmentStore(":memory:") as store:
        store.add_grading_result(_graded_draft(), user_id="u1", session_id="s1")

        aggregates = store.aggregate_accuracy(["u1"])

    by_point = {item.knowledge_point: item for item in aggregates}
    # 客观题：浮点数 2 题对 1 题 → 0.5；主观题不稀释准确率
    assert by_point["浮点数"].total_questions == 2
    assert by_point["浮点数"].correct == 1
    assert by_point["浮点数"].accuracy == 0.5
    assert "梯度下降" not in by_point


def test_class_summary_computes_stats() -> None:
    with AssignmentStore(":memory:") as store:
        store.add_grading_result(_graded_draft(), user_id="u1", session_id="s1")
        store.add_grading_result(_graded_draft(), user_id="u2", session_id="s1")

        summary = store.class_summary(["u1", "u2"])

    assert summary.submission_count == 2
    assert summary.student_count == 2
    assert summary.max_score == summary.min_score == summary.average_score


def test_empty_scope_returns_zeroed_summary() -> None:
    with AssignmentStore(":memory:") as store:
        store.add_grading_result(_graded_draft(), user_id="u1", session_id="s1")

        summary = store.class_summary([])
        aggregates = store.aggregate_accuracy([])

    assert summary.submission_count == 0
    assert aggregates == []


def test_anonymous_submissions_live_in_own_tenant() -> None:
    with AssignmentStore(":memory:") as store:
        store.add_grading_result(_graded_draft(), user_id=None, session_id="s1")

        assert len(store.list_submissions()) == 1
        assert len(store.list_submissions(user_id="u1")) == 0
