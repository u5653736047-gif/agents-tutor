"""D7-T1 聊天附件上传端点测试:POST /files + GET /files/{file_id}。

覆盖范围:
- 合法上传:201 + 回执(url 为受控相对路径 / content_type / size /
  name),文件真实落盘到 API_UPLOAD_DIR 下,内容一致;
- 校验拦截:白名单外扩展名 / 超过大小上限(monkeypatch 小上限)/
  空文件 → 422 invalid_request,均在 API 层拦截;
- 用户隔离:不同 X-User-Id 落盘到不同 user_key 目录,文件互不可见;
  无头请求 → anonymous 目录;
- 路径穿越防护:X-User-Id 携带 "../" 或反斜杠被消毒(非安全字符替换
  为 "_"),落盘目录不构成穿越;下载路径段(file_id 非法)拒绝;
- 下载可达:上传后 GET url → 200 + 内容与 content-type 一致;他人
  user_key(取当前 X-User-Id 消毒目录)下不存在该 file_id → 404;
  非法 file_id 段 → 404;
- OpenAPI:FileUploadResponse/Attachment 契约与两个路径可见。

测试复用 D6-T5 的 ASGITransport + tmp_path + monkeypatch env
(API_UPLOAD_DIR)模式;create_app() 不跑 lifespan,files 路由
不依赖 lifespan 装配。
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from pytest import MonkeyPatch

from api.app import create_app


async def _upload(
    app: FastAPI,
    filename: str,
    content: bytes,
    content_type: str = "application/octet-stream",
    *,
    user_id: str | None = None,
) -> Response:
    transport = ASGITransport(app=app)
    headers = {} if user_id is None else {"X-User-Id": user_id}
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post(
            "/files",
            headers=headers,
            files={"file": (filename, content, content_type)},
        )


async def _get(app: FastAPI, url: str, user_id: str | None = None) -> Response:
    transport = ASGITransport(app=app)
    headers = {} if user_id is None else {"X-User-Id": user_id}
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get(url, headers=headers)


def _uploads_root(tmp_path: Path, monkeypatch: MonkeyPatch) -> Path:
    """把 API_UPLOAD_DIR 指到 tmp_path 并返回上传根目录(测试隔离)。"""
    root = tmp_path / "files"
    monkeypatch.setenv("API_UPLOAD_DIR", str(root))
    return root


# ── 正常上传路径 ───────────────────────────────────────────────────


def test_upload_txt_persists_and_returns_receipt(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    root = _uploads_root(tmp_path, monkeypatch)
    content = "附件内容:一元二次方程。".encode()

    response = asyncio.run(
        _upload(create_app(), "notes.txt", content, user_id="user-1")
    )

    assert response.status_code == 201
    body = response.json()
    assert body["size"] == len(content)
    assert body["content_type"] == "text/plain"
    assert body["name"] == "notes.txt"
    # url 是受控相对路径:/files/{uuid 落盘名}(用户隔离在 GET 时按
    # 当前 X-User-Id 消毒目录解析)。
    assert re.fullmatch(r"/files/[0-9a-f]{32}\.txt", body["url"]) is not None

    # 文件真实落盘,内容一致。
    disk_name = body["url"].rsplit("/", maxsplit=1)[-1]
    stored = root / "user-1" / disk_name
    assert stored.is_file()
    assert stored.read_bytes() == content


def test_upload_strips_path_prefix_from_filename(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """浏览器式假路径文件名不泄漏:filename 只取纯文件名(防御回归)。"""
    _uploads_root(tmp_path, monkeypatch)

    response = asyncio.run(
        _upload(create_app(), r"C:\fakepath\notes.txt", b"x", user_id="user-1")
    )

    assert response.status_code == 201
    assert response.json()["name"] == "notes.txt"
    assert "fakepath" not in response.text


# ── 请求校验(422 由 API 层拦截)────────────────────────────────────


def test_upload_rejects_disallowed_extensions(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    root = _uploads_root(tmp_path, monkeypatch)

    for filename in ("notes.exe", "virus.bat", "noext"):
        response = asyncio.run(_upload(create_app(), filename, b"whatever"))

        assert response.status_code == 422
        assert response.json()["detail"]["error_code"] == "invalid_request"
    # 校验失败不产生任何落盘产物。
    assert not root.exists()


def test_upload_rejects_oversized_file(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """大小上限用 monkeypatch 调小,避免在测试里构造 10MB 内存。"""
    root = _uploads_root(tmp_path, monkeypatch)
    monkeypatch.setattr("api.files.MAX_UPLOAD_BYTES", 1024)

    too_big = asyncio.run(_upload(create_app(), "big.txt", b"x" * 2048))
    assert too_big.status_code == 422
    assert too_big.json()["detail"]["error_code"] == "invalid_request"
    # 超限即拒,不产生落盘产物。
    assert not root.exists()

    # 恰好等于上限不误杀。
    at_limit = asyncio.run(_upload(create_app(), "ok.txt", b"y" * 1024))
    assert at_limit.status_code == 201


def test_upload_rejects_empty_file(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    root = _uploads_root(tmp_path, monkeypatch)

    response = asyncio.run(_upload(create_app(), "empty.txt", b""))

    assert response.status_code == 422
    assert response.json()["detail"]["error_code"] == "invalid_request"
    assert not root.exists()


# ── 用户隔离与路径穿越防护 ─────────────────────────────────────────


def test_upload_isolates_users_by_user_key(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    root = _uploads_root(tmp_path, monkeypatch)

    first = asyncio.run(_upload(create_app(), "a.txt", b"aaa", user_id="user-1"))
    second = asyncio.run(_upload(create_app(), "b.txt", b"bbb", user_id="user-2"))

    assert first.status_code == 201
    assert second.status_code == 201
    # url 是 /files/{file_id}:file_id 全局唯一(uuid),用户隔离在下载时
    # 按当前 X-User-Id 消毒目录解析。
    assert first.json()["url"].startswith("/files/")
    assert second.json()["url"].startswith("/files/")

    # 目录隔离:两个用户目录都存在,且文件集合互不相交。
    assert (root / "user-1").is_dir()
    assert (root / "user-2").is_dir()
    names_1 = {p.name for p in (root / "user-1").iterdir()}
    names_2 = {p.name for p in (root / "user-2").iterdir()}
    assert names_1.isdisjoint(names_2)


def test_upload_sanitizes_traversal_user_key(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """X-User-Id 携带 "../" 或反斜杠:消毒后无法构成路径穿越。"""
    root = _uploads_root(tmp_path, monkeypatch)

    for evil in ("../evil", r"..\evil"):
        response = asyncio.run(
            _upload(create_app(), "evil.txt", b"x", user_id=evil)
        )

        assert response.status_code == 201
        url = response.json()["url"]
        # 消毒后目录名不含路径分隔符;url 不出现 ".." 路径段。
        assert "/../" not in url
        assert not url.startswith("..")
        assert url.startswith("/files/")

        # 文件落在消毒后的安全目录内,而不是根目录之外。
        disk_name = url.rsplit("/", maxsplit=1)[-1]
        assert (root / ".._evil" / disk_name).is_file()

    # 根目录下只有消毒后的目录;穿越目标(tmp_path/evil)不存在。
    assert {p.name for p in root.iterdir()} == {".._evil"}
    assert not (root.parent / "evil").exists()


def test_upload_sanitizes_pure_dot_user_keys(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """X-User-Id 为纯 "." / ".." 时回退 anonymous(review blocking 回归)。

    点号在安全字符集内,纯 "." / ".." 消毒后仍是自身——它们会让
    {root}/.. 折叠到上传根之外。消毒必须把它们回退为 anonymous,
    落盘目录只能是 root/anonymous。
    """
    root = _uploads_root(tmp_path, monkeypatch)

    for evil in (".", ".."):
        response = asyncio.run(_upload(create_app(), "dot.txt", b"x", user_id=evil))

        assert response.status_code == 201
        assert response.json()["url"].startswith("/files/")
        disk_name = response.json()["url"].rsplit("/", maxsplit=1)[-1]
        # 落盘在 anonymous 目录,且从未写到上传根之外。
        assert (root / "anonymous" / disk_name).is_file()
        assert not (root.parent / "anonymous").exists()

    # 根下只有 anonymous 目录。
    assert {p.name for p in root.iterdir()} == {"anonymous"}


def test_upload_anonymous_uses_anonymous_dir(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    root = _uploads_root(tmp_path, monkeypatch)

    response = asyncio.run(_upload(create_app(), "anon.txt", b"x"))

    assert response.status_code == 201
    assert response.json()["url"].startswith("/files/")
    assert (root / "anonymous").is_dir()


# ── 受控下载 ───────────────────────────────────────────────────────


def test_uploaded_file_is_reachable_via_url(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    _uploads_root(tmp_path, monkeypatch)
    content = "下载可达:内容一致。".encode()
    uploaded = asyncio.run(
        _upload(create_app(), "notes.txt", content, user_id="user-1")
    )
    assert uploaded.status_code == 201

    fetched = asyncio.run(
        _get(create_app(), uploaded.json()["url"], user_id="user-1")
    )

    assert fetched.status_code == 200
    assert fetched.content == content
    assert fetched.headers["content-type"].startswith("text/plain")


def test_get_other_users_file_returns_404(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """越权下载:他人用户以自己 user_key 目录解析同一 file_id → 404。"""
    _uploads_root(tmp_path, monkeypatch)
    uploaded = asyncio.run(
        _upload(create_app(), "secret.txt", b"s", user_id="user-1")
    )
    assert uploaded.status_code == 201
    file_id = uploaded.json()["url"].rsplit("/", maxsplit=1)[-1]

    # user-2 的 user_key 目录下不存在该 file_id → 404(不返回 403,
    # 避免泄露目录存在性)。
    fetched = asyncio.run(_get(create_app(), f"/files/{file_id}", user_id="user-2"))

    assert fetched.status_code == 404
    assert fetched.json()["detail"]["error_code"] == "invalid_request"

    # 无 X-User-Id(anonymous 目录)同样 404。
    anonymous = asyncio.run(_get(create_app(), f"/files/{file_id}"))
    assert anonymous.status_code == 404


def test_get_missing_file_returns_404(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    _uploads_root(tmp_path, monkeypatch)

    fetched = asyncio.run(
        _get(create_app(), f"/files/{'0' * 32}.txt", user_id="user-1")
    )

    assert fetched.status_code == 404
    assert fetched.json()["detail"]["error_code"] == "invalid_request"


def test_get_rejects_unsafe_file_ids(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    _uploads_root(tmp_path, monkeypatch)

    # 含 ".." 段:httpx 会对 URL 做路径规范化(折叠成 /files/),请求
    # 不匹配路由 → 404(body 为框架默认,不在此断言 ErrorResponse)。
    dotdot = asyncio.run(_get(create_app(), "/files/..", user_id="user-1"))
    assert dotdot.status_code == 404

    # 含路径分隔符的 file_id 不匹配安全段校验 → 404。
    traversal = asyncio.run(
        _get(create_app(), "/files/..%2Fsecret.txt", user_id="user-1")
    )
    assert traversal.status_code == 404

    # 白名单外字符(分号注入)拒绝。
    weird = asyncio.run(_get(create_app(), "/files/x;.txt", user_id="user-1"))
    assert weird.status_code == 404


# ── OpenAPI 契约 ───────────────────────────────────────────────────


def test_openapi_exposes_upload_contracts() -> None:
    openapi = create_app().openapi()
    schemas = openapi["components"]["schemas"]

    upload_schema = schemas["FileUploadResponse"]
    assert set(upload_schema["properties"]) == {
        "file_id",
        "name",
        "content_type",
        "size",
        "url",
    }
    # Attachment 契约扩展同时可见。
    assert set(schemas["Attachment"]["properties"]) == {
        "file_id",
        "name",
        "content_type",
        "size",
    }

    paths = openapi["paths"]
    post = paths["/files"]["post"]
    assert (
        post["responses"]["201"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/FileUploadResponse"
    )
    assert (
        post["responses"]["422"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/ErrorResponse"
    )
    get = paths["/files/{file_id}"]["get"]
    assert (
        get["responses"]["404"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/ErrorResponse"
    )
