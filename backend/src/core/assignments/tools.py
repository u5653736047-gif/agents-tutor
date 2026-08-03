"""作业批改与学情分析工具集。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field, field_validator

from . import analysis, grading, parsing, templates
from .models import AnswerKey, QuestionAnswer
from .store import AssignmentStore


class _ParseUploadInput(BaseModel):
    """校验工具输入，使错误可在调用前被分类。"""

    path: str = Field(min_length=1, max_length=1024)

    @field_validator("path")
    @classmethod
    def reject_blank_path(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("path must not be empty")
        return value


class _ExtractPdfTextInput(BaseModel):
    """校验工具输入，使错误可在调用前被分类。"""

    path: str = Field(min_length=1, max_length=1024)
    max_pages: int = Field(default=200, ge=1, le=1000)
    max_chars_per_page: int = Field(default=10_000, ge=100, le=100_000)

    @field_validator("path")
    @classmethod
    def reject_blank_path(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("path must not be empty")
        return value


class _GradeSubmissionInput(BaseModel):
    """校验工具输入，使错误可在调用前被分类。"""

    questions: list[QuestionAnswer] = Field(min_length=1)
    answer_key: AnswerKey
    title: str = Field(default="", max_length=200)


class _AnalyzeClassInput(BaseModel):
    """校验工具输入，使错误可在调用前被分类。"""

    class_id: str = Field(min_length=1, max_length=100)
    user_ids: list[str] | None = Field(
        default=None,
        description="聚合范围；None 表示全部提交",
    )
    weak_threshold: float = Field(default=0.6, ge=0, le=1)
    since_days: int | None = Field(default=None, ge=1, le=3650)

    @field_validator("class_id", "user_ids")
    @classmethod
    def reject_blank(cls, value: str | list[str] | None) -> str | list[str] | None:
        if value is None:
            return value
        if isinstance(value, str):
            if not value.strip():
                raise ValueError("class_id must not be empty")
            return value
        for item in value:
            if not item.strip():
                raise ValueError("user_ids 不能包含空白项")
        return value


class _GenerateQuizInput(BaseModel):
    """校验工具输入，使错误可在调用前被分类。"""

    knowledge_points: list[str] = Field(min_length=1, max_length=10)
    question_count: int = Field(default=5, ge=1, le=20)
    difficulty: Literal["easy", "medium", "hard", "mixed"] = "mixed"
    objective_ratio: float = Field(default=0.6, ge=0, le=1)
    total_points: int = Field(default=100, ge=10, le=1000)

    @field_validator("knowledge_points")
    @classmethod
    def reject_blank_points(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("knowledge_points 不能包含空白项")
        return value


class _GenerateLessonMaterialInput(BaseModel):
    """校验工具输入，使错误可在调用前被分类。"""

    topic: str = Field(min_length=1, max_length=100)
    knowledge_points: list[str] = Field(default_factory=list, max_length=10)
    estimated_minutes: int = Field(default=45, ge=5, le=180)
    include_exercises: bool = True

    @field_validator("topic", "knowledge_points")
    @classmethod
    def reject_blank(cls, value: str | list[str]) -> str | list[str]:
        if isinstance(value, str):
            if not value.strip():
                raise ValueError("topic must not be empty")
            return value
        if any(not item.strip() for item in value):
            raise ValueError("knowledge_points 不能包含空白项")
        return value


def create_assignment_tools(
    store: AssignmentStore,
    *,
    user_id: str | None = None,
    session_id: str = "default",
    upload_root: Path | None = None,
) -> tuple[BaseTool, ...]:
    """创建作业批改与学情分析工具集。

    身份与上传目录边界在构建时绑定，模型不传易错的 user/session 参数。
    返回顺序：parse_upload, extract_pdf_text, grade_submission,
    analyze_class_performance, generate_quiz, generate_lesson_material。
    """

    @tool("parse_upload", args_schema=_ParseUploadInput)
    def parse_upload(path: str) -> dict[str, Any]:
        """解析学生作业上传：识别 PDF 文本层并返回页级统计。"""
        return parsing.parse_upload(path, upload_root=upload_root)

    @tool("extract_pdf_text", args_schema=_ExtractPdfTextInput)
    def extract_pdf_text(
        path: str,
        max_pages: int = 200,
        max_chars_per_page: int = 10_000,
    ) -> dict[str, Any]:
        """抽取 PDF 逐页文本，供批改与讲解使用。"""
        return parsing.extract_pdf_text(
            path,
            max_pages=max_pages,
            max_chars_per_page=max_chars_per_page,
        )

    @tool("grade_submission", args_schema=_GradeSubmissionInput)
    def grade_submission(
        questions: list[QuestionAnswer],
        answer_key: AnswerKey,
        title: str = "",
    ) -> dict[str, Any]:
        """批改作业：客观题自动比对打分，主观题给出评分建议，结果落库。"""
        draft = grading.grade_questions(
            questions,
            answer_key,
            title=title,
        )
        result = store.add_grading_result(
            draft,
            user_id=user_id,
            session_id=session_id,
        )
        return {
            "submission_id": result.submission_id,
            "saved": True,
            "title": result.title,
            "total_points": result.total_points,
            "total_score": result.total_score,
            "objective": {
                "correct": result.objective_correct,
                "total": result.objective_total,
            },
            "warnings": result.warnings,
            "created_at": result.created_at.isoformat(),
            "questions": [
                {
                    "question_id": item.question_id,
                    "kind": item.kind,
                    "points": item.points,
                    "score": item.score,
                    "is_correct": item.is_correct,
                    "is_estimated": item.is_estimated,
                    "correct_answer": item.correct_answer,
                    "student_answer": item.student_answer,
                    "knowledge_points": item.knowledge_points,
                    "comment": item.comment,
                }
                for item in result.questions
            ],
        }

    @tool("analyze_class_performance", args_schema=_AnalyzeClassInput)
    def analyze_class_performance(
        class_id: str,
        user_ids: list[str] | None = None,
        weak_threshold: float = 0.6,
        since_days: int | None = None,
    ) -> dict[str, Any]:
        """基于批改数据做学情诊断，输出薄弱点报告。"""
        summary = store.class_summary(user_ids, since_days=since_days)
        aggregates = store.aggregate_accuracy(user_ids, since_days=since_days)
        report = analysis.build_class_report(
            class_id=class_id,
            scope=summary,
            knowledge_points=aggregates,
            weak_threshold=weak_threshold,
        )
        return report.model_dump(mode="json")

    @tool("generate_quiz", args_schema=_GenerateQuizInput)
    def generate_quiz(
        knowledge_points: list[str],
        question_count: int = 5,
        difficulty: Literal["easy", "medium", "hard", "mixed"] = "mixed",
        objective_ratio: float = 0.6,
        total_points: int = 100,
    ) -> dict[str, Any]:
        """生成测验骨架：按知识点/难度/分值分配题目结构，内容待填充。"""
        skeleton, warnings = templates.build_quiz_skeleton(
            knowledge_points=knowledge_points,
            question_count=question_count,
            difficulty=difficulty,
            objective_ratio=objective_ratio,
            total_points=total_points,
        )
        return {"warnings": warnings, "quiz": skeleton.model_dump(mode="json")}

    @tool("generate_lesson_material", args_schema=_GenerateLessonMaterialInput)
    def generate_lesson_material(
        topic: str,
        knowledge_points: list[str] | None = None,
        estimated_minutes: int = 45,
        include_exercises: bool = True,
    ) -> dict[str, Any]:
        """生成教案素材骨架：教学目标、章节结构与内容占位。"""
        skeleton = templates.build_lesson_material_skeleton(
            topic=topic,
            knowledge_points=knowledge_points or [],
            estimated_minutes=estimated_minutes,
            include_exercises=include_exercises,
        )
        return skeleton.model_dump(mode="json")

    return (
        parse_upload,
        extract_pdf_text,
        grade_submission,
        analyze_class_performance,
        generate_quiz,
        generate_lesson_material,
    )


__all__ = ["create_assignment_tools"]
