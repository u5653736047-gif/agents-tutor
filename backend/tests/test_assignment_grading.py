"""作业批改纯函数测试。"""

from __future__ import annotations

from core.assignments.grading import grade_questions, normalize_answer, suggest_subjective
from core.assignments.models import AnswerKey, AnswerKeyItem, QuestionAnswer


def _answer_key() -> AnswerKey:
    return AnswerKey(
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
    )


def test_normalize_answer_collapses_whitespace_and_case() -> None:
    assert normalize_answer("  B \n B ") == "b b"
    assert normalize_answer("Ｂ") == "b"  # 全角 B


def test_objective_match_is_insensitive_to_case_whitespace_and_fullwidth() -> None:
    draft = grade_questions(
        [
            QuestionAnswer(question_id="q1", answer=" Ｂ "),  # 全角 B + 空格
            QuestionAnswer(question_id="q2", answer="0.5"),
        ],
        _answer_key(),
    )

    assert draft.objective_correct == 2
    assert draft.objective_total == 2
    assert draft.questions[0].score == 10
    assert draft.questions[0].is_correct is True
    assert draft.questions[1].is_correct is True


def test_wrong_objective_answer_scores_zero() -> None:
    draft = grade_questions(
        [QuestionAnswer(question_id="q1", answer="C")],
        _answer_key(),
    )

    assert draft.questions[0].score == 0
    assert draft.questions[0].is_correct is False


def test_missing_answers_score_zero_with_warning() -> None:
    draft = grade_questions(
        [QuestionAnswer(question_id="q1", answer="B")],
        _answer_key(),
    )

    assert draft.questions[1].score == 0
    assert draft.questions[1].comment == "未作答"
    assert any("q2, q3" in warning for warning in draft.warnings)


def test_extra_answers_ignored_with_warning() -> None:
    draft = grade_questions(
        [
            QuestionAnswer(question_id="q1", answer="B"),
            QuestionAnswer(question_id="q99", answer="hack"),
        ],
        _answer_key(),
    )

    assert any("q99" in warning for warning in draft.warnings)
    assert draft.total_score == draft.questions[0].score


def test_subjective_suggestion_reflects_keyword_hits() -> None:
    suggestion = suggest_subjective(
        "梯度下降在损失函数收敛性上依赖学习率的选择，学习率过大不收敛。",
        points=20,
        keyword_hints=["收敛", "学习率"],
    )

    assert suggestion.keyword_hits == 2
    assert suggestion.keyword_total == 2
    assert suggestion.suggested_range[0] >= 10  # 满分 20 的 50%+
    assert "收敛" in suggestion.rationale


def test_subjective_question_is_estimated_with_guidance_comment() -> None:
    draft = grade_questions(
        [QuestionAnswer(question_id="q3", answer="需要合理选择学习率保证收敛。")],
        _answer_key(),
    )

    question = draft.questions[2]
    assert question.is_correct is None
    assert question.is_estimated is True
    assert 0 < question.score <= 20  # 落库使用建议区间下限（保守估计）
    assert "评分建议" in question.comment


def test_total_points_and_score_are_summed() -> None:
    draft = grade_questions(
        [
            QuestionAnswer(question_id="q1", answer="B"),
            QuestionAnswer(question_id="q2", answer="0.5"),
            QuestionAnswer(question_id="q3", answer="学习率决定收敛速度，需要仔细调参。"),
        ],
        _answer_key(),
    )

    assert draft.total_points == 40
    assert draft.total_score == 20 + draft.questions[2].score
