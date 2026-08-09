"""SQLite storage for conversation metadata."""

# 会话元数据与图 checkpoint 是「两本账」：这里只记录会话的 id/用户/
# 创建时间/归档状态/列表标题，对话内容与图状态都在 checkpoint 里（两者通过
# session_id 关联，归档只隐藏列表、不删任何数据）。

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
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
    updated_at: datetime
    archived: bool = False
    # 侧栏展示标题（首条用户消息提炼，只写一次）；老数据为 None，
    # 前端回退显示 session_id。
    title: str | None = None
    # 主工作区与额外授权目录均保存规范化绝对路径。None 只用于兼容
    # 手工构造的旧 SessionRecord；SessionStore 返回的记录始终有主目录。
    workspace_root: str | None = None
    additional_workspace_roots: tuple[str, ...] = ()


class SessionStore:
    """Persist session metadata separately from graph checkpoints."""

    def __init__(
        self,
        path: str | Path,
        *,
        default_workspace_root: str | Path | None = None,
        allowed_workspace_roots: Sequence[str | Path] | None = None,
    ) -> None:
        self._default_workspace_root = self._canonical_directory(
            Path.cwd() if default_workspace_root is None else default_workspace_root
        )
        self._allowed_workspace_roots = (
            None
            if allowed_workspace_roots is None
            else tuple(
                self._canonical_directory(root) for root in allowed_workspace_roots
            )
        )
        self._assert_workspace_allowed(self._default_workspace_root)
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
                        updated_at TEXT NOT NULL,
                        archived INTEGER NOT NULL DEFAULT 0,
                        title TEXT,
                        workspace_root TEXT NOT NULL,
                        additional_workspace_roots TEXT NOT NULL DEFAULT '[]',
                        PRIMARY KEY (user_key, session_id)
                    )
                    """
                )
                # 增量迁移：老库（title 列加入前建的表）走 CREATE TABLE IF NOT
                # EXISTS 是空操作，必须单独 ALTER 补列；新库已含该列，跳过。
                columns = {
                    str(row["name"])
                    for row in connection.execute("PRAGMA table_info(sessions)")
                }
                if "title" not in columns:
                    connection.execute("ALTER TABLE sessions ADD COLUMN title TEXT")
                if "updated_at" not in columns:
                    connection.execute("ALTER TABLE sessions ADD COLUMN updated_at TEXT")
                    connection.execute(
                        "UPDATE sessions SET updated_at = created_at WHERE updated_at IS NULL"
                    )
                if "workspace_root" not in columns:
                    connection.execute("ALTER TABLE sessions ADD COLUMN workspace_root TEXT")
                if "additional_workspace_roots" not in columns:
                    connection.execute(
                        "ALTER TABLE sessions ADD COLUMN additional_workspace_roots "
                        "TEXT NOT NULL DEFAULT '[]'"
                    )
                connection.execute(
                    "UPDATE sessions SET workspace_root = ? "
                    "WHERE workspace_root IS NULL OR TRIM(workspace_root) = ''",
                    (str(self._default_workspace_root),),
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
        *,
        workspace_root: str | Path | None = None,
    ) -> SessionRecord:
        """Create and return a session metadata record."""
        if not session_id.strip():
            raise ValueError("session_id must not be empty")
        resolved_workspace = self.resolve_workspace_root(workspace_root)
        now = datetime.now(UTC)
        record = SessionRecord(
            session_id=session_id,
            user_id=user_id,
            created_at=now,
            updated_at=now,
            workspace_root=resolved_workspace,
        )
        try:
            with self._lock, self._connection:
                self._connection.execute(
                    """
                    INSERT INTO sessions (
                        user_key, user_id, session_id, created_at, updated_at, archived,
                        workspace_root, additional_workspace_roots
                    )
                    VALUES (?, ?, ?, ?, ?, 0, ?, '[]')
                    """,
                    (
                        self._user_key(user_id),
                        user_id,
                        session_id,
                        record.created_at.isoformat(),
                        record.updated_at.isoformat(),
                        resolved_workspace,
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
        """List one user's sessions by most recent conversation activity."""
        query = """
            SELECT session_id, user_id, created_at, updated_at, archived, title,
                   workspace_root, additional_workspace_roots
            FROM sessions
            WHERE user_key = ?
        """
        parameters: list[object] = [self._user_key(user_id)]
        # 默认只列未归档会话：归档只影响列表可见性，数据不删除
        if not include_archived:
            query += " AND archived = 0"
        query += " ORDER BY updated_at DESC, created_at DESC, session_id DESC"
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        return [self._record_from_row(row) for row in rows]

    def get_session(
        self,
        session_id: str,
        user_id: str | None = None,
    ) -> SessionRecord | None:
        """Return one owned session without scanning the user's full history."""
        with self._lock:
            row = self._connection.execute(
                """
                SELECT session_id, user_id, created_at, updated_at, archived, title,
                       workspace_root, additional_workspace_roots
                FROM sessions
                WHERE user_key = ? AND session_id = ?
                """,
                (self._user_key(user_id), session_id),
            ).fetchone()
        return None if row is None else self._record_from_row(row)

    def add_workspace_root(
        self,
        session_id: str,
        path: str | Path,
        user_id: str | None = None,
    ) -> SessionRecord | None:
        """Authorize one additional directory for an existing owned session."""
        resolved = self.resolve_workspace_root(path)
        with self._lock, self._connection:
            row = self._connection.execute(
                """
                SELECT session_id, user_id, created_at, updated_at, archived, title,
                       workspace_root, additional_workspace_roots
                FROM sessions
                WHERE user_key = ? AND session_id = ?
                """,
                (self._user_key(user_id), session_id),
            ).fetchone()
            if row is None:
                return None
            record = self._record_from_row(row)
            roots = list(record.additional_workspace_roots)
            if resolved != record.workspace_root and resolved not in roots:
                roots.append(resolved)
                self._connection.execute(
                    """
                    UPDATE sessions
                    SET additional_workspace_roots = ?, updated_at = ?
                    WHERE user_key = ? AND session_id = ?
                    """,
                    (
                        json.dumps(roots, ensure_ascii=False),
                        datetime.now(UTC).isoformat(),
                        self._user_key(user_id),
                        session_id,
                    ),
                )
        return self.get_session(session_id, user_id=user_id)

    @property
    def default_workspace_root(self) -> str:
        return str(self._default_workspace_root)

    def resolve_workspace_root(self, path: str | Path | None = None) -> str:
        """Canonicalize and authorize a user-selected workspace directory."""
        candidate = (
            self._default_workspace_root
            if path is None
            else self._canonical_directory(path)
        )
        self._assert_workspace_allowed(candidate)
        return str(candidate)

    def list_workspace_directories(
        self,
        path: str | Path | None = None,
        *,
        max_results: int = 200,
    ) -> dict[str, object]:
        """List child directories for the local workspace selection dialog."""
        if not 1 <= max_results <= 500:
            raise ValueError("max_results must be between 1 and 500")
        current = Path(self.resolve_workspace_root(path))
        parent: str | None = None
        if current.parent != current:
            try:
                parent = self.resolve_workspace_root(current.parent)
            except ValueError:
                parent = None

        directories: list[dict[str, str]] = []
        try:
            children = sorted(current.iterdir(), key=lambda item: item.name.casefold())
        except OSError as error:
            raise ValueError("workspace directory cannot be browsed") from error
        for child in children:
            if len(directories) >= max_results:
                break
            try:
                resolved = self.resolve_workspace_root(child)
            except ValueError:
                continue
            directories.append({"name": child.name, "path": resolved})
        return {
            "path": str(current),
            "parent": parent,
            "directories": directories,
        }

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

    def touch_session(
        self,
        session_id: str,
        user_id: str | None = None,
    ) -> bool:
        """Record fresh conversation activity and report whether a row changed."""
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE sessions
                SET updated_at = ?
                WHERE user_key = ? AND session_id = ?
                """,
                (datetime.now(UTC).isoformat(), self._user_key(user_id), session_id),
            )
            return cursor.rowcount > 0

    def set_title_if_absent(
        self,
        session_id: str,
        title: str,
        user_id: str | None = None,
    ) -> bool:
        """Set the sidebar title once; an existing title is never overwritten."""
        if not title.strip():
            raise ValueError("title must not be empty")
        with self._lock, self._connection:
            # 只写一次(WHERE title IS NULL):后续消息不得覆盖首条消息
            # 提炼的标题;对不存在的会话静默 0 行(与归档同款语义)。
            cursor = self._connection.execute(
                """
                UPDATE sessions
                SET title = ?
                WHERE user_key = ? AND session_id = ? AND title IS NULL
                """,
                (title, self._user_key(user_id), session_id),
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
        raw_additional = row["additional_workspace_roots"]
        try:
            parsed_additional = json.loads(str(raw_additional or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed_additional = []
        additional = tuple(
            value for value in parsed_additional if isinstance(value, str) and value
        )
        return SessionRecord(
            session_id=str(row["session_id"]),
            user_id=cast(str | None, row["user_id"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
            archived=bool(row["archived"]),
            title=cast(str | None, row["title"]),
            workspace_root=cast(str | None, row["workspace_root"]),
            additional_workspace_roots=additional,
        )

    @staticmethod
    def _canonical_directory(path: str | Path) -> Path:
        candidate = Path(path).expanduser()
        try:
            resolved = candidate.resolve(strict=True)
        except (FileNotFoundError, OSError) as error:
            raise ValueError("workspace root must be an existing directory") from error
        if not resolved.is_dir():
            raise ValueError("workspace root must be an existing directory")
        return resolved

    def _assert_workspace_allowed(self, candidate: Path) -> None:
        if self._allowed_workspace_roots is None:
            return
        if any(
            candidate == allowed or candidate.is_relative_to(allowed)
            for allowed in self._allowed_workspace_roots
        ):
            return
        raise ValueError("workspace root is not allowed by server policy")


SESSION_TITLE_MAX_LENGTH = 30


def derive_session_title(message: str) -> str | None:
    """从首条用户消息提炼侧栏标题：压缩空白、截断 30 字。

    换行/缩进等连续空白归一为单个空格（侧栏单行展示）；全空白返回
    None（调用方不写标题，理论上来不了——ChatRequest 已拒绝空白消息，
    这里做防御）。超长截断并加省略号。
    """
    normalized = " ".join(message.split())
    if not normalized:
        return None
    if len(normalized) <= SESSION_TITLE_MAX_LENGTH:
        return normalized
    return normalized[:SESSION_TITLE_MAX_LENGTH].rstrip() + "…"


__all__ = ["SessionRecord", "SessionStore", "derive_session_title"]
