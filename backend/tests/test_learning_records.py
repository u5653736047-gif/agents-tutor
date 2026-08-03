"""学习记录存储与读写工具测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from core.events import ErrorCode
from core.learning import LearningRecord, LearningRecordStore, create_learning_tools
from core.state import AgentRole
from core.tools import ToolExecutor


def test_add_and_list_records_in_reverse_chronological_order(
    tmp_path: Path,
) -> None:
    with LearningRecordStore(tmp_path / "learning.sqlite") as store:
        store.add_record(
            LearningRecord(
                user_id="u1",
                session_id="s1",
                topic="梯度下降",
                mastery=2,
                note="初步了解",
            )
        )
        store.add_record(
            LearningRecord(
                user_id="u1",
                session_id="s2",
                topic="反向传播",
                mastery=4,
            )
        )

        records = store.list_records(user_id="u1")

        assert [record.topic for record in records] == ["反向传播", "梯度下降"]
        assert records[0].mastery == 4
        assert records[1].note == "初步了解"


def test_list_filters_by_topic_and_limits() -> None:
    with LearningRecordStore(":memory:") as store:
        for topic in ("线性回归", "逻辑回归", "线性回归"):
            store.add_record(
                LearningRecord(user_id="u1", session_id="s1", topic=topic, mastery=1)
            )

        regression = store.list_records(user_id="u1", topic="线性回归")
        limited = store.list_records(user_id="u1", limit=1)

        assert len(regression) == 2
        assert len(limited) == 1


def test_records_are_isolated_per_user() -> None:
    with LearningRecordStore(":memory:") as store:
        store.add_record(
            LearningRecord(user_id="u1", session_id="s1", topic="注意力机制", mastery=3)
        )

        assert store.list_records(user_id="u1")
        assert store.list_records(user_id="u2") == []


def test_store_persists_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "learning.sqlite"
    with LearningRecordStore(path) as store:
        store.add_record(
            LearningRecord(user_id="u1", session_id="s1", topic="CNN", mastery=2)
        )

    with LearningRecordStore(path) as reopened:
        assert [record.topic for record in reopened.list_records(user_id="u1")] == [
            "CNN"
        ]


def test_tools_save_and_query_with_bound_identity(tmp_path: Path) -> None:
    store = LearningRecordStore(tmp_path / "learning.sqlite")
    try:
        save_tool, query_tool = create_learning_tools(
            store,
            user_id="u1",
            session_id="s1",
        )

        saved = save_tool.invoke({"topic": "Transformer", "mastery": 3, "note": "论文"})
        found = query_tool.invoke({"topic": "Transformer"})

        assert saved["saved"] is True
        assert found["found"] is True
        assert found["records"][0]["topic"] == "Transformer"
        assert found["records"][0]["mastery"] == 3
        assert found["records"][0]["note"] == "论文"
    finally:
        store.close()


def test_tools_respect_user_isolation(tmp_path: Path) -> None:
    store = LearningRecordStore(tmp_path / "learning.sqlite")
    try:
        save_tool, _ = create_learning_tools(store, user_id="u1", session_id="s1")
        _, query_tool = create_learning_tools(store, user_id="u2", session_id="s1")

        save_tool.invoke({"topic": "RNN", "mastery": 1})
        found = query_tool.invoke({})

        assert found["found"] is False
        assert found["records"] == []
    finally:
        store.close()


def test_save_tool_rejects_blank_topic() -> None:
    store = LearningRecordStore(":memory:")
    try:
        save_tool, _ = create_learning_tools(store, user_id="u1", session_id="s1")
        execution = ToolExecutor([save_tool]).execute(
            {"name": "save_learning_record", "args": {"topic": " ", "mastery": 2}},
            AgentRole.LEARNING_ASSISTANT,
        )

        assert execution.result.success is False
        assert execution.result.error_code is ErrorCode.TOOL_INVALID_ARGUMENTS
    finally:
        store.close()


def test_record_rejects_out_of_range_mastery() -> None:
    with pytest.raises(ValidationError):
        LearningRecord(user_id="u1", session_id="s1", topic="SVM", mastery=9)
