"""SQLite storage for conversation metadata."""

# 会话元数据与图 checkpoint 是「两本账」：这里只记录会话的 id/用户/
# 创建时间/归档状态，对话内容与图状态都在 checkpoint 里（两者通过
# session_id 关联，归档只隐藏列表、不删任何数据）。

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from types import TracebackType
from typing import Self, cast


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """Immutable metadata for one user-owned conversation."""

    session_id: str
    user_id: str | None
    created_at: datetime
    archived: bool = False


class SessionStore:
    """Persist session metadata separately from graph checkpoints."""

    def __init__(self, path: str | Path) -> None:
        database_path = Path(path)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        # 所有连接访问均由实例锁串行化，因此允许跨线程复用。
        connection = sqlite3.connect(database_path, check_same_thread=False)
        try:
            connection.row_factory = sqlite3.Row  # 查询结果按列名访问
            with connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sessions (
                        user_key TEXT NOT NULL,
                        user_id TEXT,
                        session_id TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        archived INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (user_key, session_id)
                    )
                    """
                )
        except BaseException:
            connection.close()
            raise
        self._lock = RLock()
        self._connection = connection

    def create_session(
        self,
        session_id: str,
        user_id: str | None = None,
    ) -> SessionRecord:
        """Create and return a session metadata record."""
        if not session_id.strip():
            raise ValueError("session_id must not be empty")
        record = SessionRecord(
            session_id=session_id,
            user_id=user_id,
            created_at=datetime.now(UTC),
        )
        try:
            with self._lock, self._connection:
                self._connection.execute(
                    """
                    INSERT INTO sessions (user_key, user_id, session_id, created_at, archived)
                    VALUES (?, ?, ?, ?, 0)
                    """,
                    (
                        self._user_key(user_id),
                        user_id,
                        session_id,
                        record.created_at.isoformat(),
                    ),
                )
        # 主键冲突 = 同用户重复创建同一会话，转成业务错误抛出
        except sqlite3.IntegrityError as exc:
            if exc.sqlite_errorcode not in (
                sqlite3.SQLITE_CONSTRAINT_PRIMARYKEY,
                sqlite3.SQLITE_CONSTRAINT_UNIQUE,
            ):
                raise
            raise ValueError(
                f"session already exists for user: {session_id}"
            ) from exc
        return record

    def list_sessions(
        self,
        user_id: str | None = None,
        *,
        include_archived: bool = False,
    ) -> list[SessionRecord]:
        """List one user's sessions in creation order."""
        query = """
            SELECT session_id, user_id, created_at, archived
            FROM sessions
            WHERE user_key = ?
        """
        parameters: list[object] = [self._user_key(user_id)]
        # 默认只列未归档会话：归档只影响列表可见性，数据不删除
        if not include_archived:
            query += " AND archived = 0"
        query += " ORDER BY created_at, session_id"
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        return [self._record_from_row(row) for row in rows]

    def archive_session(
        self,
        session_id: str,
        user_id: str | None = None,
    ) -> bool:
        """Archive an active session and report whether a row changed."""
        with self._lock:
            # 归档 = 打标记不删数据：只置 archived=1，checkpoint 内容保持不动
            with self._connection:
                cursor = self._connection.execute(
                    """
                    UPDATE sessions
                    SET archived = 1
                    WHERE user_key = ? AND session_id = ? AND archived = 0
                    """,
                    (self._user_key(user_id), session_id),
                )
            return cursor.rowcount > 0

    def close(self) -> None:
        """Close the metadata database connection."""
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

    # 用户键规范化：None 统一归一到 "none"，真实 id 加长度前缀，
    # 保证键唯一无歧义（如 id 恰好为 "none" 也不会与 None 混淆）
    @staticmethod
    def _user_key(user_id: str | None) -> str:
        if user_id is None:
            return "none"
        if not user_id.strip():
            raise ValueError("user_id must not be empty")
        return f"value:{len(user_id)}:{user_id}"

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(
            session_id=str(row["session_id"]),
            user_id=cast(str | None, row["user_id"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            archived=bool(row["archived"]),
        )


__all__ = ["SessionRecord", "SessionStore"]
