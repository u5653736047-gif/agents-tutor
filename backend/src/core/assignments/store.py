"""SQLite 持久化的批改记录存储与学情聚合。"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import RLock
from types import TracebackType
from typing import Self, cast

from .models import (
    ClassSummary,
    GradedQuestion,
    GradingDraft,
    GradingResult,
    KnowledgePointAggregate,
    QuestionKind,
)


class AssignmentStore:
    """按 user_key 保存批改记录，供学情聚合查询。"""

    def __init__(self, path: str | Path) -> None:
        database_path = Path(path)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        # 所有连接访问均由实例锁串行化，因此允许跨线程复用。
        connection = sqlite3.connect(database_path, check_same_thread=False)
        try:
            connection.row_factory = sqlite3.Row
            with connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS submissions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        submission_id TEXT NOT NULL UNIQUE,
                        user_key TEXT NOT NULL,
                        user_id TEXT,
                        session_id TEXT NOT NULL,
                        title TEXT NOT NULL DEFAULT '',
                        total_points REAL NOT NULL,
                        total_score REAL NOT NULL,
                        objective_correct INTEGER NOT NULL DEFAULT 0,
                        objective_total INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_submissions_user
                        ON submissions(user_key, created_at);
                    CREATE TABLE IF NOT EXISTS graded_questions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        submission_id TEXT NOT NULL,
                        question_id TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        points REAL NOT NULL,
                        score REAL NOT NULL,
                        is_correct INTEGER,
                        is_estimated INTEGER NOT NULL DEFAULT 0,
                        correct_answer TEXT,
                        student_answer TEXT NOT NULL,
                        comment TEXT NOT NULL DEFAULT '',
                        UNIQUE (submission_id, question_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_graded_questions_sub
                        ON graded_questions(submission_id);
                    CREATE TABLE IF NOT EXISTS question_knowledge_points (
                        submission_id TEXT NOT NULL,
                        question_id TEXT NOT NULL,
                        knowledge_point TEXT NOT NULL,
                        PRIMARY KEY (submission_id, question_id, knowledge_point)
                    );
                    """
                )
        except BaseException:
            connection.close()
            raise
        self._lock = RLock()
        self._connection = connection

    def add_grading_result(
        self,
        draft: GradingDraft,
        *,
        user_id: str | None,
        session_id: str,
    ) -> GradingResult:
        """在单个事务中落库批改记录并返回完整结果。"""
        if not session_id.strip():
            raise ValueError("session_id must not be empty")
        submission_id = uuid.uuid4().hex[:12]
        created_at = datetime.now(UTC)
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO submissions
                    (submission_id, user_key, user_id, session_id, title,
                     total_points, total_score, objective_correct, objective_total,
                     created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    submission_id,
                    self._user_key(user_id),
                    user_id,
                    session_id,
                    draft.title,
                    draft.total_points,
                    draft.total_score,
                    draft.objective_correct,
                    draft.objective_total,
                    created_at.isoformat(),
                ),
            )
            for question in draft.questions:
                self._connection.execute(
                    """
                    INSERT INTO graded_questions
                        (submission_id, question_id, kind, points, score,
                         is_correct, is_estimated, correct_answer, student_answer,
                         comment)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        submission_id,
                        question.question_id,
                        question.kind,
                        question.points,
                        question.score,
                        None if question.is_correct is None else int(question.is_correct),
                        int(question.is_estimated),
                        question.correct_answer,
                        question.student_answer,
                        question.comment,
                    ),
                )
                for knowledge_point in question.knowledge_points:
                    self._connection.execute(
                        """
                        INSERT INTO question_knowledge_points
                            (submission_id, question_id, knowledge_point)
                        VALUES (?, ?, ?)
                        """,
                        (submission_id, question.question_id, knowledge_point),
                    )
        return GradingResult(
            **draft.model_dump(),
            submission_id=submission_id,
            user_id=user_id,
            session_id=session_id,
            created_at=created_at,
        )

    def list_submissions(
        self,
        user_id: str | None = None,
        *,
        limit: int = 50,
    ) -> list[GradingResult]:
        """按时间倒序返回某用户的批改记录（含题目级明细）。"""
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT submission_id, user_id, session_id, title,
                       total_points, total_score, objective_correct, objective_total,
                       created_at
                FROM submissions
                WHERE user_key = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (self._user_key(user_id), limit),
            ).fetchall()
        results = [self._grading_result_from_row(row) for row in rows]
        self._attach_questions(results)
        return results

    def aggregate_accuracy(
        self,
        user_ids: list[str] | None = None,
        *,
        since_days: int | None = None,
    ) -> list[KnowledgePointAggregate]:
        """按知识点统计客观题准确率；只统计客观题（is_correct 非空）。"""
        scope, parameters = self._scope_parameters(user_ids, since_days, table="s")
        if scope is None:
            return []
        rows = self._connection.execute(
            f"""
            SELECT kp.knowledge_point AS knowledge_point,
                   COUNT(q.is_correct) AS total_questions,
                   SUM(CASE WHEN q.is_correct = 1 THEN 1 ELSE 0 END) AS correct
            FROM question_knowledge_points kp
            JOIN graded_questions q
              ON q.submission_id = kp.submission_id
             AND q.question_id = kp.question_id
            JOIN submissions s ON s.submission_id = q.submission_id
            WHERE {scope}
            GROUP BY kp.knowledge_point
            HAVING COUNT(q.is_correct) > 0
            """,
            parameters,
        ).fetchall()
        return [
            KnowledgePointAggregate(
                knowledge_point=str(row["knowledge_point"]),
                total_questions=int(row["total_questions"]),
                correct=int(row["correct"]),
                accuracy=(
                    int(row["correct"]) / int(row["total_questions"])
                    if int(row["total_questions"]) > 0
                    else 0.0
                ),
            )
            for row in rows
        ]

    def class_summary(
        self,
        user_ids: list[str] | None = None,
        *,
        since_days: int | None = None,
    ) -> ClassSummary:
        """返回班级范围统计；user_ids 为 None 表示全量，[] 表示空班级。"""
        scope, parameters = self._scope_parameters(user_ids, since_days)
        if scope is None:
            return ClassSummary()
        row = self._connection.execute(
            f"""
            SELECT COUNT(*) AS submission_count,
                   COUNT(DISTINCT user_key) AS student_count,
                   AVG(total_score) AS average_score,
                   MAX(total_score) AS max_score,
                   MIN(total_score) AS min_score
            FROM submissions
            WHERE {scope}
            """,
            parameters,
        ).fetchone()
        return ClassSummary(
            submission_count=int(row["submission_count"]),
            student_count=int(row["student_count"]),
            average_score=float(row["average_score"] or 0.0),
            max_score=float(row["max_score"] or 0.0),
            min_score=float(row["min_score"] or 0.0),
        )

    def close(self) -> None:
        """关闭批改记录数据库连接。"""
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

    def _scope_parameters(
        self,
        user_ids: list[str] | None,
        since_days: int | None,
        *,
        table: str = "submissions",
    ) -> tuple[str | None, list[object]]:
        """构造 WHERE 子句与参数；返回 (None, []) 表示空结果集。

        table 为聚合查询中 submissions 表的别名（如 "s"），
        普通查询直接查询 submissions 表时传默认值。
        """
        if user_ids == []:
            return None, []
        prefix = f"{table}." if table else ""
        if user_ids is None:
            scope = "1 = 1"
            parameters: list[object] = []
        else:
            keys = [self._user_key(user_id) for user_id in user_ids]
            placeholders = ", ".join("?" for _ in keys)
            scope = f"{prefix}user_key IN ({placeholders})"
            parameters = list(keys)
        if since_days is not None:
            cutoff = (datetime.now(UTC) - timedelta(days=since_days)).isoformat()
            scope += f" AND {prefix}created_at >= ?"
            parameters.append(cutoff)
        return scope, parameters

    @staticmethod
    def _user_key(user_id: str | None) -> str:
        if user_id is None:
            return "none"
        if not user_id.strip():
            raise ValueError("user_id must not be empty")
        return f"value:{len(user_id)}:{user_id}"

    @staticmethod
    def _grading_result_from_row(row: sqlite3.Row) -> GradingResult:
        return GradingResult(
            submission_id=str(row["submission_id"]),
            user_id=cast(str | None, row["user_id"]),
            session_id=str(row["session_id"]),
            title=str(row["title"]),
            total_points=float(row["total_points"]),
            total_score=float(row["total_score"]),
            objective_correct=int(row["objective_correct"]),
            objective_total=int(row["objective_total"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            questions=[],
            warnings=[],
        )

    def _attach_questions(self, results: list[GradingResult]) -> None:
        """按 submission_id 批量补齐题目级明细与知识点。"""
        if not results:
            return
        ids = [result.submission_id for result in results]
        placeholders = ", ".join("?" for _ in ids)
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT submission_id, question_id, kind, points, score,
                       is_correct, is_estimated, correct_answer, student_answer, comment
                FROM graded_questions
                WHERE submission_id IN ({placeholders})
                """,
                ids,
            ).fetchall()
            points = self._connection.execute(
                f"""
                SELECT submission_id, question_id, knowledge_point
                FROM question_knowledge_points
                WHERE submission_id IN ({placeholders})
                """,
                ids,
            ).fetchall()
        knowledge_by_key: dict[tuple[str, str], list[str]] = {}
        for row in points:
            key = (str(row["submission_id"]), str(row["question_id"]))
            knowledge_by_key.setdefault(key, []).append(str(row["knowledge_point"]))
        by_id = {result.submission_id: result for result in results}
        for row in rows:
            submission_id = str(row["submission_id"])
            question_id = str(row["question_id"])
            knowledge_points = knowledge_by_key.get((submission_id, question_id), [])
            by_id[submission_id].questions.append(
                GradedQuestion(
                    question_id=question_id,
                    kind=cast(QuestionKind, str(row["kind"])),
                    knowledge_points=knowledge_points,
                    points=float(row["points"]),
                    score=float(row["score"]),
                    is_correct=(
                        None
                        if row["is_correct"] is None
                        else bool(row["is_correct"])
                    ),
                    is_estimated=bool(row["is_estimated"]),
                    correct_answer=cast(str | None, row["correct_answer"]),
                    student_answer=str(row["student_answer"]),
                    comment=str(row["comment"]),
                )
            )


__all__ = ["AssignmentStore"]
