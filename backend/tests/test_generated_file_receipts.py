"""T5-3 生成文件下载回执测试。

覆盖三层：
- core.state：with_generated_files / message_generated_files 的写入与宽容读取；
- core.graph_builder._attach_generated_files：从 officecli_edit 成功结果
  收集清单并挂到本轮终端回答消息；
- api.files.attachments_for_generated_files：把工作区文件注册为受控下载
  附件（幂等、版本化 file_id），并经 GET /files/{file_id} 真实下载。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pytest import MonkeyPatch

from api.app import create_app
from api.files import attachments_for_generated_files
from core.events import ErrorCode
from core.graph_builder import _attach_generated_files
from core.state import (
    AgentRole,
    GeneratedFile,
    ToolResult,
    message_generated_files,
    with_generated_files,
)


def _entry(path: Path, *, size: int | None = None, mtime_ns: int | None = None) -> GeneratedFile:
    """构造与 office 工具输出同构的回执元数据。"""
    stat = path.stat()
    return GeneratedFile(
        path=str(path),
        name=path.name,
        size=stat.st_size if size is None else size,
        mtime_ns=stat.st_mtime_ns if mtime_ns is None else mtime_ns,
    )


# ── core.state：写入与宽容读取 ────────────────────────────────────


def test_generated_files_metadata_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "成绩单.xlsx"
    target.write_bytes(b"xlsx")
    message = AIMessage(content="已生成成绩单")

    updated = with_generated_files(message, [_entry(target)])

    assert message.additional_kwargs == {}  # 原对象不被修改（副本语义）
    restored = message_generated_files(updated)
    assert restored is not None
    assert restored[0].path == str(target)
    assert restored[0].name == "成绩单.xlsx"


def test_generated_files_empty_list_is_not_attached(tmp_path: Path) -> None:
    target = tmp_path / "a.xlsx"
    target.write_bytes(b"x")
    message = AIMessage(content="无生成文件")

    unchanged = with_generated_files(message, [])

    assert unchanged is message
    assert message_generated_files(unchanged) is None


def test_message_generated_files_tolerates_dirty_metadata() -> None:
    message = AIMessage(content="脏数据")
    dirty = message.model_copy(
        update={
            "additional_kwargs": {
                "generated_files": ["junk", {"path": 1}, {"path": "x", "name": "n", "size": 0, "mtime_ns": 0}],
            }
        }
    )

    restored = message_generated_files(dirty)

    # 非法项跳过、合法项保留；读取端永不崩溃
    assert restored is not None and len(restored) == 1
    assert message_generated_files(AIMessage(content="无键")) is None


# ── graph_builder：从工具结果挂载到终端回答 ──────────────────────


def _office_edit_result(output: dict[str, object], *, success: bool = True) -> ToolResult:
    return ToolResult(
        tool_call_id="call-1",
        tool_name="officecli_edit",
        agent_role=AgentRole.SUPERVISOR,
        success=success,
        output=json.dumps(output, ensure_ascii=False),
        error=None if success else "工具执行失败",
        error_code=None if success else ErrorCode.TOOL_EXECUTION_FAILED,
        duration_ms=1.0,
    )


def test_attach_generated_files_to_terminal_answer(tmp_path: Path) -> None:
    target = tmp_path / "报告.docx"
    target.write_bytes(b"docx")
    entry = _entry(target)
    messages = [
        HumanMessage(content="生成报告"),
        AIMessage(
            content="",
            tool_calls=[{"name": "officecli_edit", "args": {}, "id": "call-1"}],
        ),
        ToolMessage(content="ok", tool_call_id="call-1"),
        AIMessage(content="报告已生成"),
    ]
    results = [
        _office_edit_result(
            {"ok": True, "generated_files": [entry.model_dump(mode="json")]}
        )
    ]

    updated, attached = _attach_generated_files(messages, results)

    # 只挂到最后一条无 tool_calls 的 AIMessage；工具调用消息不挂
    assert attached is True
    assert message_generated_files(updated[3]) is not None
    assert message_generated_files(updated[1]) is None


def test_attach_generated_files_skips_failed_or_unrelated_results(tmp_path: Path) -> None:
    target = tmp_path / "a.xlsx"
    target.write_bytes(b"x")
    entry = _entry(target)
    messages = [AIMessage(content="回答")]
    failed = _office_edit_result(
        {"ok": False, "generated_files": [entry.model_dump(mode="json")]},
        success=False,
    )
    other_tool = ToolResult(
        tool_call_id="call-2",
        tool_name="search_knowledge",
        agent_role=AgentRole.SUPERVISOR,
        success=True,
        output=json.dumps({"generated_files": [entry.model_dump(mode="json")]}),
        error=None,
        error_code=None,
        duration_ms=1.0,
    )

    updated, attached = _attach_generated_files(messages, [failed, other_tool])

    # 失败结果与非 officecli_edit 工具的结果都不产生回执
    assert attached is False
    assert message_generated_files(updated[0]) is None


def test_attach_generated_files_dedupes_same_path(tmp_path: Path) -> None:
    target = tmp_path / "a.xlsx"
    target.write_bytes(b"x")
    entry = _entry(target)
    messages = [AIMessage(content="回答")]
    results = [
        _office_edit_result({"ok": True, "generated_files": [entry.model_dump(mode="json")]}),
        _office_edit_result({"ok": True, "generated_files": [entry.model_dump(mode="json")]}),
    ]

    updated, _attached = _attach_generated_files(messages, results)

    restored = message_generated_files(updated[0])
    assert restored is not None and len(restored) == 1


# ── api.files：注册为受控下载附件并可真实下载 ────────────────────


def test_attachments_for_generated_files_registers_and_downloads(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    uploads = tmp_path / "uploads"
    monkeypatch.setenv("API_UPLOAD_DIR", str(uploads))
    source_dir = tmp_path / "workspace"
    source_dir.mkdir()
    source = source_dir / "成绩单.xlsx"
    source.write_bytes(b"xlsx-content")
    message = with_generated_files(AIMessage(content="已生成"), [_entry(source)])

    attachments = attachments_for_generated_files("user-1", message)

    assert attachments is not None and len(attachments) == 1
    attachment = attachments[0]
    assert attachment.name == "成绩单.xlsx"
    assert (
        attachment.content_type
        == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert attachment.size == source.stat().st_size
    # 文件已复制进用户隔离目录
    assert (uploads / "user-1" / attachment.file_id).read_bytes() == b"xlsx-content"

    # 真实下载：复用 GET /files/{file_id} 通道
    async def download() -> bytes:
        transport = ASGITransport(app=create_app())
        async with AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await client.get(
                f"/files/{attachment.file_id}",
                headers={"X-User-Id": "user-1"},
            )
            assert response.status_code == 200
            return response.content

    assert asyncio.run(download()) == b"xlsx-content"

    # 幂等：重复注册返回同一 file_id，不重复复制
    again = attachments_for_generated_files("user-1", message)
    assert again is not None and again[0].file_id == attachment.file_id


def test_attachments_for_generated_files_versioned_by_content_version(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("API_UPLOAD_DIR", str(tmp_path / "uploads"))
    source = tmp_path / "a.docx"
    source.write_bytes(b"v1")
    message_v1 = with_generated_files(
        AIMessage(content="v1"),
        [_entry(source, size=100, mtime_ns=111)],
    )
    message_v2 = with_generated_files(
        AIMessage(content="v2"),
        [_entry(source, size=200, mtime_ns=222)],
    )

    first = attachments_for_generated_files("user-1", message_v1)
    second = attachments_for_generated_files("user-1", message_v2)

    # 同一文件的不同写入版本各是各的回执（file_id 不同）
    assert first is not None and second is not None
    assert first[0].file_id != second[0].file_id


def test_attachments_for_generated_files_skips_missing_and_bad_suffix(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("API_UPLOAD_DIR", str(tmp_path / "uploads"))
    missing = tmp_path / "gone.xlsx"
    bad_suffix = tmp_path / "evil.exe"
    bad_suffix.write_bytes(b"exe")
    message = with_generated_files(
        AIMessage(content="回答"),
        [
            GeneratedFile(
                path=str(missing), name="gone.xlsx", size=1, mtime_ns=1
            ),
            GeneratedFile(
                path=str(bad_suffix), name="evil.exe", size=3, mtime_ns=1
            ),
        ],
    )

    assert attachments_for_generated_files("user-1", message) is None
    # 无元数据消息 → None（前端零渲染）
    assert attachments_for_generated_files("user-1", AIMessage(content="空")) is None


def test_attachments_for_generated_files_isolate_users(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    uploads = tmp_path / "uploads"
    monkeypatch.setenv("API_UPLOAD_DIR", str(uploads))
    source = tmp_path / "表.xlsx"
    source.write_bytes(b"data")
    message = with_generated_files(AIMessage(content="已生成"), [_entry(source)])

    attachments = attachments_for_generated_files("user-1", message)
    assert attachments is not None

    # 他人用户下载该 file_id → 404（用户隔离语义与上传文件一致）
    async def download_as_other() -> int:
        transport = ASGITransport(app=create_app())
        async with AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await client.get(
                f"/files/{attachments[0].file_id}",
                headers={"X-User-Id": "user-2"},
            )
            return response.status_code

    assert asyncio.run(download_as_other()) == 404
    assert (uploads / "user-1" / attachments[0].file_id).is_file()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
