"""SQLite-backed LangGraph checkpoint lifecycle helpers."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver


@contextmanager
def open_sqlite_checkpointer(path: str | Path) -> Iterator[SqliteSaver]:
    """Open a SQLite checkpointer and close its connection on exit."""
    database_path = Path(path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path, check_same_thread=False)
    try:
        yield SqliteSaver(connection)
    finally:
        connection.close()


__all__ = ["open_sqlite_checkpointer"]
