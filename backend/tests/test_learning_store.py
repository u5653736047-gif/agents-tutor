"""学习记录存储测试（六大功能计划 P0-4 验收）。

覆盖：追加/聚合/预警阈值/用户隔离/复合幂等键（多题 fixture，
pi 三轮审查 🟡3——单题 fixture 测不出单列 UNIQUE 静默丢数据）/
outcome 推导/写入端严格校验。全部 tmp_path 纯函数单测，不依赖图。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.learning import LearningRecordStore


@pytest.fixture()
def store(tmp_path: Path) -> LearningRecordStore:
    record_store = LearningRecordStore(tmp_path / "learning.db")
    yield record_store
    record_store.close()


def test_append_and_summarize_basic_aggregation(store: LearningRecordStore) -> None:
    store.append_record(
        "student-a",
        session_id="s1",
        knowledge_point="梯度下降",
        outcome="correct",
        kind="answer",
    )
    store.append_record(
        "student-a",
        session_id="s1",
        knowledge_point="梯度下降",
        outcome="partial",
        kind="grading",
    )

    summary = store.summarize("student-a")

    assert summary["total_attempts"] == 2
    point = summary["knowledge_points"][0]
    assert point["knowledge_point"] == "梯度下降"
    assert point["attempts"] == 2
    assert point["correct"] == 1
    # 加权正确率 = (1×1 + 1×0.5) / 2 = 0.75
    assert point["accuracy"] == 0.75
    assert summary["weak_points"] == []


def test_weak_point_rule_requires_attempts_and_low_accuracy(
    store: LearningRecordStore,
) -> None:
    # 薄弱点：3 次作答仅 1 次 partial → 正确率 0.167 < 0.6 → weak
    store.append_record(
        "student-a", knowledge_point="反向传播", outcome="incorrect", kind="answer"
    )
    store.append_record(
        "student-a", knowledge_point="反向传播", outcome="incorrect", kind="grading"
    )
    store.append_record(
        "student-a", knowledge_point="反向传播", outcome="partial", kind="answer"
    )
    # 单次低分不构成预警（attempts 下限 2）
    store.append_record(
        "student-a", knowledge_point="卷积", outcome="incorrect", kind="answer"
    )
    # 高正确率不构成预警
    store.append_record(
        "student-a", knowledge_point="正则化", outcome="correct", kind="answer"
    )
    store.append_record(
        "student-a", knowledge_point="正则化", outcome="correct", kind="answer"
    )

    summary = store.summarize("student-a")

    assert summary["weak_points"] == ["反向传播"]


def test_user_isolation_between_students(store: LearningRecordStore) -> None:
    store.append_record(
        "student-a", knowledge_point="SVM", outcome="correct", kind="answer"
    )

    summary_b = store.summarize("student-b")

    assert summary_b["total_attempts"] == 0
    assert summary_b["knowledge_points"] == []
    assert summary_b["weak_points"] == []


def test_uncategorized_records_counted_but_not_grouped(
    store: LearningRecordStore,
) -> None:
    store.append_record("student-a", outcome="correct", kind="grading")

    summary = store.summarize("student-a")

    assert summary["total_attempts"] == 1
    assert summary["knowledge_points"] == []
    assert summary["uncategorized"]["attempts"] == 1
    assert summary["uncategorized"]["correct"] == 1


def test_composite_idempotency_key_keeps_all_questions(
    store: LearningRecordStore,
) -> None:
    """pi 三轮审查 🟡3：一次批改的 N 题共享 tool_call_id，复合键下
    全部入库（单列 UNIQUE 会只留第 1 题）；重放同一次批改幂等忽略。"""
    items = [
        {"question_id": "q1", "score": 10, "max_score": 10, "knowledge_point": "注意力机制"},
        {"question_id": "q2", "score": 0, "max_score": 10, "knowledge_point": "注意力机制"},
        {"question_id": "q3", "score": 5, "max_score": 10, "knowledge_point": "Transformer"},
    ]

    inserted = store.append_grading_records(
        items, user_id="student-a", session_id="s1", tool_call_id="call-1"
    )
    # 重放同一次批改：三题复合键全部冲突，零新增
    replayed = store.append_grading_records(
        items, user_id="student-a", session_id="s1", tool_call_id="call-1"
    )

    assert inserted == 3
    assert replayed == 0
    summary = store.summarize("student-a")
    assert summary["total_attempts"] == 3
    by_point = {p["knowledge_point"]: p for p in summary["knowledge_points"]}
    assert by_point["注意力机制"]["attempts"] == 2
    assert by_point["Transformer"]["attempts"] == 1
    # outcome 推导：满分→correct、零分→incorrect、部分→partial；
    # 注意力机制正确率 0.5 < 0.6 且 attempts=2 → 预警
    assert summary["weak_points"] == ["注意力机制"]


def test_null_idempotency_keys_allow_repeated_model_records(
    store: LearningRecordStore,
) -> None:
    """SQLite UNIQUE 中 NULL 可重复：record_learning_outcome 路径
    （无 tool_call_id/question_id）的重复记录不受幂等约束影响。"""
    first = store.append_record(
        "student-a", knowledge_point="损失函数", outcome="incorrect", kind="answer"
    )
    second = store.append_record(
        "student-a", knowledge_point="损失函数", outcome="correct", kind="answer"
    )

    assert first is True
    assert second is True
    assert store.summarize("student-a")["total_attempts"] == 2


def test_missing_knowledge_point_record_is_accepted(
    store: LearningRecordStore,
) -> None:
    # 批改漏填知识点：记为未分类（总量统计有、知识点聚合无）
    inserted = store.append_grading_records(
        [{"question_id": "q1", "score": 3, "max_score": 10}],
        user_id="student-a",
        session_id=None,
        tool_call_id="call-x",
    )

    assert inserted == 1
    summary = store.summarize("student-a")
    assert summary["uncategorized"]["attempts"] == 1
    assert summary["knowledge_points"] == []


@pytest.mark.parametrize(
    "kwargs,expected_error",
    [
        ({"outcome": "unknown"}, ValueError),
        # grading 在 store 层合法（批改落库路径直写），非法 kind 用 quiz
        ({"kind": "quiz"}, ValueError),
        ({"user_id": ""}, ValueError),
    ],
    ids=["bad-outcome", "bad-kind", "blank-user"],
)
def test_append_record_rejects_invalid_inputs(
    store: LearningRecordStore,
    kwargs: dict[str, str],
    expected_error: type[ValueError],
) -> None:
    payload = {
        "user_id": "student-a",
        "knowledge_point": "某知识点",
        "outcome": "correct",
        "kind": "answer",
        **kwargs,
    }
    with pytest.raises(expected_error):
        store.append_record(payload.pop("user_id"), **payload)  # type: ignore[arg-type]
