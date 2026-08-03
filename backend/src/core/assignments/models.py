"""作业批改与学情分析的领域模型。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

# ─────────────────────────────────────────────
# answer_key 与作答
# ─────────────────────────────────────────────

QuestionKind = Literal["objective", "subjective"]


class AnswerKeyItem(BaseModel):
    """answer_key 中单道题的标准答案条目。"""

    question_id: str = Field(min_length=1)
    kind: QuestionKind
    correct_answer: str | None = Field(
        default=None,
        description="客观题标准答案；主观题置 None",
    )
    keyword_hints: list[str] = Field(
        default_factory=list,
        description="主观题评分要点关键词",
    )
    points: float = Field(gt=0)
    knowledge_points: list[str] = Field(
        default_factory=list,
        description="学情聚合维度",
    )

    @model_validator(mode="after")
    def _objective_requires_answer(self) -> AnswerKeyItem:
        if self.kind == "objective" and not (self.correct_answer or "").strip():
            raise ValueError("客观题必须提供 correct_answer")
        return self


class AnswerKey(BaseModel):
    """批改标准答案集合。"""

    version: str = "1"
    items: list[AnswerKeyItem] = Field(min_length=1)

    @model_validator(mode="after")
    def _reject_duplicate_question_ids(self) -> AnswerKey:
        ids = [item.question_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("answer_key 中存在重复的 question_id")
        return self


class QuestionAnswer(BaseModel):
    """学生对单道题的作答。"""

    question_id: str = Field(min_length=1)
    answer: str | None = Field(default=None, description="None/空白 = 未作答")


# ─────────────────────────────────────────────
# 批改结果
# ─────────────────────────────────────────────


class SubjectiveSuggestion(BaseModel):
    """主观题的确定性评分建议。"""

    keyword_hits: int
    keyword_total: int
    length: int
    suggested_range: tuple[float, float] = Field(description="(保守下限, 上限)")
    rationale: str


class GradedQuestion(BaseModel):
    """单道题的批改结果。"""

    question_id: str
    kind: QuestionKind
    knowledge_points: list[str]
    points: float
    score: float = Field(ge=0)
    is_correct: bool | None = Field(default=None, description="主观题为 None")
    is_estimated: bool = Field(default=False, description="主观题分数为启发式估计")
    correct_answer: str | None = None
    student_answer: str
    comment: str = ""


class GradingDraft(BaseModel):
    """一次批改的纯函数输出（无身份与时间戳）。"""

    title: str = ""
    total_points: float = Field(gt=0)
    total_score: float = Field(ge=0)
    objective_correct: int = Field(ge=0)
    objective_total: int = Field(ge=0)
    questions: list[GradedQuestion]
    warnings: list[str] = Field(default_factory=list)


class GradingResult(GradingDraft):
    """落库后的完整批改记录。"""

    submission_id: str
    user_id: str | None = None
    session_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ─────────────────────────────────────────────
# 学情报告
# ─────────────────────────────────────────────


class KnowledgePointAggregate(BaseModel):
    """单个知识点的客观题准确率聚合。"""

    knowledge_point: str
    total_questions: int = Field(ge=0)
    correct: int = Field(ge=0)
    accuracy: float = Field(ge=0, le=1)
    is_weak: bool = Field(default=False, description="由学情报告按阈值标记")


class ClassSummary(BaseModel):
    """班级范围统计。"""

    submission_count: int = Field(default=0, ge=0)
    student_count: int = Field(default=0, ge=0)
    average_score: float = Field(default=0.0, ge=0)
    max_score: float = Field(default=0.0, ge=0)
    min_score: float = Field(default=0.0, ge=0)


class WeakPoint(BaseModel):
    """低于阈值的薄弱知识点。"""

    knowledge_point: str
    total_questions: int = Field(ge=0)
    correct: int = Field(ge=0)
    accuracy: float = Field(ge=0, le=1)


class ClassPerformanceReport(BaseModel):
    """学情诊断报告。"""

    class_id: str
    scope: ClassSummary
    knowledge_points: list[KnowledgePointAggregate]
    weak_points: list[WeakPoint]
    weak_threshold: float = Field(ge=0, le=1)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ─────────────────────────────────────────────
# 备课素材骨架
# ─────────────────────────────────────────────


class QuizQuestionSkeleton(BaseModel):
    """单道测验题的结构化骨架。"""

    question_id: str
    kind: QuestionKind
    difficulty: Literal["easy", "medium", "hard"]
    knowledge_point: str
    points: float
    stem_placeholder: str
    options_placeholder: list[str] | None = None
    answer_placeholder: str


class QuizSkeleton(BaseModel):
    """整份测验的结构化骨架。"""

    title_placeholder: str
    knowledge_points: list[str]
    total_points: float
    questions: list[QuizQuestionSkeleton] = Field(min_length=1)


class LessonSection(BaseModel):
    """教案单个章节的骨架。"""

    section_id: str
    section_type: Literal["warmup", "knowledge_points", "examples", "exercises", "summary"]
    title_placeholder: str
    content_placeholder: str


class LessonMaterialSkeleton(BaseModel):
    """一份教案素材的结构化骨架。"""

    topic: str
    estimated_minutes: int = Field(ge=1)
    objectives: list[str]
    knowledge_points: list[str]
    sections: list[LessonSection]


# ─────────────────────────────────────────────
# PDF 检视
# ─────────────────────────────────────────────


class PageText(BaseModel):
    """单页文本统计。"""

    page: int = Field(ge=1)
    text: str | None = None
    char_count: int = Field(ge=0)


class PdfInspection(BaseModel):
    """PDF 文本层检视结果。"""

    pdf_type: Literal["text_based", "mixed", "scanned", "image_based"]
    page_count: int = Field(ge=0)
    text_pages: int = Field(ge=0)
    blank_pages: int = Field(ge=0)
    image_pages: int = Field(ge=0)
    pages: list[PageText]


__all__ = [
    "AnswerKey",
    "AnswerKeyItem",
    "ClassPerformanceReport",
    "ClassSummary",
    "GradedQuestion",
    "GradingDraft",
    "GradingResult",
    "KnowledgePointAggregate",
    "LessonMaterialSkeleton",
    "LessonSection",
    "PageText",
    "PdfInspection",
    "QuestionAnswer",
    "QuestionKind",
    "QuizQuestionSkeleton",
    "QuizSkeleton",
    "SubjectiveSuggestion",
    "WeakPoint",
]
