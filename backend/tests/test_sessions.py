"""SQLite session metadata store tests."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from core.sessions import SessionRecord, SessionStore, derive_session_title


def test_session_store_creates_and_lists_immutable_records(tmp_path: Path) -> None:
    with SessionStore(tmp_path / "metadata" / "sessions.sqlite") as store:
        first = store.create_session("session-1", user_id="user-1")
        second = store.create_session("session-2", user_id="user-1")

        assert store.list_sessions(user_id="user-1") == [second, first]

    assert first.session_id == "session-1"
    assert first.user_id == "user-1"
    assert first.updated_at == first.created_at
    assert first.archived is False
    with pytest.raises(FrozenInstanceError):
        first.archived = True  # type: ignore[misc]


def test_session_store_orders_by_recent_activity_and_can_touch_a_session(
    tmp_path: Path,
) -> None:
    with SessionStore(tmp_path / "sessions.sqlite") as store:
        first = store.create_session("first", user_id="user-1")
        store.create_session("second", user_id="user-1")

        assert [record.session_id for record in store.list_sessions(user_id="user-1")] == [
            "second",
            "first",
        ]

        touch_session = getattr(store, "touch_session", None)
        assert callable(touch_session)
        assert touch_session("first", user_id="user-1") is True

        records = store.list_sessions(user_id="user-1")
        assert [record.session_id for record in records] == ["first", "second"]
        assert records[0].updated_at > first.updated_at


def test_session_store_archives_without_deleting_metadata(tmp_path: Path) -> None:
    with SessionStore(tmp_path / "sessions.sqlite") as store:
        active = store.create_session("active", user_id="user-1")
        store.create_session("archived", user_id="user-1")

        assert store.archive_session("archived", user_id="user-1") is True
        assert store.archive_session("archived", user_id="user-1") is False
        assert store.archive_session("missing", user_id="user-1") is False
        assert store.list_sessions(user_id="user-1") == [active]

        all_sessions = store.list_sessions(user_id="user-1", include_archived=True)

    assert [record.session_id for record in all_sessions] == ["archived", "active"]
    assert [record.archived for record in all_sessions] == [True, False]


@pytest.mark.parametrize("session_id", ["", "   "])
def test_session_store_rejects_empty_session_ids(
    tmp_path: Path,
    session_id: str,
) -> None:
    with (
        SessionStore(tmp_path / "sessions.sqlite") as store,
        pytest.raises(ValueError, match="session_id"),
    ):
        store.create_session(session_id, user_id="user-1")


def test_session_store_rejects_duplicate_user_session_key(tmp_path: Path) -> None:
    with SessionStore(tmp_path / "sessions.sqlite") as store:
        store.create_session("session-1", user_id="user-1")

        with pytest.raises(ValueError, match="already exists"):
            store.create_session("session-1", user_id="user-1")


def test_session_store_supports_one_instance_across_threads(tmp_path: Path) -> None:
    with SessionStore(tmp_path / "sessions.sqlite") as store:
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [
                executor.submit(
                    store.create_session,
                    f"session-{index}",
                    "user-1",
                )
                for index in range(8)
            ]
            records = [future.result() for future in futures]

        assert store.list_sessions(user_id="user-1") == sorted(
            records,
            key=lambda record: (record.updated_at, record.created_at, record.session_id),
            reverse=True,
        )


def test_create_propagates_non_unique_integrity_error(tmp_path: Path) -> None:
    database_path = tmp_path / "sessions.sqlite"
    with SessionStore(database_path) as store:
        with sqlite3.connect(database_path) as setup:
            setup.execute(
                """
                CREATE TRIGGER reject_create
                BEFORE INSERT ON sessions
                BEGIN
                    SELECT RAISE(ABORT, 'forced create failure');
                END
                """
            )

        with pytest.raises(sqlite3.IntegrityError, match="forced create failure"):
            store.create_session("blocked", user_id="user-1")

        assert store._connection.in_transaction is False
        with sqlite3.connect(database_path, timeout=0) as independent:
            independent.execute("BEGIN IMMEDIATE")
            independent.rollback()


def test_session_store_isolates_users_and_persists_after_reopen(tmp_path: Path) -> None:
    database_path = tmp_path / "sessions.sqlite"
    with SessionStore(database_path) as store:
        first_user = store.create_session("shared-session", user_id="user-1")
        second_user = store.create_session("shared-session", user_id="user-2")
        assert store.list_sessions(user_id="user-1") == [first_user]
        assert store.list_sessions(user_id="user-2") == [second_user]
        assert store.archive_session("shared-session", user_id="user-1") is True

    with SessionStore(database_path) as reopened:
        assert reopened.list_sessions(user_id="user-1") == []
        assert reopened.list_sessions(user_id="user-1", include_archived=True) == [
            SessionRecord(
                session_id=first_user.session_id,
                user_id=first_user.user_id,
                created_at=first_user.created_at,
                updated_at=first_user.updated_at,
                archived=True,
            )
        ]
        assert reopened.list_sessions(user_id="user-2") == [second_user]


@pytest.mark.parametrize("user_id", ["", "   "])
@pytest.mark.parametrize("method_name", ["create_session", "list_sessions", "archive_session"])
def test_session_store_rejects_empty_user_ids(
    tmp_path: Path,
    user_id: str,
    method_name: str,
) -> None:
    with (
        SessionStore(tmp_path / "sessions.sqlite") as store,
        pytest.raises(ValueError, match="user_id"),
    ):
        if method_name == "list_sessions":
            store.list_sessions(user_id=user_id)
        else:
            getattr(store, method_name)("session-1", user_id=user_id)


def test_archive_failure_rolls_back_and_releases_the_write_lock(tmp_path: Path) -> None:
    database_path = tmp_path / "sessions.sqlite"
    with SessionStore(database_path) as store:
        store.create_session("blocked", user_id="user-1")
        with sqlite3.connect(database_path) as setup:
            setup.execute(
                """
                CREATE TRIGGER reject_archive
                BEFORE UPDATE OF archived ON sessions
                WHEN OLD.session_id = 'blocked'
                BEGIN
                    SELECT RAISE(ABORT, 'forced archive failure');
                END
                """
            )

        with pytest.raises(sqlite3.IntegrityError, match="forced archive failure"):
            store.archive_session("blocked", user_id="user-1")

        assert store._connection.in_transaction is False
        with sqlite3.connect(database_path, timeout=0) as independent:
            independent.execute("BEGIN IMMEDIATE")
            independent.rollback()


def test_derive_session_title_normalizes_whitespace_and_truncates() -> None:
    # 换行/缩进等连续空白压缩为单个空格(侧栏单行展示)
    assert derive_session_title("  什么是\n  注意力机制?  ") == "什么是 注意力机制?"
    # 短标题原样返回
    assert derive_session_title("你好") == "你好"
    # 超长按 30 字截断并加省略号
    assert derive_session_title("字" * 40) == "字" * 30 + "…"
    # 全空白返回 None(防御;ChatRequest 已拒绝空白消息)
    assert derive_session_title("  \n\t ") is None


def test_session_store_sets_title_once_and_persists(tmp_path: Path) -> None:
    database_path = tmp_path / "sessions.sqlite"
    with SessionStore(database_path) as store:
        store.create_session("session-1", user_id="user-1")

        assert store.set_title_if_absent("session-1", "首条消息标题", user_id="user-1") is True
        # 只写一次:后续写入返回 False,标题保持首次值
        assert store.set_title_if_absent("session-1", "后续消息", user_id="user-1") is False
        # 不存在的会话静默 0 行(与归档同款语义)
        assert store.set_title_if_absent("missing", "标题", user_id="user-1") is False

    with SessionStore(database_path) as reopened:
        assert [record.title for record in reopened.list_sessions(user_id="user-1")] == [
            "首条消息标题"
        ]


@pytest.mark.parametrize("title", ["", "   "])
def test_session_store_rejects_blank_titles(tmp_path: Path, title: str) -> None:
    with (
        SessionStore(tmp_path / "sessions.sqlite") as store,
        pytest.raises(ValueError, match="title"),
    ):
        store.create_session("session-1", user_id="user-1")
        store.set_title_if_absent("session-1", title, user_id="user-1")


def test_session_store_migrates_legacy_database_without_title_column(tmp_path: Path) -> None:
    database_path = tmp_path / "sessions.sqlite"
    # 模拟 title 列加入前的旧库:老表结构 + 一行存量会话
    with sqlite3.connect(database_path) as legacy:
        legacy.execute(
            """
            CREATE TABLE sessions (
                user_key TEXT NOT NULL,
                user_id TEXT,
                session_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                archived INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_key, session_id)
            )
            """
        )
        legacy.execute(
            """
            INSERT INTO sessions (user_key, user_id, session_id, created_at, archived)
            VALUES (?, ?, ?, ?, 0)
            """,
            ("value:6:user-1", "user-1", "legacy-session", "2026-01-01T00:00:00+00:00"),
        )

    with SessionStore(database_path) as store:
        # 迁移后:老行可读,title 为 None(前端回退显示 session_id)
        records = store.list_sessions(user_id="user-1")
        assert [record.session_id for record in records] == ["legacy-session"]
        assert [record.title for record in records] == [None]
        assert records[0].updated_at == records[0].created_at
        # 迁移后的库可正常补标题
        assert (
            store.set_title_if_absent("legacy-session", "旧会话标题", user_id="user-1") is True
        )

    # 重开幂等:迁移不重复执行,已写标题保留
    with SessionStore(database_path) as reopened:
        assert [record.title for record in reopened.list_sessions(user_id="user-1")] == [
            "旧会话标题"
        ]
