"""学情诊断与洞察端点（六大功能计划 P3-15 + 赛前可视化增强）。

`GET /learning/diagnosis/summary`：按用户聚合学习记录（learning.db），
返回知识点作答明细与预警列表。数据源是 P2-10 批改落库与 P0-5 对话
记录的确定性 SQL 聚合（预警规则 attempts≥2 且加权正确率<0.6，见
core/learning/store.py）——LLM 叙述只出现在对话内诊断报告，本端点
是纯数据视图，可复现、可审计。

`GET /learning/insights/summary`：错题归因分布/正确率趋势/路径存档
回显（学习进度页可视化数据源），同为确定性聚合，降级红线与诊断端点一致。

产品边界显式声明（pi 审查 🟡7）：
- 对话内诊断 = 当前用户自助诊断（学生视角，evaluator 角色卡约定）；
- 本端点的 `student_id` 查询参数供教师视角查询指定学生——REST 层
  入参，不违背工具层「user_id 模型不可控」红线（对话内工具仍只能用
  scope 注入的当前用户）；
- v1 无认证体系（SIX_FEATURES §三 / TASKS_STAGE_3_DETAILS §八 单独立项）：student_id 查询在
  演示环境为可信声明，生产部署需待认证落地后加角色授权；
- store 未注入（未配置 learning.db 的部署）→ 返回空报告 200 而非
  报错（降级红线，与「空数据返回空报告」同一口径）。
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from api.schemas import (
    DailyAccuracyPoint,
    DiagnosisKnowledgePoint,
    DiagnosisSummary,
    LearningInsights,
    PathPlanRecordDto,
)
from api.sessions import current_user_id
from core.learning import LearningRecordStore

router = APIRouter(prefix="/learning", tags=["learning"])
_LOGGER = logging.getLogger("api.learning")


def _learning_store(request: Request) -> LearningRecordStore | None:
    """从 app.state 取学习记录 store（lifespan 装配；未配置时 None）。

    与 knowledge_service 的 getattr 兜底同一模式：单测直接 create_app()
    不跑 lifespan 时属性不存在，返回 None 走空报告降级而非 500。
    """
    return getattr(request.app.state, "learning_store", None)


def _resolve_target_user(
    user_id: str | None,
    student_id: str | None,
) -> str | None:
    """确定查询目标：教师视角显式 student_id 优先，学生视角用当前 X-User-Id。

    横向读取他人学情的行为审计留痕（脱敏：只记标识与动作，不记聚合数据本身）
    ——v1 无认证下可追溯，认证落地后升级为角色校验。
    """
    if student_id is not None and student_id != user_id:
        _LOGGER.warning(
            "学情数据教师视角查询：accessor=%s target_student=%s",
            user_id,
            student_id,
        )
    return student_id if student_id else user_id


def _empty_summary(user_id: str | None) -> DiagnosisSummary:
    """空报告（无 store / 无作答记录）：200 + 零数据，不报错。"""
    return DiagnosisSummary(user_id=user_id)


@router.get("/diagnosis/summary", response_model=DiagnosisSummary)
async def diagnosis_summary(
    request: Request,
    user_id: Annotated[str | None, Depends(current_user_id)],
    # 审查 W6：输入约束（防超长注入物进日志/存储）+ 教师视角审计——
    # 横向读取他人学情画像的行为必须留痕，认证落地时按日志收口。
    student_id: Annotated[str | None, Query(max_length=64)] = None,
) -> DiagnosisSummary:
    """返回学情诊断摘要（默认当前用户；student_id 供教师视角查询）。

    边界与降级见模块 docstring：v1 的 student_id 查询是演示环境可信
    声明（无鉴权），生产需待认证落地；store 缺失或空数据返回空报告。
    """
    store = _learning_store(request)
    # 教师视角：显式 student_id 优先；学生视角：当前 X-User-Id。
    target_user = _resolve_target_user(user_id, student_id)
    if store is None or target_user is None:
        return _empty_summary(target_user)
    summary = store.summarize(target_user)
    return DiagnosisSummary(
        user_id=target_user,
        total_attempts=int(summary["total_attempts"]),
        knowledge_points=[
            DiagnosisKnowledgePoint(
                knowledge_point=str(point["knowledge_point"]),
                attempts=int(point["attempts"]),
                correct=int(point["correct"]),
                accuracy=float(point["accuracy"]),
                last_at=point["last_at"],
            )
            for point in summary["knowledge_points"]
        ],
        uncategorized_attempts=int(summary["uncategorized"]["attempts"]),
        weak_points=list(summary["weak_points"]),
    )


@router.get("/insights/summary", response_model=LearningInsights)
async def learning_insights(
    request: Request,
    user_id: Annotated[str | None, Depends(current_user_id)],
    # 与诊断端点同一产品边界：教师视角查询参数，审计留痕在 _resolve_target_user。
    student_id: Annotated[str | None, Query(max_length=64)] = None,
) -> LearningInsights:
    """返回学情洞察摘要（错题归因/正确率趋势/路径存档回显）。

    降级红线与诊断端点一致：store 缺失或空数据返回空报告 200。
    数据窗口有界（趋势近 30 日、路径近 20 条，见 core/learning/store.py）。
    """
    store = _learning_store(request)
    target_user = _resolve_target_user(user_id, student_id)
    if store is None or target_user is None:
        return LearningInsights(user_id=target_user)
    data = store.insights(target_user)
    return LearningInsights(
        user_id=target_user,
        total_wrong=int(data["total_wrong"]),
        error_tag_counts={
            str(tag): int(count)
            for tag, count in data["error_tag_counts"].items()
        },
        daily_accuracy=[
            DailyAccuracyPoint(
                date=str(point["date"]),
                attempts=int(point["attempts"]),
                accuracy=float(point["accuracy"]),
            )
            for point in data["daily_accuracy"]
        ],
        recent_path_plans=[
            PathPlanRecordDto(
                knowledge_point=plan["knowledge_point"],
                created_at=plan["created_at"],
            )
            for plan in data["recent_path_plans"]
        ],
    )


__all__ = ["router"]
