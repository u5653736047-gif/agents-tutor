"""SQLite 持久化的学习记录存储。"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from types import TracebackType
from typing import Self, cast

from .models import LearningRecord


class LearningRecordStore:
    """按 user_id + topic 保存学习记录，供助学与评价 Agent 读取。"""

    def __init__(self, path: str | Path) -> None:
        database_path = Path(path)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        # 所有连接访问均由实例锁串行化，因此允许跨线程复用。
        connection = sqlite3.connect(database_path, check_same_thread=False)
        try:
            connection.row_factory = sqlite3.Row
            with connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS learning_records (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_key TEXT NOT NULL,
                        user_id TEXT,
                        session_id TEXT NOT NULL,
                        topic TEXT NOT NULL,
                        mastery INTEGER NOT NULL,
                        note TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL
                    )
                    """
                )
        except BaseException:
            connection.close()
            raise
        self._lock = RLock()
        self._connection = connection

    def add_record(self, record: LearningRecord) -> LearningRecord:
        """保存一条学习记录并返回带时间戳的完整记录。"""
        if not record.session_id.strip():
            raise ValueError("session_id must not be empty")
        stamped = record.model_copy(
            update={"created_at": datetime.now(UTC)},
        )
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO learning_records
                    (user_key, user_id, session_id, topic, mastery, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._user_key(record.user_id),
                    record.user_id,
                    record.session_id,
                    record.topic.strip(),
                    record.mastery,
                    record.note,
                    stamped.created_at.isoformat(),
                ),
            )
        return stamped

    def list_records(
        self,
        user_id: str | None = None,
        *,
        topic: str | None = None,
        limit: int = 50,
    ) -> list[LearningRecord]:
        """按创建时间倒序返回某用户的学习记录，可按主题过滤。"""
        query = """
            SELECT user_id, session_id, topic, mastery, note, created_at
            FROM learning_records
            WHERE user_key = ?
        """
        parameters: list[object] = [self._user_key(user_id)]
        if topic is not None:
            query += " AND topic = ?"
            parameters.append(topic.strip())
        query += " ORDER BY created_at DESC LIMIT ?"
        parameters.append(limit)
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        return [self._record_from_row(row) for row in rows]

    def close(self) -> None:
        """关闭记录数据库连接。"""
        with self._lock:
            self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    @staticmethod
    def _user_key(user_id: str | None) -> str:
        if user_id is None:
            return "none"
        if not user_id.strip():
            raise ValueError("user_id must not be empty")
        return f"value:{len(user_id)}:{user_id}"

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> LearningRecord:
        return LearningRecord(
            user_id=cast(str | None, row["user_id"]),
            session_id=str(row["session_id"]),
            topic=str(row["topic"]),
            mastery=int(row["mastery"]),
            note=str(row["note"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )


__all__ = ["LearningRecordStore"]
