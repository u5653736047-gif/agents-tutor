"""SQLite-backed LangGraph checkpoint lifecycle helpers."""

# 模块职责：管理 SQLite checkpoint 的完整生命周期——自动建目录、打开
# 连接、构造 SqliteSaver、退出时保证连接关闭，调用方无需关心连接管理。

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
    database_path.parent.mkdir(parents=True, exist_ok=True)  # 目录不存在时自动创建
    # 连接只交给带内部锁的 SqliteSaver，生命周期由上下文管理器管理。
    connection = sqlite3.connect(database_path, check_same_thread=False)
    try:
        yield SqliteSaver(connection)
    finally:
        connection.close()  # 无论正常退出还是抛异常，都关闭连接


__all__ = ["open_sqlite_checkpointer"]
