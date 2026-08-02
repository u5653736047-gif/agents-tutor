"""SQLite session metadata store tests."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from core.sessions import SessionRecord, SessionStore


def test_session_store_creates_and_lists_immutable_records(tmp_path: Path) -> None:
    with SessionStore(tmp_path / "metadata" / "sessions.sqlite") as store:
        first = store.create_session("session-1", user_id="user-1")
        second = store.create_session("session-2", user_id="user-1")

        assert store.list_sessions(user_id="user-1") == [first, second]

    assert first.session_id == "session-1"
    assert first.user_id == "user-1"
    assert first.archived is False
    with pytest.raises(FrozenInstanceError):
        first.archived = True  # type: ignore[misc]


def test_session_store_archives_without_deleting_metadata(tmp_path: Path) -> None:
    with SessionStore(tmp_path / "sessions.sqlite") as store:
        active = store.create_session("active", user_id="user-1")
        store.create_session("archived", user_id="user-1")

        assert store.archive_session("archived", user_id="user-1") is True
        assert store.archive_session("archived", user_id="user-1") is False
        assert store.archive_session("missing", user_id="user-1") is False
        assert store.list_sessions(user_id="user-1") == [active]

        all_sessions = store.list_sessions(user_id="user-1", include_archived=True)

    assert [record.session_id for record in all_sessions] == ["active", "archived"]
    assert [record.archived for record in all_sessions] == [False, True]


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
            key=lambda record: (record.created_at, record.session_id),
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
