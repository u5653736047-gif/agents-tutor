"""附件进上下文测试（六大功能计划 P2-7 验收）。

覆盖（pi 审查 🟡5 四路径 + 零回归）：
1. 无附件时消息逐字节一致（零回归红线）；
2. txt 直读拼入消息（端到端：chat 端点 → 图收到的 user_input 含
   附件文本）；
3. 跨用户 file_id 拒绝（他人文件内容不进消息，不泄露存在性）;
4. OCR 可用时图片提取文本进消息（标注机器识别误差提示）；
5. OCR 不可用时友好提示而非报错；
6. 超限截断（单附件上限 + 总量上限，标注「已截断」）。

纯函数层直接测 compose_message_with_attachments；端到端复用
test_chat_api.py 的 ChatGraph 替身 + test_uploads_api.py 的
ASGITransport + tmp_path + monkeypatch API_UPLOAD_DIR 模式。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from pytest import MonkeyPatch

from api.app import create_app
from api.attachments import compose_message_with_attachments
from api.schemas import Attachment
from core.sessions import SessionStore


class _FakeOcr:
    """OCR 替身：固定返回识别文本。"""

    def extract_text(self, image_bytes: bytes) -> str:
        return "手写答案：42"


def _attachment(file_id: str, name: str) -> Attachment:
    return Attachment(file_id=file_id, name=name, content_type=None, size=1)


def _write_user_file(
    root: Path, user_key: str, file_id: str, content: bytes
) -> None:
    target = root / user_key / file_id
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


# ── 纯函数层：提取与组装规则 ──────────────────────────────


def test_no_attachments_returns_message_verbatim(tmp_path: Path) -> None:
    assert compose_message_with_attachments("你好", None, "u1", None) == "你好"
    assert compose_message_with_attachments("你好", [], "u1", None) == "你好"


def test_txt_attachment_text_enters_message(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("API_UPLOAD_DIR", str(tmp_path))
    _write_user_file(tmp_path, "student-a", "abc123.txt", "1. 答案：42".encode())

    composed = compose_message_with_attachments(
        "请批改", [_attachment("abc123.txt", "作业.txt")], "student-a", None
    )

    assert composed.startswith("请批改")
    assert "[附件 1：作业.txt]" in composed
    assert "1. 答案：42" in composed


def test_cross_user_file_id_is_rejected_silently(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """他人目录下的文件内容不进消息（用户隔离），附不可用标注。"""
    monkeypatch.setenv("API_UPLOAD_DIR", str(tmp_path))
    _write_user_file(tmp_path, "student-b", "other99.txt", "别人的作业".encode())

    composed = compose_message_with_attachments(
        "请批改", [_attachment("other99.txt", "作业.txt")], "student-a", None
    )

    assert "别人的作业" not in composed
    assert "该附件不可用" in composed


def test_image_uses_ocr_provider_when_available(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("API_UPLOAD_DIR", str(tmp_path))
    _write_user_file(tmp_path, "student-a", "img001.png", b"fake-bytes")

    composed = compose_message_with_attachments(
        "请批改", [_attachment("img001.png", "作业照片.png")], "student-a", _FakeOcr()
    )

    assert "手写答案：42" in composed
    assert "机器识别文本，可能存在识别误差" in composed


def test_image_without_ocr_returns_friendly_hint(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("API_UPLOAD_DIR", str(tmp_path))
    _write_user_file(tmp_path, "student-a", "img002.jpg", b"fake-bytes")

    composed = compose_message_with_attachments(
        "请批改", [_attachment("img002.jpg", "照片.jpg")], "student-a", None
    )

    # S5-B3 三级降级链末级：VLM 与 OCR 均不可用时的友好提示。
    assert "当前部署未启用图片理解" in composed


def test_per_attachment_limit_truncates_with_marker(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("API_UPLOAD_DIR", str(tmp_path))
    monkeypatch.setenv("API_ATTACHMENT_MAX_CHARS", "100")
    _write_user_file(tmp_path, "student-a", "big.txt", ("长" * 500).encode())

    # 文件名避开「长」字，防止干扰截断字符计数断言
    composed = compose_message_with_attachments(
        "请批改", [_attachment("big.txt", "大文件.txt")], "student-a", None
    )

    assert composed.count("长") == 100
    assert "[已截断，仅前 100 字符参与处理]" in composed


def test_total_limit_truncates_second_attachment(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("API_UPLOAD_DIR", str(tmp_path))
    monkeypatch.setenv("API_ATTACHMENT_MAX_CHARS", "1000")
    monkeypatch.setenv("API_ATTACHMENTS_TOTAL_MAX_CHARS", "150")
    _write_user_file(tmp_path, "student-a", "a1.txt", ("甲" * 120).encode())
    _write_user_file(tmp_path, "student-a", "a2.txt", ("乙" * 120).encode())

    composed = compose_message_with_attachments(
        "请批改",
        [_attachment("a1.txt", "一.txt"), _attachment("a2.txt", "二.txt")],
        "student-a",
        None,
    )

    assert composed.count("甲") == 120
    # 第二个附件只剩 30 字符额度（150-120），随后截断标注
    assert composed.count("乙") == 30
    assert "附件总量上限 150 字符" in composed


def test_attachment_count_is_capped_with_overflow_note(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """审查 W2：附件数量上限 10，超限部分合并为单条标注——
    「附件不可用」标注段落总量也有界，不能绕过字符护栏放大消息。"""
    monkeypatch.setenv("API_UPLOAD_DIR", str(tmp_path))
    # 12 条不存在的 file_id：前 10 条各产出不可用标注，后 2 条被截断
    attachments = [
        _attachment(f"ghost{i:02d}.txt", f"附件{i:02d}.txt") for i in range(12)
    ]

    composed = compose_message_with_attachments(
        "请批改", attachments, "student-a", None
    )

    # 只处理前 10 个（编号 1-10），超限 2 个合并为单条标注
    assert "[附件 10：" in composed
    assert "[附件 11：" not in composed
    assert "已忽略 2 个超出数量上限的附件" in composed
    # 段落总量有界：不可用标注不绕过字符护栏（总长受控）
    assert len(composed) < 3000


def test_contract_rejects_more_than_ten_attachments(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """审查 W2：契约层 max_length=10，超限请求 422。"""
    monkeypatch.setenv("API_UPLOAD_DIR", str(tmp_path / "uploads"))
    app = create_app()
    app.state.session_store = SessionStore(tmp_path / "sessions.sqlite3")
    app.state.graph = _RecordingGraph()
    app.state.ocr_provider = None

    async def _scenario() -> int:
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await client.post(
                "/chat",
                headers={"X-User-Id": "student-a"},
                json={
                    "session_id": "session-overflow",
                    "message": "请批改",
                    "attachments": [
                        {
                            "file_id": f"f{i:02d}.txt",
                            "name": f"x{i:02d}.txt",
                            "content_type": None,
                            "size": 1,
                        }
                        for i in range(11)
                    ],
                },
            )
            return response.status_code

    assert asyncio.run(_scenario()) == 422


# ── 端到端：chat 端点消费 attachments ─────────────────────


class _RecordingGraph:
    """记录 run 收到的 user_input（复用 test_chat_api.ChatGraph 口径）。"""

    def __init__(self) -> None:
        self.run_inputs: list[tuple[str, str, str | None]] = []

    def get_state(self, session_id: str, user_id: str | None = None) -> None:
        return None

    def run(
        self, user_input: str, session_id: str, user_id: str | None = None
    ) -> dict[str, Any]:
        self.run_inputs.append((user_input, session_id, user_id))
        return {
            "messages": [AIMessage(content="已收到")],
            "events": [],
            "current_agent": "supervisor",
            "run_error": None,
            "pending_handoff": None,
        }

    def get_pending_handoff(
        self, session_id: str, user_id: str | None = None
    ) -> None:
        return None


def test_chat_endpoint_composes_attachment_into_graph_input(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    uploads_root = tmp_path / "uploads"
    monkeypatch.setenv("API_UPLOAD_DIR", str(uploads_root))
    _write_user_file(uploads_root, "student-a", "hw001.txt", "答题正文：选A".encode())

    app = create_app()
    app.state.session_store = SessionStore(tmp_path / "sessions.sqlite3")
    graph = _RecordingGraph()
    app.state.graph = graph
    app.state.ocr_provider = None

    async def _scenario() -> None:
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await client.post(
                "/chat",
                headers={"X-User-Id": "student-a"},
                json={
                    "session_id": "session-attach",
                    "message": "请批改我的作业",
                    "attachments": [
                        {
                            "file_id": "hw001.txt",
                            "name": "作业.txt",
                            "content_type": None,
                            "size": 1,
                        }
                    ],
                },
            )
            assert response.status_code == 200

    asyncio.run(_scenario())

    assert len(graph.run_inputs) == 1
    user_input = graph.run_inputs[0][0]
    assert user_input.startswith("请批改我的作业")
    assert "答题正文：选A" in user_input
    assert "[附件 1：作业.txt]" in user_input


def test_chat_endpoint_without_attachments_is_unchanged(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("API_UPLOAD_DIR", str(tmp_path / "uploads"))
    app = create_app()
    app.state.session_store = SessionStore(tmp_path / "sessions.sqlite3")
    graph = _RecordingGraph()
    app.state.graph = graph
    app.state.ocr_provider = None

    async def _scenario() -> None:
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await client.post(
                "/chat",
                headers={"X-User-Id": "student-a"},
                json={"session_id": "session-plain", "message": "普通问题"},
            )
            assert response.status_code == 200

    asyncio.run(_scenario())

    # 零回归：无附件时图收到的消息与原消息逐字节一致
    assert graph.run_inputs[0][0] == "普通问题"
