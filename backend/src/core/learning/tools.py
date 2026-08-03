"""把学习记录存储封装为 Agent 可调用的工具。"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field, field_validator

from .models import LearningRecord
from .store import LearningRecordStore


class _SaveRecordInput(BaseModel):
    """校验工具输入，使错误可在调用前被分类。"""

    topic: str = Field(min_length=1, max_length=200)
    mastery: int = Field(ge=0, le=5)
    note: str = Field(default="", max_length=2000)

    @field_validator("topic")
    @classmethod
    def reject_blank_topic(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("topic must not be empty")
        return value


class _QueryRecordsInput(BaseModel):
    """校验工具输入，使错误可在调用前被分类。"""

    topic: str | None = Field(default=None, max_length=200)
    limit: int = Field(default=10, ge=1, le=100)

    @field_validator("topic")
    @classmethod
    def reject_blank_topic(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("topic must not be empty")
        return value


def create_learning_tools(
    store: LearningRecordStore,
    *,
    user_id: str | None = None,
    session_id: str = "default",
) -> tuple[BaseTool, BaseTool]:
    """创建绑定用户与会话的学习记录读写工具。

    身份由调用方在图构建时绑定，避免模型自行传递易错的 user/session 参数。
    """

    @tool("save_learning_record", args_schema=_SaveRecordInput)
    def save_learning_record(
        topic: str,
        mastery: int,
        note: str = "",
    ) -> dict[str, Any]:
        """记录学生对某个知识点的掌握程度，供后续助学与评价使用。"""
        record = store.add_record(
            LearningRecord(
                user_id=user_id,
                session_id=session_id,
                topic=topic,
                mastery=mastery,
                note=note,
            )
        )
        return {
            "saved": True,
            "topic": record.topic,
            "mastery": record.mastery,
            "created_at": record.created_at.isoformat(),
        }

    @tool("query_learning_records", args_schema=_QueryRecordsInput)
    def query_learning_records(
        topic: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """查询当前用户的历史学习记录，按时间倒序。"""
        records = store.list_records(user_id=user_id, topic=topic, limit=limit)
        return {
            "found": bool(records),
            "records": [
                {
                    "topic": record.topic,
                    "mastery": record.mastery,
                    "note": record.note,
                    "created_at": record.created_at.isoformat(),
                }
                for record in records
            ],
        }

    return save_learning_record, query_learning_records


__all__ = ["create_learning_tools"]
