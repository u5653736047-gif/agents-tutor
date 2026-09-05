"""聊天附件上传与受控下载端点(D7-T1):POST /files + GET /files/{file_id}。

错误码约定(与 api/feedback.py 的 _raise_error 同构):
- 请求校验失败(扩展名不在白名单 / 超过大小上限 / 空文件):422 +
  invalid_request,均在 API 层拦截,不依赖运行时异常;
- 下载路径段非法(可能携带 "../" 等穿越载荷)或文件不存在(含越权
  访问——他人 user_key 下的文件名是 uuid,不可枚举,等价于文件不
  存在):404 + invalid_request;不返回 403,避免泄露目录存在性;
- 落盘/读盘 OSError 等底层异常:500 + internal_error,不向客户端
  暴露底层错误细节。

防路径穿越设计(上传/下载两道独立防线):
1. 上传:user_key 由 X-User-Id 消毒(非 [A-Za-z0-9_.-] 字符统一替换
   为 "_",消毒结果为空 / "." / ".." 回退 anonymous),落盘文件名是
   服务端生成的 uuid4().hex + 白名单后缀,原始文件名从不落盘——穿越
   载荷("../"、反斜杠等)在目录名与文件名两层都无法形成路径分隔符,
   从根上杜绝穿越;
2. 下载:file_id 必须匹配 [A-Za-z0-9_.-]+ 且不是 "." / "..",不匹配
   一律 404;user_key 在 GET 侧按当前 X-User-Id 消毒得出(与上传同源)。
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Annotated, Any, NoReturn
from uuid import NAMESPACE_URL, uuid4, uuid5

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from langchain_core.messages import BaseMessage
from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse

from api.schemas import (
    ApiErrorCode,
    Attachment,
    ErrorDetail,
    ErrorResponse,
    FileUploadResponse,
)
from api.sessions import current_user_id
from core.state import GeneratedFile, message_generated_files

router = APIRouter(prefix="/files", tags=["files"])
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse},
    status.HTTP_404_NOT_FOUND: {"model": ErrorResponse},
    status.HTTP_500_INTERNAL_SERVER_ERROR: {"model": ErrorResponse},
}
# D7-T1 上传限制:扩展名白名单是服务端校验(文件名小写后缀;浏览器/
# 客户端可伪造 content-type,白名单以扩展名为准——与 D6-T5 的
# knowledge.py 同一约定);大小上限在逐块读取时累计拦截,不能信
# Content-Length(客户端可谎报),超限即 422。
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
# 教学项目白名单:txt(消息内文本展示)/ png / jpg / jpeg(图片内联
# 预览)/ pdf(下载)足够 D7「上传-随消息附带给 Agent-消息内渲染」
# 闭环;识别/解析类高级能力(手写公式识别、语音)不在本期范围。
ALLOWED_UPLOAD_EXTENSIONS = frozenset({".pdf", ".png", ".jpg", ".jpeg", ".txt"})
# 扩展名 → content_type 映射:上传响应与下载响应的类型同源于此,
# 不信任客户端伪造的 content-type 字段。
_UPLOAD_CONTENT_TYPES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".txt": "text/plain",
    # T5-3：officecli 生成文件的下载类型（上传白名单仍不收这些扩展名——
    # ALLOWED_UPLOAD_EXTENSIONS 在上传入口先行拦截，本表只影响下载响应）。
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
# 目录名 / URL 路径段的安全字符集:上传消毒与下载校验共用同一口径。
_UNSAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9_.-]")
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
# 仓库根 data/uploads(与 app.py 的 _REPO_ROOT 同一惯例,见 app.py
# 52-61 行注释:默认路径必须解析到仓库根 data/,不能写成相对启动
# 工作目录的 "data/uploads"——uvicorn 与 pytest 都在 backend/ 下
# 启动,相对路径会落到 backend/data/)。
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_UPLOADS_PATH = str(_REPO_ROOT / "data" / "uploads")


def _raise_error(status_code: int, error_code: ApiErrorCode, message: str) -> NoReturn:
    """与 api/feedback._raise_error 同构:抛出标准 ErrorResponse 体。"""
    detail = ErrorDetail(error_code=error_code, message=message)
    raise HTTPException(status_code=status_code, detail=detail.model_dump(mode="json"))


def _sanitize_user_key(user_id: str | None) -> str:
    """把 X-User-Id 消毒为磁盘目录名(防路径穿越第一道防线)。

    - None(匿名上传)→ "anonymous";
    - 非安全字符统一替换为 "_":X-User-Id 可能携带 "../"、反斜杠等
      穿越载荷,消毒后只剩 [A-Za-z0-9_.-],不可能形成路径分隔符;
    - **纯 "." / ".." 不消毒**(点号在安全字符集内),但它们会让
      {root}/.. 折叠到上传根之外——消毒结果为空 / "." / ".." 时
      回退 "anonymous"(review blocking 修复,上传/下载双端同源);
    - 不同原始 ID 可能消毒成同一 key(如 "a/b" 与 "a_b")——教学项目
      可接受,不做碰撞规避。
    """
    key = _UNSAFE_CHARS_RE.sub("_", user_id) if user_id is not None else "anonymous"
    if not key or key in (".", ".."):
        return "anonymous"
    return key


def _is_safe_segment(value: str) -> bool:
    """URL 路径段安全校验(下载防线):只允许 [A-Za-z0-9_.-],且拒绝 "." / ".."。

    正则允许点号与连字符,单独的 "." / ".." 段必须显式拒绝——它们会让
    {root}/{user_key}/.. 解析到上一级目录,绕过用户隔离与根目录边界。
    """
    return value not in (".", "..") and _SAFE_SEGMENT_RE.fullmatch(value) is not None


# T5-3：生成文件下载回执的确定性 file_id 命名空间。file_id 由
# 「user_key + 授权路径 + 写入时刻 size/mtime_ns」派生——同一文件多轮
# 修改各是各的回执（版本化）；重复 serve（实时响应与历史刷新）命中同一
# ID，已落盘的拷贝不重复复制（幂等）。
_GENERATED_FILE_ID_MATERIAL = "agents-tutor-generated-file"
# 单条消息最多挂出的生成文件附件数（防异常清单撑爆响应）。
_MAX_GENERATED_ATTACHMENTS = 4
# 生成文件只允许这三类 Office 扩展名（与 core 侧 OFFICE_EXTENSIONS 同源
# 口径；防御一层，防止脏元数据把任意后缀的文件引入下载目录）。
_GENERATED_FILE_SUFFIXES = frozenset({".docx", ".xlsx", ".pptx"})


def _uploads_root() -> Path:
    """上传根目录:env API_UPLOAD_DIR,默认仓库根 data/uploads。

    env 模式与 D6-T5 的 API_*_PATH 一致(start-stage3.ps1 注入绝对
    路径;测试用 monkeypatch 指到 tmp_path);每次调用现读 env,测试
    隔离与运行时重配置都自然生效。任务文件(D7-T1)指定的环境变量名
    是 API_UPLOAD_DIR。
    """
    return Path(os.getenv("API_UPLOAD_DIR", DEFAULT_UPLOADS_PATH))


def _save_upload(root: Path, user_key: str, disk_name: str, data: bytes) -> None:
    """把上传字节写入 {root}/{user_key}/{disk_name}(同步,线程池内跑)。

    - mkdir parents=True + exist_ok=True:首次上传自动建目录;
    - disk_name 由 API 层生成(uuid4().hex + 白名单后缀),原文件名从不
      落盘——文件名层从根上杜绝穿越(见模块 docstring);
    - 分块写(1MB):10MB 上限一次性写也可接受,分块与「逐块读累计
      校验」同构,避免一次性大内存拷贝。
    """
    target = root / user_key / disk_name
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as fh:
        for offset in range(0, len(data), 1024 * 1024):
            fh.write(data[offset : offset + 1024 * 1024])


@router.post(
    "",
    response_model=FileUploadResponse,
    responses=ERROR_RESPONSES,
    status_code=status.HTTP_201_CREATED,
)
async def upload_file(
    file: UploadFile,
    user_id: Annotated[str | None, Depends(current_user_id)],
) -> FileUploadResponse:
    """上传聊天附件(按用户隔离存储),返回受控下载回执。

    - 扩展名白名单 / 大小上限 / 空文件都在 API 层拦截为 422(大小在
      逐块读取时累计,不能信 Content-Length);
    - 存储目录按 user_key 隔离(消毒后的 X-User-Id,None → anonymous);
      落盘名是 uuid4().hex + 白名单后缀,原始文件名只作展示字段返回;
    - 同步写盘走 run_in_threadpool,不阻塞事件循环;写盘失败(OSError
      等)统一映射 500 internal_error,不泄底层细节。
    """
    # 文件名可能带浏览器假路径前缀("C:\\fakepath\\x.txt" 或
    # "/tmp/x.txt"),先剥掉一切分隔符得到纯文件名,再取小写后缀
    # (与 knowledge.py 同一约定)。
    basename = (file.filename or "").replace("\\", "/").rsplit("/", maxsplit=1)[-1]
    ext = Path(basename).suffix.lower()
    if ext not in ALLOWED_UPLOAD_EXTENSIONS:
        _raise_error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            ApiErrorCode.INVALID_REQUEST,
            "Unsupported file type.",
        )
    # 逐块读取并累计大小(上限 10MB,一次性读入内存可接受):不能信
    # Content-Length(客户端可谎报),超限立即 422,不再继续读。
    data = bytearray()
    while True:
        block = await file.read(1024 * 1024)
        if not block:
            break
        data.extend(block)
        if len(data) > MAX_UPLOAD_BYTES:
            _raise_error(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                ApiErrorCode.INVALID_REQUEST,
                "File is too large.",
            )
    if not data:
        _raise_error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            ApiErrorCode.INVALID_REQUEST,
            "File is empty.",
        )
    user_key = _sanitize_user_key(user_id)
    disk_name = uuid4().hex + ext
    try:
        await run_in_threadpool(
            _save_upload,
            _uploads_root(),
            user_key,
            disk_name,
            bytes(data),
        )
    except Exception:  # noqa: BLE001 - 服务边界只暴露稳定错误码,不泄底层细节
        _raise_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            ApiErrorCode.INTERNAL_ERROR,
            "The request could not be completed.",
        )
    return FileUploadResponse(
        file_id=disk_name,
        name=basename,
        content_type=_UPLOAD_CONTENT_TYPES[ext],
        size=len(data),
        url=f"/files/{disk_name}",
    )


@router.get(
    "/{file_id}",
    responses=ERROR_RESPONSES,
)
async def get_uploaded_file(
    file_id: str,
    user_id: Annotated[str | None, Depends(current_user_id)],
) -> FileResponse:
    """按受控下载路径提供已上传文件(用户隔离校验)。

    - file_id 必须是安全段(见 _is_safe_segment),不匹配一律 404;
    - 存储路径 {root}/{user_key}/{file_id}:user_key 由当前 X-User-Id
      消毒得出(与上传同源)——他人目录下的 uuid 文件名不可枚举,
      等价于文件不存在,统一 404(不返回 403,避免泄露目录存在性);
    - media_type 按扩展名映射,与上传响应的 content_type 同源
      (_UPLOAD_CONTENT_TYPES);底层读盘异常映射 500。
    """
    if not _is_safe_segment(file_id):
        _raise_error(
            status.HTTP_404_NOT_FOUND,
            ApiErrorCode.INVALID_REQUEST,
            "File was not found.",
        )
    user_key = _sanitize_user_key(user_id)
    path = _uploads_root() / user_key / file_id
    if not path.is_file():
        _raise_error(
            status.HTTP_404_NOT_FOUND,
            ApiErrorCode.INVALID_REQUEST,
            "File was not found.",
        )
    ext = Path(file_id).suffix.lower()
    try:
        return FileResponse(path, media_type=_UPLOAD_CONTENT_TYPES.get(ext))
    except OSError:
        _raise_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            ApiErrorCode.INTERNAL_ERROR,
            "The request could not be completed.",
        )


def attachments_for_generated_files(
    user_id: str | None,
    message: BaseMessage,
) -> list[Attachment] | None:
    """把 core 消息元数据中的生成文件清单注册为受控下载附件（T5-3）。

    - 读取端宽容：无 generated_files 元数据 → None（与「无附件就不携带」
      的契约一致，前端零渲染）；逐项注册失败（文件已被移走/占用等）跳过，
      不让单个坏项击穿整条消息的映射；
    - 注册是把工作区文件复制进上传目录（用户隔离不变，下载复用现有
      GET /files/{file_id} 通道），复制在首次 serve 时惰性发生——实时
      响应与历史刷新共用本函数，幂等。
    """
    entries = message_generated_files(message)
    if not entries:
        return None
    user_key = _sanitize_user_key(user_id)
    attachments: list[Attachment] = []
    for entry in entries[:_MAX_GENERATED_ATTACHMENTS]:
        attachment = _register_generated_file(user_key, entry)
        if attachment is not None:
            attachments.append(attachment)
    return attachments or None


def _register_generated_file(user_key: str, entry: GeneratedFile) -> Attachment | None:
    """把单个生成文件复制进用户上传目录并返回附件回执；失败返回 None。

    - file_id 确定性派生（版本化：同一文件多轮修改各是各的回执）；
    - 只接受三类 Office 扩展名（防御脏元数据把任意文件引入下载目录）；
    - 复制经临时文件 + os.replace 原子落盘，并发重复注册不会得到半个文件。
    """
    suffix = Path(entry.name).suffix.lower()
    if suffix not in _GENERATED_FILE_SUFFIXES:
        return None
    source = Path(entry.path)
    try:
        if not source.is_file():
            return None
    except OSError:
        return None
    file_id = (
        uuid5(
            NAMESPACE_URL,
            f"{_GENERATED_FILE_ID_MATERIAL}:{user_key}:{entry.path}:"
            f"{entry.size}:{entry.mtime_ns}",
        ).hex
        + suffix
    )
    target = _uploads_root() / user_key / file_id
    if not target.exists():
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            staging = target.with_name(f"{target.name}.{uuid4().hex}.tmp")
            shutil.copy2(source, staging)
            os.replace(staging, target)
        except OSError:
            return None
    return Attachment(
        file_id=file_id,
        name=entry.name,
        content_type=_UPLOAD_CONTENT_TYPES[suffix],
        size=entry.size,
    )
