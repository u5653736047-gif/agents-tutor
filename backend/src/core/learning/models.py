"""学习记录领域模型。"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field, field_validator


class LearningRecord(BaseModel):
    """学生对单个知识点的学习记录。"""

    user_id: str | None = None
    session_id: str
    topic: str = Field(min_length=1, max_length=200)
    mastery: int = Field(
        ge=0,
        le=5,
        description="掌握程度，0（未掌握）~ 5（精通）",
    )
    note: str = Field(default="", max_length=2000)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("topic")
    @classmethod
    def reject_blank_topic(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("topic must not be empty")
        return value


__all__ = ["LearningRecord"]
