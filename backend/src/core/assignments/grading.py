"""作业批改纯函数：答案归一化比对与主观题评分建议。"""

from __future__ import annotations

import unicodedata

from .models import (
    AnswerKey,
    GradedQuestion,
    GradingDraft,
    QuestionAnswer,
    SubjectiveSuggestion,
)


def normalize_answer(text: str | None) -> str:
    """归一化答案：NFKC（全角→半角）→ casefold → 折叠空白。"""
    if text is None:
        return ""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def grade_questions(
    answers: list[QuestionAnswer],
    answer_key: AnswerKey,
    *,
    title: str = "",
) -> GradingDraft:
    """按 answer_key 批改作答，返回题目级结果与告警。

    宽容策略：answer_key 中无对应作答的题按 0 分计；作答中多余的题目忽略，
    两者都写入 warnings 供模型观察。
    """
    answer_by_id = {
        item.question_id: item.answer or "" for item in answers
    }
    key_ids = [item.question_id for item in answer_key.items]
    warnings: list[str] = []

    missing = [qid for qid in key_ids if qid not in answer_by_id]
    if missing:
        warnings.append(
            f"answer_key 中题目 {', '.join(missing)} 没有对应作答，按未作答计 0 分"
        )
    extra = [qid for qid in answer_by_id if qid not in set(key_ids)]
    if extra:
        warnings.append(f"作答中包含 answer_key 之外的题目，已忽略：{', '.join(extra)}")

    graded: list[GradedQuestion] = []
    total_points = 0.0
    objective_correct = 0
    objective_total = 0

    for item in answer_key.items:
        total_points += item.points
        student_answer = answer_by_id.get(item.question_id, "")
        if not student_answer.strip():
            graded.append(
                GradedQuestion(
                    question_id=item.question_id,
                    kind=item.kind,
                    knowledge_points=list(item.knowledge_points),
                    points=item.points,
                    score=0.0,
                    is_correct=False if item.kind == "objective" else None,
                    is_estimated=item.kind == "subjective",
                    correct_answer=item.correct_answer,
                    student_answer="",
                    comment="未作答",
                )
            )
            if item.kind == "objective":
                objective_total += 1
            continue

        if item.kind == "objective":
            objective_total += 1
            correct = (
                normalize_answer(student_answer) == normalize_answer(item.correct_answer)
            )
            score = item.points if correct else 0.0
            if correct:
                objective_correct += 1
            graded.append(
                GradedQuestion(
                    question_id=item.question_id,
                    kind=item.kind,
                    knowledge_points=list(item.knowledge_points),
                    points=item.points,
                    score=score,
                    is_correct=correct,
                    is_estimated=False,
                    correct_answer=item.correct_answer,
                    student_answer=student_answer,
                    comment="",
                )
            )
        else:
            suggestion = suggest_subjective(
                student_answer,
                points=item.points,
                keyword_hints=list(item.keyword_hints),
            )
            graded.append(
                GradedQuestion(
                    question_id=item.question_id,
                    kind=item.kind,
                    knowledge_points=list(item.knowledge_points),
                    points=item.points,
                    score=suggestion.suggested_range[0],
                    is_correct=None,
                    is_estimated=True,
                    correct_answer=None,
                    student_answer=student_answer,
                    comment=(
                        f"评分建议：{suggestion.rationale}"
                        "；请结合作答要点给出定性评语与最终得分建议"
                    ),
                )
            )

    return GradingDraft(
        title=title,
        total_points=total_points,
        total_score=sum(item.score for item in graded),
        objective_correct=objective_correct,
        objective_total=objective_total,
        questions=graded,
        warnings=warnings,
    )


def suggest_subjective(
    answer: str,
    *,
    points: float,
    keyword_hints: list[str],
) -> SubjectiveSuggestion:
    """用确定性启发式给出主观题评分建议区间。

    floor = points * (0.15 + 0.55 * 命中率)，长度 ≥30 字加 0.15*points，
    <10 字减 0.05*points，均钳制到 [0, points]。
    """
    normalized = normalize_answer(answer)
    hits = sum(1 for hint in keyword_hints if normalize_answer(hint) in normalized)
    hit_ratio = hits / max(1, len(keyword_hints))

    floor = points * (0.15 + 0.55 * hit_ratio)
    if len(normalized) >= 30:
        floor += 0.15 * points
    elif len(normalized) < 10:
        floor -= 0.05 * points
    floor = min(max(floor, 0.0), points)

    ceiling = min(points, floor + 0.3 * points)
    rationale = (
        f"作答 {len(normalized)} 字，命中 {hits}/{len(keyword_hints)} 个要点关键词"
        f"（{('、'.join(keyword_hints)) if keyword_hints else '无关键词'}），"
        f"建议得分区间 {_round2(floor)}-{_round2(ceiling)} 分"
    )
    return SubjectiveSuggestion(
        keyword_hits=hits,
        keyword_total=len(keyword_hints),
        length=len(normalized),
        suggested_range=(_round2(floor), _round2(ceiling)),
        rationale=rationale,
    )


def _round2(value: float) -> float:
    return round(value, 2)


__all__ = ["grade_questions", "normalize_answer", "suggest_subjective"]
