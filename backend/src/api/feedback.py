"""用户反馈 REST 路由(D6-T1):只收脱敏引用字段,追加写入 JSONL 存储。

脱敏口径:请求体只包含 session_id / message_id / rating / comment /
error_code 等引用字段,绝不接收或持久化消息全文;user_id 来自
X-User-Id 头(缺失时为匿名 None),与会话 API 的匿名语义一致。
存储为 JSONL 单行追加,一行一条反馈记录,字段固定 7 个
(见 submit_feedback 的 record 注释)。

错误码约定:校验失败由 app 层统一返回 422 + invalid_request;存储
写失败(IO / JSON 序列化异常)返回 500 + internal_error 的稳定
ErrorResponse,不向客户端暴露底层错误细节(与 api/sessions 的
_raise_error 同构)。
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, NoReturn

from fastapi import APIRouter, Depends, HTTPException, status

from api.schemas import (
    ApiErrorCode,
    ErrorDetail,
    ErrorResponse,
    FeedbackRequest,
    FeedbackResponse,
)
from api.sessions import current_user_id

router = APIRouter(prefix="/feedback", tags=["feedback"])
FEEDBACK_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse},
    status.HTTP_500_INTERNAL_SERVER_ERROR: {"model": ErrorResponse},
}
# ── 默认存储路径:仓库根 data/feedback.jsonl ────────────────────────
# 与 api/app.py 的 DEFAULT_SESSION_STORE_PATH 同一惯例:用 __file__
# 定位仓库根(feedback.py 位于 backend/src/api/,parents[3] 即仓库
# 根)。不能写成相对启动工作目录的 "data/...":uvicorn 与 pytest 都
# 在 backend/ 下启动,相对路径会落到 backend/data/(目录不存在,
# 而 JSONL 追加写不会自动兜底)。部署时由 start-stage3.ps1 注入
# 绝对路径 env 覆盖此默认值。
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_FEEDBACK_STORE_PATH = str(_REPO_ROOT / "data" / "feedback.jsonl")


def _raise_error(status_code: int, error_code: ApiErrorCode, message: str) -> NoReturn:
    """与 api/sessions._raise_error 同构:抛出标准 ErrorResponse 体。"""
    detail = ErrorDetail(error_code=error_code, message=message)
    raise HTTPException(status_code=status_code, detail=detail.model_dump(mode="json"))


def _append_feedback_record(store_path: Path, record: dict[str, str | None]) -> None:
    """把一条反馈记录以 JSON 行追加到存储文件。

    单进程语义:只做单行追加写(不跨进程加锁),Python 文件对象的
    单行 write 在单进程内串行,行级原子性足够;多进程部署如需更强
    保证应换用带锁的存储后端,此处注释说明而不引入锁。
    """
    store_path.parent.mkdir(parents=True, exist_ok=True)
    with store_path.open("a", encoding="utf-8") as store:
        store.write(json.dumps(record, ensure_ascii=False) + "\n")


@router.post("", response_model=FeedbackResponse, responses=FEEDBACK_ERROR_RESPONSES)
def submit_feedback(
    payload: FeedbackRequest,
    user_id: Annotated[str | None, Depends(current_user_id)],
) -> FeedbackResponse:
    """记录一条用户反馈,返回受理确认。

    只存脱敏引用字段:record 固定 7 个键,不含消息全文;user_id 为
    None 时落 null(匿名反馈),与会话 API 的匿名语义一致。
    """
    store_path = Path(os.getenv("API_FEEDBACK_STORE_PATH", DEFAULT_FEEDBACK_STORE_PATH))
    record: dict[str, str | None] = {
        "user_id": user_id,
        "session_id": payload.session_id,
        "message_id": payload.message_id,
        "rating": payload.rating.value,
        "comment": payload.comment,
        "error_code": payload.error_code,
        "received_at": datetime.now(UTC).isoformat(),
    }
    try:
        _append_feedback_record(store_path, record)
    except (OSError, ValueError, TypeError):
        # OSError 覆盖 IOError / IsADirectoryError / PermissionError 等
        # 文件写入失败;ValueError / TypeError 覆盖 JSON 序列化异常。
        # 统一映射为稳定错误码 internal_error,不向客户端暴露细节。
        _raise_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            ApiErrorCode.INTERNAL_ERROR,
            "The request could not be completed.",
        )
    return FeedbackResponse(received=True)
