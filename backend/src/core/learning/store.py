"""学习记录存储（六大功能计划 P0-4）：跨会话学生数据的 SQLite 底座。

（面向初学者的设计说明）

1. 为什么需要这个模块
   既有持久化全部是会话级的（checkpoint / sessions / feedback.jsonl），
   没有任何「学生 × 知识点」粒度的记录——学情诊断（功能 3）、学习
   路径规划（功能 4）、学习陪伴的错题归因（功能 6）都无从谈起。
   本模块提供单表 learning_records 的追加式记录与 SQL 聚合能力，
   是三个功能的公共数据底座。

2. 表结构要点
   - 只存标签与引用、**不存作答正文**（脱敏口径同 feedback.py：
     事件/记录不落敏感正文）；
   - outcome 三档（correct/partial/incorrect）由 CHECK 约束；
   - kind 标记记录来源（answer=对话内答疑、grading=批改落库、
     diagnosis=诊断摘要、path_plan=路径存档）；
   - 复合唯一约束 UNIQUE(source_tool_call_id, question_id)（pi 三轮
     审查 🟡3）：批改路径一次 submit_grading 的 N 题共享同一
     tool_call_id，单列 UNIQUE + INSERT OR IGNORE 会让第 2..N 题
     静默丢失；复合键下每题各占一行，重放同一次批改幂等忽略。
     SQLite 的 UNIQUE 中 NULL 可重复——模型主动调用的
     record_learning_outcome 路径两键均 NULL，不受该约束影响。

3. 聚合与预警规则（确定性，可单测）
   - 按知识点统计 attempts / correct / 加权正确率（partial 计 0.5）；
   - 预警：attempts≥2 且正确率<0.6 → weak（学情诊断的预警列表）；
   - 最久未练排序：last_at 升序（学习陪伴「最久未练优先」巩固出题）。
   LLM 只消费聚合结果写叙述，不逐条阅读原始记录（诊断成本恒定）。

4. 并发与线程安全
   与 SessionStore 同一模式：单连接 check_same_thread=False + RLock
   串行化所有访问；WAL 模式允许读写并发不阻塞（读多写少的学情数据）。
"""

from __future__ import annotations

import sqlite3
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any

# 学习记录作用域：携带当前运行轮次的 user_id/session_id。
# 为什么用 ContextVar 而不是工具参数：user_id 由图执行上下文注入、
# 模型不可见不可控（防跨用户伪造记录）；定义在 core/learning 而非
# graph_builder，避免 tools → graph_builder 的循环 import。
# None 表示「未设置作用域」（无 store 注入的测试环境/直接调用）。
learning_scope: ContextVar[dict[str, str | None] | None] = ContextVar(
    "learning_scope", default=None
)

# outcome / kind 的合法取值（schema CHECK 约束的同源定义）。
LEARNING_OUTCOMES = frozenset({"correct", "partial", "incorrect"})
LEARNING_RECORD_KINDS = frozenset({"answer", "grading", "diagnosis", "path_plan"})

# 预警规则（P3 学情诊断）：至少作答 2 次且加权正确率低于 0.6。
# 为什么阈值定在这里：确定性规则写死在存储层可单测、可解释，
# LLM 不参与判定（诊断报告的预警项必须可复现）。
_WEAK_MIN_ATTEMPTS = 2
_WEAK_ACCURACY_THRESHOLD = 0.6

# 学情洞察聚合的有界窗口（赛前可视化增强）：正确率趋势最多回看
# 30 个 UTC 日、路径存档最多回显 20 条——教学项目数据规模小，窗口
# 只为防御异常数据量撑爆契约，与 step_outputs 截断同一「有界」哲学。
_INSIGHTS_MAX_DAYS = 30
_INSIGHTS_MAX_PATH_PLANS = 20


def _outcome_from_score(score: float, max_score: float) -> str:
    """批改落库的 outcome 推导（确定性规则，P2-10）。

    满分 → correct、零分 → incorrect、其余 → partial；max_score 非正
    属调用方 schema 错误，防御性归为 partial（不让脏数据击穿落库）。
    """
    if max_score <= 0:
        return "partial"
    if score >= max_score:
        return "correct"
    if score <= 0:
        return "incorrect"
    return "partial"


class LearningRecordStore:
    """学习记录的追加式存储与聚合查询（单表 learning_records）。"""

    def __init__(self, database_path: str | Path) -> None:
        database_path = Path(database_path)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        # 所有连接访问均由实例锁串行化，因此允许跨线程复用
        # （与 SessionStore 同一注释与模式）。
        connection = sqlite3.connect(database_path, check_same_thread=False)
        try:
            connection.row_factory = sqlite3.Row
            # WAL：读多写少的学情数据，读不阻塞写、写不阻塞读；
            # 与 checkpointer 的 WAL 先例一致。
            connection.execute("PRAGMA journal_mode=WAL")
            with connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS learning_records (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id TEXT NOT NULL,
                        session_id TEXT,
                        knowledge_point TEXT,
                        question_id TEXT,
                        outcome TEXT NOT NULL
                            CHECK (outcome IN ('correct', 'partial', 'incorrect')),
                        error_tag TEXT,
                        kind TEXT NOT NULL
                            CHECK (kind IN ('answer', 'grading', 'diagnosis', 'path_plan')),
                        source_tool_call_id TEXT,
                        created_at TEXT NOT NULL,
                        UNIQUE (source_tool_call_id, question_id)
                    )
                    """
                )
                # 用户隔离与按知识点聚合的主查询路径索引。
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_learning_records_user "
                    "ON learning_records (user_id)"
                )
        except BaseException:
            connection.close()
            raise
        self._lock = RLock()
        self._connection = connection

    def append_record(
        self,
        user_id: str,
        *,
        session_id: str | None = None,
        knowledge_point: str | None = None,
        outcome: str,
        kind: str,
        error_tag: str | None = None,
        question_id: str | None = None,
        source_tool_call_id: str | None = None,
    ) -> bool:
        """追加一条学习记录；复合唯一键冲突时幂等忽略（返回 False）。

        返回 True 表示本次真实插入、False 表示被幂等键忽略（重放
        保护，INSERT OR IGNORE 语义）。枚举非法值抛 ValueError——
        写入端严格（与 schema CHECK 双保险，脏数据不进库）。
        """
        if not user_id.strip():
            raise ValueError("user_id must not be blank")
        if outcome not in LEARNING_OUTCOMES:
            raise ValueError(f"invalid outcome: {outcome}")
        if kind not in LEARNING_RECORD_KINDS:
            raise ValueError(f"invalid kind: {kind}")
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO learning_records (
                    user_id, session_id, knowledge_point, question_id,
                    outcome, error_tag, kind, source_tool_call_id, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    session_id,
                    knowledge_point,
                    question_id,
                    outcome,
                    error_tag,
                    kind,
                    source_tool_call_id,
                    now,
                ),
            )
            return cursor.rowcount > 0

    def append_grading_records(
        self,
        items: list[dict[str, Any]],
        *,
        user_id: str,
        session_id: str | None,
        tool_call_id: str,
    ) -> int:
        """批改结果的逐题确定性落库（P2-10，不靠模型自觉）。

        items 每项需含 question_id/score/max_score，可含
        knowledge_point/error_tag；outcome 由得分比例推导
        （_outcome_from_score）、kind 固定 "grading"、复合幂等键
        (tool_call_id, question_id) 防重放重复入库。返回真实插入条数
        （多题 fixture 下应等于题数——单列 UNIQUE 的静默丢数据 bug
        正是由此断言守护，pi 三轮审查 🟡3）。
        """
        inserted = 0
        for item in items:
            inserted += int(
                self.append_record(
                    user_id,
                    session_id=session_id,
                    knowledge_point=item.get("knowledge_point"),
                    outcome=_outcome_from_score(
                        float(item.get("score", 0)),
                        float(item.get("max_score", 0)),
                    ),
                    kind="grading",
                    error_tag=item.get("error_tag"),
                    question_id=item.get("question_id"),
                    source_tool_call_id=tool_call_id,
                )
            )
        return inserted

    def summarize(self, user_id: str) -> dict[str, Any]:
        """按用户聚合学习记录（学情诊断/路径规划/陪伴的数据源）。

        返回结构（全 JSON 原生类型，工具层可直接序列化）：
        - total_attempts：总作答次数（含未分类）；
        - knowledge_points：按知识点聚合明细（按最近练习时间倒序，
          「最久未练」即列表尾部——学习陪伴巩固出题 v1 口径）；
        - uncategorized：knowledge_point 为 NULL 的记录总量统计；
        - weak_points：预警知识点列表（attempts≥2 且正确率<0.6）。
        加权正确率 =（correct×1 + partial×0.5）/ attempts。
        """
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT
                    knowledge_point,
                    COUNT(*) AS attempts,
                    SUM(CASE WHEN outcome = 'correct' THEN 1 ELSE 0 END) AS correct,
                    SUM(CASE WHEN outcome = 'partial' THEN 1 ELSE 0 END) AS partial,
                    MAX(created_at) AS last_at
                FROM learning_records
                WHERE user_id = ? AND kind IN ('answer', 'grading')
                GROUP BY knowledge_point
                """,
                (user_id,),
            ).fetchall()
            total_row = self._connection.execute(
                """
                SELECT COUNT(*) AS attempts
                FROM learning_records
                WHERE user_id = ? AND kind IN ('answer', 'grading')
                """,
                (user_id,),
            ).fetchone()
        points: list[dict[str, Any]] = []
        uncategorized: dict[str, Any] = {"attempts": 0, "correct": 0, "accuracy": 0.0}
        weak_points: list[str] = []
        for row in rows:
            attempts = int(row["attempts"])
            correct = int(row["correct"])
            partial = int(row["partial"])
            accuracy = (
                (correct + 0.5 * partial) / attempts if attempts > 0 else 0.0
            )
            entry: dict[str, Any] = {
                "knowledge_point": row["knowledge_point"],
                "attempts": attempts,
                "correct": correct,
                "accuracy": round(accuracy, 3),
                "last_at": row["last_at"],
            }
            if row["knowledge_point"] is None:
                uncategorized = entry | {"knowledge_point": None}
                continue
            points.append(entry)
            if attempts >= _WEAK_MIN_ATTEMPTS and accuracy < _WEAK_ACCURACY_THRESHOLD:
                weak_points.append(str(row["knowledge_point"]))
        # 最近练习时间倒序：尾部即「最久未练」（陪伴巩固出题口径）。
        points.sort(key=lambda point: str(point["last_at"]), reverse=True)
        return {
            "total_attempts": int(total_row["attempts"]) if total_row else 0,
            "knowledge_points": points,
            "uncategorized": uncategorized,
            "weak_points": weak_points,
        }

    def insights(self, user_id: str) -> dict[str, Any]:
        """学情洞察聚合（前端学习进度页可视化的数据源）。

        与 summarize 的分工：summarize 面向「知识点掌握与预警」（诊断），
        本方法面向「错题归因 / 正确率趋势 / 路径存档回显」（展示）。全部是
        确定性 SQL 聚合，可单测、可复现。

        返回结构（全 JSON 原生类型）：
        - total_wrong：错答/部分正确记录总量（kind 限 answer/grading）；
        - error_tag_counts：错因标签分布（仅统计 outcome 非 correct 且
          error_tag 非空的记录；同 summarize 只计 answer/grading 两类）；
        - daily_accuracy：按 UTC 日聚合的加权正确率（升序，最多近 30 日，
          与 summarize 同一加权口径：correct×1 + partial×0.5）；
        - recent_path_plans：最近的路径存档记录（倒序，最多 20 条）——
          脱敏口径不变：只有知识点与时间，无路径正文。
        """
        with self._lock:
            wrong_total_row = self._connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM learning_records
                WHERE user_id = ?
                    AND kind IN ('answer', 'grading')
                    AND outcome != 'correct'
                """,
                (user_id,),
            ).fetchone()
            tag_rows = self._connection.execute(
                """
                SELECT error_tag, COUNT(*) AS count
                FROM learning_records
                WHERE user_id = ?
                    AND kind IN ('answer', 'grading')
                    AND outcome != 'correct'
                    AND error_tag IS NOT NULL
                GROUP BY error_tag
                ORDER BY count DESC, error_tag ASC
                """,
                (user_id,),
            ).fetchall()
            day_rows = self._connection.execute(
                """
                SELECT
                    substr(created_at, 1, 10) AS day,
                    COUNT(*) AS attempts,
                    SUM(CASE WHEN outcome = 'correct' THEN 1 ELSE 0 END) AS correct,
                    SUM(CASE WHEN outcome = 'partial' THEN 1 ELSE 0 END) AS partial
                FROM learning_records
                WHERE user_id = ? AND kind IN ('answer', 'grading')
                GROUP BY day
                ORDER BY day DESC
                LIMIT ?
                """,
                (user_id, _INSIGHTS_MAX_DAYS),
            ).fetchall()
            plan_rows = self._connection.execute(
                """
                SELECT knowledge_point, created_at
                FROM learning_records
                WHERE user_id = ? AND kind = 'path_plan'
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (user_id, _INSIGHTS_MAX_PATH_PLANS),
            ).fetchall()
        daily: list[dict[str, Any]] = []
        for row in reversed(day_rows):
            attempts = int(row["attempts"])
            accuracy = (
                (int(row["correct"]) + 0.5 * int(row["partial"])) / attempts
                if attempts > 0
                else 0.0
            )
            daily.append(
                {
                    "date": str(row["day"]),
                    "attempts": attempts,
                    "accuracy": round(accuracy, 3),
                }
            )
        return {
            "total_wrong": int(wrong_total_row["total"]) if wrong_total_row else 0,
            "error_tag_counts": {
                str(row["error_tag"]): int(row["count"]) for row in tag_rows
            },
            "daily_accuracy": daily,
            "recent_path_plans": [
                {
                    "knowledge_point": row["knowledge_point"],
                    "created_at": row["created_at"],
                }
                for row in plan_rows
            ],
        }

    def close(self) -> None:
        """关闭底层连接（lifespan 退出时调用，避免连接泄漏）。"""
        with self._lock:
            self._connection.close()


__all__ = [
    "LEARNING_OUTCOMES",
    "LEARNING_RECORD_KINDS",
    "LearningRecordStore",
    "learning_scope",
]
