"""officecli 工具集成的单元测试（计划 T3-3）。

覆盖：动词/选项/batch 三层白名单、--prop 与 merge --data 的文件引用校验、
存在性规则、token 上限、子进程环境变量与 stdin 关闭、输出截断与超时、
写工具运行时审批门、per-file 锁，以及真实 officecli 集成（无二进制时跳过）。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError
from pytest import MonkeyPatch

from core.filesystem import WorkspaceFileSystem
from core.tools import office_tools
from core.tools.office_tools import (
    OfficeCliSettings,
    approved_office_execution,
    create_office_tools,
    officecli_enabled,
    resolve_officecli_binary,
)

# 假子进程的固定成功返回（工具级测试用，真实子进程由 monkeypatch 拦下）
_FAKE_OK: dict[str, object] = {
    "ok": True,
    "exit_code": 0,
    "stdout": "done",
    "stderr": "",
    "timed_out": False,
    "truncated": False,
}


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    directory = tmp_path / "workspace"
    directory.mkdir()
    return directory


@pytest.fixture
def filesystem(workspace: Path) -> WorkspaceFileSystem:
    return WorkspaceFileSystem(workspace)


@pytest.fixture
def settings() -> OfficeCliSettings:
    return OfficeCliSettings(binary=sys.executable)


@pytest.fixture
def tools(
    filesystem: WorkspaceFileSystem,
    settings: OfficeCliSettings,
) -> tuple[object, object]:
    return create_office_tools(filesystem, settings)


def _capture_runner(
    captured: list[dict[str, object]],
    result: dict[str, object] | None = None,
):
    """构造假 _run_officecli：记录调用参数并返回固定结果。"""

    def fake(
        binary: str,
        argv: list[str],
        *,
        cwd: Path | None,
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> dict[str, object]:
        captured.append(
            {
                "binary": binary,
                "argv": list(argv),
                "cwd": cwd,
                "timeout_seconds": timeout_seconds,
            }
        )
        return dict(result or _FAKE_OK)

    return fake


# ── 1/2. 动词白名单 ──────────────────────────────────────────────


def test_inspect_allows_read_verbs_and_rewrites_file_argument(
    monkeypatch: MonkeyPatch,
    tools: tuple[object, object],
    workspace: Path,
) -> None:
    inspect_tool, _edit_tool = tools
    (workspace / "报告.docx").write_bytes(b"docx")
    captured: list[dict[str, object]] = []
    monkeypatch.setattr(office_tools, "_run_officecli", _capture_runner(captured))

    result = inspect_tool.invoke({"command": ["view", "报告.docx", "text"]})

    assert result["ok"] is True
    argv = captured[0]["argv"]
    # 文件参数被重写为授权绝对路径，verb/mode 原样保留
    assert argv[0] == "view"
    assert argv[1] == str((workspace / "报告.docx").resolve())
    assert argv[2] == "text"


@pytest.mark.parametrize("verb", ["help", "load_skill", "view", "get", "query", "validate"])
def test_inspect_whitelist_allows_all_read_verbs(
    monkeypatch: MonkeyPatch,
    tools: tuple[object, object],
    workspace: Path,
    verb: str,
) -> None:
    inspect_tool, _edit_tool = tools
    (workspace / "a.xlsx").write_bytes(b"xlsx")
    captured: list[dict[str, object]] = []
    monkeypatch.setattr(office_tools, "_run_officecli", _capture_runner(captured))
    arguments = {
        "help": ["help"],
        "load_skill": ["load_skill", "excel"],
        "view": ["view", "a.xlsx", "text"],
        "get": ["get", "a.xlsx"],
        "query": ["query", "a.xlsx", "*"],
        "validate": ["validate", "a.xlsx"],
    }

    result = inspect_tool.invoke({"command": arguments[verb]})

    assert result["ok"] is True, f"{verb} 应在只读白名单内"


def test_inspect_rejects_edit_verbs(
    tools: tuple[object, object],
    workspace: Path,
) -> None:
    inspect_tool, _edit_tool = tools
    (workspace / "a.xlsx").write_bytes(b"xlsx")

    result = inspect_tool.invoke({"command": ["set", "a.xlsx", "/Sheet1/A1"]})

    assert result["ok"] is False
    assert "officecli_edit" in str(result["message"])


def test_edit_rejects_read_verbs(
    tools: tuple[object, object],
    workspace: Path,
) -> None:
    _inspect_tool, edit_tool = tools
    (workspace / "a.xlsx").write_bytes(b"xlsx")

    with approved_office_execution():
        result = edit_tool.invoke({"command": ["view", "a.xlsx", "text"]})

    assert result["ok"] is False
    assert "officecli_inspect" in str(result["message"])


@pytest.mark.parametrize(
    "verb",
    ["mcp", "watch", "raw", "raw-set", "add-part", "unwatch", "open", "save", "close", "dump", "plugins", "refresh", "skills", "install"],
)
def test_dangerous_verbs_are_rejected_by_both_tools(
    tools: tuple[object, object],
    workspace: Path,
    verb: str,
) -> None:
    inspect_tool, edit_tool = tools
    (workspace / "a.xlsx").write_bytes(b"xlsx")

    read_result = inspect_tool.invoke({"command": [verb, "a.xlsx"]})
    with approved_office_execution():
        edit_result = edit_tool.invoke({"command": [verb, "a.xlsx"]})

    # save/close 已移除：错误信息需说明「编辑即时落盘，无需保存动作」
    assert read_result["ok"] is False and "不支持的动词" in str(read_result["message"])
    assert edit_result["ok"] is False and "不支持的动词" in str(edit_result["message"])


# ── 3. 选项白名单（默认拒绝） ────────────────────────────────────


@pytest.mark.parametrize(
    ("command", "reason_hint"),
    [
        (["view", "a.xlsx", "text", "-o", "out.html"], "落盘"),
        (["view", "a.xlsx", "text", "--browser"], "浏览器"),
        (["view", "a.xlsx", "text", "--screenshot-width", "800"], "截图"),
        (["get", "a.xlsx", "/", "--save", "x.png"], "写原语"),
        (["set", "a.xlsx", "/Sheet1/A1", "--force"], "文档保护"),
        (["batch", "a.xlsx", "--input", "cmds.json"], "--commands"),
        (["batch", "a.xlsx", "--stdin"], "stdin"),
        (["batch", "a.xlsx", "--best-effort"], "原子"),
        (["import", "a.xlsx", "/Sheet1", "--file", "d.csv"], "位置参数"),
        (["import", "a.xlsx", "/Sheet1", "--stdin"], "stdin"),
    ],
)
def test_denied_options_are_rejected_with_reasons(
    tools: tuple[object, object],
    workspace: Path,
    command: list[str],
    reason_hint: str,
) -> None:
    inspect_tool, edit_tool = tools
    (workspace / "a.xlsx").write_bytes(b"xlsx")
    (workspace / "d.csv").write_text("x\n", encoding="utf-8")

    # 只读动词走 inspect（无需批准上下文），写入动词走 edit
    if command[0] in {"view", "get"}:
        result = inspect_tool.invoke({"command": command})
    else:
        with approved_office_execution():
            result = edit_tool.invoke({"command": command})

    assert result["ok"] is False
    assert reason_hint in str(result["message"])


def test_unknown_option_is_rejected_with_allowed_list(
    tools: tuple[object, object],
    workspace: Path,
) -> None:
    inspect_tool, _edit_tool = tools
    (workspace / "a.xlsx").write_bytes(b"xlsx")

    result = inspect_tool.invoke({"command": ["view", "a.xlsx", "text", "--bogus", "1"]})

    assert result["ok"] is False
    message = str(result["message"])
    assert "--bogus" in message
    # 错误信息附该动词允许的选项清单（风险表：帮助模型自我纠正）
    assert "--max-lines" in message


def test_flag_option_rejects_inline_value(
    tools: tuple[object, object],
    workspace: Path,
) -> None:
    inspect_tool, _edit_tool = tools
    (workspace / "a.xlsx").write_bytes(b"xlsx")

    result = inspect_tool.invoke({"command": ["validate", "a.xlsx", "--json=true"]})

    assert result["ok"] is False
    assert "开关" in str(result["message"])


# ── 4. batch 子项白名单 ──────────────────────────────────────────


def test_batch_requires_commands_option(
    tools: tuple[object, object],
    workspace: Path,
) -> None:
    _inspect_tool, edit_tool = tools
    (workspace / "a.xlsx").write_bytes(b"xlsx")

    with approved_office_execution():
        result = edit_tool.invoke({"command": ["batch", "a.xlsx"]})

    assert result["ok"] is False
    assert "--commands" in str(result["message"])


@pytest.mark.parametrize("item_verb", ["raw", "raw-set", "add-part", "dump", "create", "import", "merge"])
def test_batch_item_verbs_outside_whitelist_are_rejected(
    tools: tuple[object, object],
    workspace: Path,
    item_verb: str,
) -> None:
    _inspect_tool, edit_tool = tools
    (workspace / "a.xlsx").write_bytes(b"xlsx")
    commands = json.dumps([{"command": item_verb, "path": "/Sheet1/A1"}])

    with approved_office_execution():
        result = edit_tool.invoke({"command": ["batch", "a.xlsx", "--commands", commands]})

    assert result["ok"] is False
    assert "batch 子命令" in str(result["message"])


def test_batch_item_props_src_must_resolve_inside_workspace(
    monkeypatch: MonkeyPatch,
    tools: tuple[object, object],
    workspace: Path,
    tmp_path: Path,
) -> None:
    _inspect_tool, edit_tool = tools
    (workspace / "a.docx").write_bytes(b"docx")
    (workspace / "pic.png").write_bytes(b"png")
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"png")
    captured: list[dict[str, object]] = []
    monkeypatch.setattr(office_tools, "_run_officecli", _capture_runner(captured))

    # 外部绝对路径：拒绝
    bad_commands = json.dumps(
        [{"command": "add", "parent": "/body", "type": "picture", "props": {"src": str(outside)}}]
    )
    with approved_office_execution():
        bad_result = edit_tool.invoke(
            {"command": ["batch", "a.docx", "--commands", bad_commands]}
        )
    assert bad_result["ok"] is False

    # 工作区内路径：放行并重写为授权绝对路径
    good_commands = json.dumps(
        [{"command": "add", "parent": "/body", "type": "picture", "props": {"src": "pic.png"}}]
    )
    with approved_office_execution():
        good_result = edit_tool.invoke(
            {"command": ["batch", "a.docx", "--commands", good_commands]}
        )
    assert good_result["ok"] is True
    rewritten_argv = captured[0]["argv"]
    rewritten_commands = json.loads(rewritten_argv[rewritten_argv.index("--commands") + 1])
    assert rewritten_commands[0]["props"]["src"] == str((workspace / "pic.png").resolve())


# ── 5. --prop 文件引用校验（H1） ─────────────────────────────────


def test_prop_file_keys_are_resolved_and_rewritten(
    monkeypatch: MonkeyPatch,
    tools: tuple[object, object],
    workspace: Path,
) -> None:
    _inspect_tool, edit_tool = tools
    (workspace / "a.docx").write_bytes(b"docx")
    (workspace / "图片.png").write_bytes(b"png")
    captured: list[dict[str, object]] = []
    monkeypatch.setattr(office_tools, "_run_officecli", _capture_runner(captured))

    with approved_office_execution():
        result = edit_tool.invoke(
            {
                "command": [
                    "add",
                    "a.docx",
                    "/body",
                    "--type",
                    "picture",
                    "--prop",
                    "src=图片.png",
                ]
            }
        )

    assert result["ok"] is True
    argv = captured[0]["argv"]
    prop_index = argv.index("--prop")
    assert argv[prop_index + 1] == f"src={(workspace / '图片.png').resolve()}"


def test_prop_path_shaped_values_must_resolve(
    tools: tuple[object, object],
    workspace: Path,
    tmp_path: Path,
) -> None:
    _inspect_tool, edit_tool = tools
    (workspace / "a.docx").write_bytes(b"docx")
    outside = tmp_path / "payload.bin"
    outside.write_bytes(b"bin")

    with approved_office_execution():
        result = edit_tool.invoke(
            {
                "command": [
                    "add",
                    "a.docx",
                    "/body",
                    "--type",
                    "ole",
                    "--prop",
                    f"data={outside}",
                ]
            }
        )

    assert result["ok"] is False
    assert "工作区" in str(result["message"])


@pytest.mark.parametrize(
    "value",
    [
        "TCP/IP 协议",
        "A/B 测试",
        "2026/08/18",
        "输入/输出",
        "Q/K/V 与自注意力",
        "1/2",
        "https://example.com/a",
    ],
)
def test_looks_like_path_does_not_flag_slash_containing_text(value: str) -> None:
    """F1：含单个 / 的教学文本不再被误判为文件引用。"""
    assert office_tools._looks_like_path(value) is False


@pytest.mark.parametrize(
    "value",
    [
        "image.png",           # 资源扩展名
        "data/图.png",         # 含 / 但以资源扩展名结尾
        "/etc/passwd",         # POSIX 绝对路径
        "C:/x",                # 盘符前缀（无扩展名也拦）
        "C:\\payload.bin",
        "\\\\server\\share\\a.xlsx",  # UNC 路径
        "//server/share/a.docx",        # POSIX 风格 UNC
    ],
)
def test_looks_like_path_still_catches_real_path_shapes(value: str) -> None:
    """F1：真实路径形态（盘符/绝对路径/UNC/资源后缀）仍完整覆盖。"""
    assert office_tools._looks_like_path(value) is True


def test_slash_text_props_pass_end_to_end(
    monkeypatch: MonkeyPatch,
    tools: tuple[object, object],
    workspace: Path,
) -> None:
    """F1 复现场景：PPT 正文写入 "Q/K/V 与自注意力" 不再被整批拒绝。"""
    _inspect_tool, edit_tool = tools
    (workspace / "讲义.pptx").write_bytes(b"pptx")
    captured: list[dict[str, object]] = []
    monkeypatch.setattr(office_tools, "_run_officecli", _capture_runner(captured))

    with approved_office_execution():
        result = edit_tool.invoke(
            {
                "command": [
                    "set",
                    "讲义.pptx",
                    "/slide[1]/shape[1]",
                    "--prop",
                    "text=Q/K/V 与自注意力（TCP/IP 与 A/B 测试）",
                ]
            }
        )

    assert result["ok"] is True
    argv = captured[0]["argv"]
    # 普通文本原样透传，不被改写也不被拒绝
    assert argv[argv.index("--prop") + 1] == "text=Q/K/V 与自注意力（TCP/IP 与 A/B 测试）"


def test_batch_view_mode_is_whitelisted(
    tools: tuple[object, object],
    workspace: Path,
) -> None:
    """F2：batch 内 view 子项的 mode 同样受「仅文本模式」冻结决策约束。"""
    _inspect_tool, edit_tool = tools
    (workspace / "a.docx").write_bytes(b"docx")
    html_commands = json.dumps([{"command": "view", "path": "/", "mode": "html"}])

    with approved_office_execution():
        result = edit_tool.invoke(
            {"command": ["batch", "a.docx", "--commands", html_commands]}
        )

    assert result["ok"] is False
    assert "文本模式" in str(result["message"])


def test_batch_view_text_mode_passes(
    monkeypatch: MonkeyPatch,
    tools: tuple[object, object],
    workspace: Path,
) -> None:
    _inspect_tool, edit_tool = tools
    (workspace / "a.docx").write_bytes(b"docx")
    captured: list[dict[str, object]] = []
    monkeypatch.setattr(office_tools, "_run_officecli", _capture_runner(captured))
    text_commands = json.dumps([{"command": "view", "path": "/", "mode": "text"}])

    with approved_office_execution():
        result = edit_tool.invoke(
            {"command": ["batch", "a.docx", "--commands", text_commands]}
        )

    assert result["ok"] is True


def test_prop_plain_text_values_pass(
    monkeypatch: MonkeyPatch,
    tools: tuple[object, object],
    workspace: Path,
) -> None:
    _inspect_tool, edit_tool = tools
    (workspace / "a.xlsx").write_bytes(b"xlsx")
    captured: list[dict[str, object]] = []
    monkeypatch.setattr(office_tools, "_run_officecli", _capture_runner(captured))

    with approved_office_execution():
        result = edit_tool.invoke(
            {"command": ["set", "a.xlsx", "/Sheet1/A1", "--prop", "value=95"]}
        )

    assert result["ok"] is True
    argv = captured[0]["argv"]
    assert argv[argv.index("--prop") + 1] == "value=95"


# ── 6/7. 路径逃逸与存在性规则 ────────────────────────────────────


@pytest.mark.parametrize("escape", ["../outside/a.xlsx", "sub/../../a.xlsx"])
def test_path_traversal_is_rejected(
    tools: tuple[object, object],
    workspace: Path,
    escape: str,
) -> None:
    inspect_tool, _edit_tool = tools

    result = inspect_tool.invoke({"command": ["view", escape, "text"]})

    assert result["ok"] is False


def test_read_requires_existing_file(
    tools: tuple[object, object],
) -> None:
    inspect_tool, _edit_tool = tools

    result = inspect_tool.invoke({"command": ["view", "missing.xlsx", "text"]})

    assert result["ok"] is False
    assert "不存在" in str(result["message"])


def test_create_allows_missing_target_but_edit_requires_existing(
    monkeypatch: MonkeyPatch,
    tools: tuple[object, object],
) -> None:
    _inspect_tool, edit_tool = tools
    captured: list[dict[str, object]] = []
    monkeypatch.setattr(office_tools, "_run_officecli", _capture_runner(captured))

    with approved_office_execution():
        create_result = edit_tool.invoke({"command": ["create", "新建.xlsx"]})
        set_result = edit_tool.invoke(
            {"command": ["set", "不存在.xlsx", "/Sheet1/A1", "--prop", "value=1"]}
        )

    assert create_result["ok"] is True
    assert set_result["ok"] is False
    assert "不存在" in str(set_result["message"])


def test_merge_template_must_exist_and_output_may_not(
    monkeypatch: MonkeyPatch,
    tools: tuple[object, object],
    workspace: Path,
) -> None:
    _inspect_tool, edit_tool = tools
    (workspace / "模板.docx").write_bytes(b"docx")
    (workspace / "数据.json").write_text('{"name": "张三"}', encoding="utf-8")
    captured: list[dict[str, object]] = []
    monkeypatch.setattr(office_tools, "_run_officecli", _capture_runner(captured))

    with approved_office_execution():
        ok_result = edit_tool.invoke(
            {"command": ["merge", "模板.docx", "输出.docx", "--data", "数据.json"]}
        )
        missing_template = edit_tool.invoke(
            {"command": ["merge", "不存在.docx", "输出.docx", "--data", "数据.json"]}
        )

    assert ok_result["ok"] is True
    argv = captured[0]["argv"]
    # 模板与 --data 文件都重写为授权绝对路径；输出允许不存在
    assert argv[1] == str((workspace / "模板.docx").resolve())
    assert argv[argv.index("--data") + 1] == str((workspace / "数据.json").resolve())
    assert missing_template["ok"] is False


def test_merge_data_accepts_inline_json_object_and_rejects_garbage(
    monkeypatch: MonkeyPatch,
    tools: tuple[object, object],
    workspace: Path,
) -> None:
    _inspect_tool, edit_tool = tools
    (workspace / "模板.docx").write_bytes(b"docx")
    captured: list[dict[str, object]] = []
    monkeypatch.setattr(office_tools, "_run_officecli", _capture_runner(captured))

    with approved_office_execution():
        inline_ok = edit_tool.invoke(
            {"command": ["merge", "模板.docx", "输出.docx", "--data", '{"name": "张三"}']}
        )
        inline_array = edit_tool.invoke(
            {"command": ["merge", "模板.docx", "输出.docx", "--data", "[1, 2]"]}
        )
        garbage = edit_tool.invoke(
            {"command": ["merge", "模板.docx", "输出.docx", "--data", "not-json"]}
        )

    assert inline_ok["ok"] is True
    assert inline_array["ok"] is False and "对象" in str(inline_array["message"])
    assert garbage["ok"] is False


def test_import_requires_xlsx_target_and_existing_source(
    monkeypatch: MonkeyPatch,
    tools: tuple[object, object],
    workspace: Path,
) -> None:
    _inspect_tool, edit_tool = tools
    (workspace / "a.docx").write_bytes(b"docx")
    (workspace / "a.xlsx").write_bytes(b"xlsx")
    (workspace / "数据.csv").write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(office_tools, "_run_officecli", _capture_runner([]))

    with approved_office_execution():
        wrong_target = edit_tool.invoke(
            {"command": ["import", "a.docx", "/Sheet1", "数据.csv"]}
        )
        missing_source = edit_tool.invoke({"command": ["import", "a.xlsx", "/Sheet1", "无.csv"]})
        ok_result = edit_tool.invoke({"command": ["import", "a.xlsx", "/Sheet1", "数据.csv"]})

    assert wrong_target["ok"] is False and ".xlsx" in str(wrong_target["message"])
    assert missing_source["ok"] is False
    assert ok_result["ok"] is True


# ── 8. token 上限（M6） ──────────────────────────────────────────


def test_blob_options_allow_large_values_but_regular_tokens_are_capped(
    monkeypatch: MonkeyPatch,
    tools: tuple[object, object],
    workspace: Path,
) -> None:
    _inspect_tool, edit_tool = tools
    (workspace / "a.xlsx").write_bytes(b"xlsx")
    monkeypatch.setattr(office_tools, "_run_officecli", _capture_runner([]))

    large_commands = json.dumps(
        [{"command": "set", "path": "/Sheet1/A1", "props": {"value": "x" * 3000}}]
    )
    with approved_office_execution():
        blob_ok = edit_tool.invoke(
            {"command": ["batch", "a.xlsx", "--commands", large_commands]}
        )
        regular_too_long = edit_tool.invoke(
            {"command": ["set", "a.xlsx", "/Sheet1/A1", "--prop", f"value={'y' * 2001}"]}
        )

    assert blob_ok["ok"] is True
    assert regular_too_long["ok"] is False
    assert "过长" in str(regular_too_long["message"])


def test_command_token_count_is_capped(
    tools: tuple[object, object],
    workspace: Path,
) -> None:
    _inspect_tool, edit_tool = tools
    (workspace / "a.xlsx").write_bytes(b"xlsx")

    # token 数上限由 pydantic schema 层先行拦截（invoke 即抛 ValidationError），
    # 工具体内的同值检查是第二道防线
    with approved_office_execution(), pytest.raises(ValidationError):
        edit_tool.invoke({"command": ["set", "a.xlsx", *["x"] * 60]})


def test_view_mode_must_be_in_whitelist(
    tools: tuple[object, object],
    workspace: Path,
) -> None:
    inspect_tool, _edit_tool = tools
    (workspace / "a.xlsx").write_bytes(b"xlsx")

    html_result = inspect_tool.invoke({"command": ["view", "a.xlsx", "html"]})
    text_result = inspect_tool.invoke({"command": ["view", "a.xlsx", "text"]})

    # html 不在白名单 → 校验拒绝；text 走真实子进程前的校验应通过
    assert html_result["ok"] is False and "文本模式" in str(html_result["message"])
    assert "不支持的" not in str(text_result.get("message", ""))


# ── 9/10. 子进程执行器（真实子进程 + 假二进制） ──────────────────


def test_run_officecli_injects_env_and_closes_stdin(tmp_path: Path) -> None:
    script = (
        "import os, sys\n"
        "data = sys.stdin.read()\n"  # stdin 已关闭：立即 EOF 而不是阻塞
        "print('env=%s|%s|%s' % (\n"
        " os.environ.get('OFFICECLI_SKIP_UPDATE'),\n"
        " os.environ.get('OFFICECLI_NO_AUTO_RESIDENT'),\n"
        " os.environ.get('OFFICECLI_NO_AUTO_INSTALL')))\n"
        "print('stdin-eof' if data == '' else 'stdin-data')\n"
    )

    result = office_tools._run_officecli(
        sys.executable,
        ["-c", script],
        cwd=tmp_path,
        timeout_seconds=10,
        max_output_bytes=4096,
    )

    assert result["ok"] is True
    assert "env=1|1|1" in str(result["stdout"])
    assert "stdin-eof" in str(result["stdout"])


def test_run_officecli_reports_nonzero_exit_and_stderr(tmp_path: Path) -> None:
    result = office_tools._run_officecli(
        sys.executable,
        ["-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"],
        cwd=tmp_path,
        timeout_seconds=10,
        max_output_bytes=4096,
    )

    assert result["ok"] is False
    assert result["exit_code"] == 3
    assert "boom" in str(result["stderr"])
    assert result["timed_out"] is False


def test_run_officecli_times_out_and_kills_process(tmp_path: Path) -> None:
    started = time.monotonic()
    result = office_tools._run_officecli(
        sys.executable,
        ["-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        timeout_seconds=1,
        max_output_bytes=4096,
    )

    assert result["timed_out"] is True
    assert result["ok"] is False
    # 超时应及时返回（远小于脚本的 30 秒睡眠）
    assert time.monotonic() - started < 15


def test_run_officecli_truncates_large_output(tmp_path: Path) -> None:
    script = "print('x' * 100000); import sys; sys.stderr.write('y' * 100000)"
    result = office_tools._run_officecli(
        sys.executable,
        ["-c", script],
        cwd=tmp_path,
        timeout_seconds=30,
        max_output_bytes=2048,
    )

    assert result["truncated"] is True
    # stdout+stderr 合并字节数不超过上限
    total = len(str(result["stdout"]).encode()) + len(str(result["stderr"]).encode())
    assert total <= 2048


# ── 11. 写工具审批门（H3） ───────────────────────────────────────


def test_edit_requires_approval_context_and_declares_extras(
    tools: tuple[object, object],
    workspace: Path,
) -> None:
    _inspect_tool, edit_tool = tools
    (workspace / "a.xlsx").write_bytes(b"xlsx")

    with pytest.raises(PermissionError, match="approval"):
        edit_tool.invoke({"command": ["set", "a.xlsx", "/Sheet1/A1", "--prop", "value=1"]})

    assert edit_tool.extras == {
        "category": "office",
        "requires_approval": True,
        "status_from_ok": True,
    }
    inspect_tool = _inspect_tool
    assert inspect_tool.extras == {"category": "office", "read_only": True}


def test_edit_marks_nonzero_exit_as_not_ok(
    monkeypatch: MonkeyPatch,
    tools: tuple[object, object],
    workspace: Path,
) -> None:
    _inspect_tool, edit_tool = tools
    (workspace / "a.xlsx").write_bytes(b"xlsx")
    failing = {**_FAKE_OK, "ok": False, "exit_code": 1, "stderr": "bad"}
    monkeypatch.setattr(
        office_tools,
        "_run_officecli",
        _capture_runner([], result=failing),
    )

    with approved_office_execution():
        result = edit_tool.invoke(
            {"command": ["set", "a.xlsx", "/Sheet1/A1", "--prop", "value=1"]}
        )

    assert result["ok"] is False


def test_edit_success_carries_generated_files_metadata(
    monkeypatch: MonkeyPatch,
    tools: tuple[object, object],
    workspace: Path,
) -> None:
    _inspect_tool, edit_tool = tools
    monkeypatch.setattr(office_tools, "_run_officecli", _capture_runner([]))

    with approved_office_execution():
        # create 的输出文件：命令成功后由工具落盘（这里用假 runner，手工补文件）
        (workspace / "新表.xlsx").write_bytes(b"xlsx")
        result = edit_tool.invoke({"command": ["create", "新表.xlsx"]})

    assert result["ok"] is True
    generated = result[office_tools.GENERATED_FILES_RESULT_KEY]
    assert isinstance(generated, list)
    assert generated[0]["name"] == "新表.xlsx"
    assert generated[0]["size"] == 4


# ── 12. per-file 锁：同文件串行、不同文件并行 ────────────────────


def test_per_file_lock_serializes_same_file_and_parallelizes_different_files(
    monkeypatch: MonkeyPatch,
    tools: tuple[object, object],
    workspace: Path,
) -> None:
    _inspect_tool, edit_tool = tools
    (workspace / "a.xlsx").write_bytes(b"xlsx")
    (workspace / "b.xlsx").write_bytes(b"xlsx")
    intervals: list[tuple[str, float, float]] = []
    intervals_lock = threading.Lock()

    def slow_runner(
        binary: str,
        argv: list[str],
        *,
        cwd: Path | None,
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> dict[str, object]:
        start = time.monotonic()
        time.sleep(0.2)
        end = time.monotonic()
        with intervals_lock:
            intervals.append((str(argv[1]), start, end))
        return dict(_FAKE_OK)

    monkeypatch.setattr(office_tools, "_run_officecli", slow_runner)

    def run_edit(name: str) -> None:
        with approved_office_execution():
            edit_tool.invoke({"command": ["set", name, "/Sheet1/A1", "--prop", "value=1"]})

    # 同文件并发：两个线程的时间区间不得重叠
    threads = [threading.Thread(target=run_edit, args=("a.xlsx",)) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    same_file = sorted(interval for interval in intervals if interval[0].endswith("a.xlsx"))
    assert len(same_file) == 2
    first, second = same_file[0], same_file[1]
    assert first[2] <= second[1]  # 第一个完全结束后第二个才开始

    # 不同文件并发：时间区间应重叠（并行）
    intervals.clear()
    threads = [
        threading.Thread(target=run_edit, args=("a.xlsx",)),
        threading.Thread(target=run_edit, args=("b.xlsx",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(intervals) == 2
    starts = sorted(interval[1] for interval in intervals)
    ends = sorted(interval[2] for interval in intervals)
    assert starts[1] < ends[0]  # 两区间存在交集


# ── 二进制解析与启动自检（T0-2） ─────────────────────────────────


def test_officecli_enabled_defaults_to_off(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("API_OFFICECLI_ENABLED", raising=False)
    assert officecli_enabled() is False
    monkeypatch.setenv("API_OFFICECLI_ENABLED", "1")
    assert officecli_enabled() is True


def test_resolve_binary_fails_fast_for_missing_path(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("API_OFFICECLI_BINARY", "D:/definitely/missing/officecli.exe")

    with pytest.raises(RuntimeError, match="officecli"):
        resolve_officecli_binary()


def test_resolve_binary_fails_when_not_on_path(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("API_OFFICECLI_BINARY", "officecli-not-exists-xyz")

    with pytest.raises(RuntimeError, match="PATH"):
        resolve_officecli_binary()


def test_resolve_binary_warns_on_version_mismatch(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake_binary = tmp_path / "officecli.exe"
    fake_binary.write_bytes(b"exe")
    monkeypatch.setenv("API_OFFICECLI_BINARY", str(tmp_path / "officecli.exe"))
    monkeypatch.setattr(office_tools.shutil, "which", lambda _name: str(fake_binary))
    monkeypatch.setattr(
        office_tools,
        "_run_officecli",
        lambda *args, **kwargs: {
            "ok": True,
            "exit_code": 0,
            "stdout": "0.0.0-test\n",
            "stderr": "",
            "timed_out": False,
            "truncated": False,
        },
    )

    with caplog.at_level("WARNING"):
        resolved = resolve_officecli_binary()

    # 版本不一致只告警不阻断
    assert resolved.endswith("officecli.exe")
    assert any("版本与预期不一致" in record.getMessage() for record in caplog.records)


def test_resolve_binary_fails_when_self_check_fails(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_binary = tmp_path / "officecli.exe"
    fake_binary.write_bytes(b"exe")
    monkeypatch.setenv("API_OFFICECLI_BINARY", "officecli")
    monkeypatch.setattr(office_tools.shutil, "which", lambda _name: str(fake_binary))
    monkeypatch.setattr(
        office_tools,
        "_run_officecli",
        lambda *args, **kwargs: {
            "ok": False,
            "exit_code": 1,
            "stdout": "",
            "stderr": "broken",
            "timed_out": False,
            "truncated": False,
        },
    )

    with pytest.raises(RuntimeError, match="自检失败"):
        resolve_officecli_binary()


# ── 13. 真实 officecli 集成（无二进制时跳过） ────────────────────


def test_real_officecli_roundtrip(
    filesystem: WorkspaceFileSystem,
    workspace: Path,
) -> None:
    binary = shutil.which(os.getenv("API_OFFICECLI_BINARY", "officecli"))
    if binary is None:
        pytest.skip("officecli 二进制不可用")
    inspect_tool, edit_tool = create_office_tools(
        filesystem,
        OfficeCliSettings(binary=binary),
    )

    with approved_office_execution():
        created = edit_tool.invoke({"command": ["create", "成绩.xlsx"]})
        assert created["ok"] is True, created
        batch = json.dumps(
            [
                {"command": "set", "path": "/Sheet1/A1", "props": {"value": "姓名"}},
                {"command": "set", "path": "/Sheet1/B1", "props": {"value": "成绩"}},
                {"command": "set", "path": "/Sheet1/A2", "props": {"value": "张三"}},
                {"command": "set", "path": "/Sheet1/B2", "props": {"value": "95"}},
            ],
            ensure_ascii=False,
        )
        edited = edit_tool.invoke({"command": ["batch", "成绩.xlsx", "--commands", batch]})
        assert edited["ok"] is True, edited

    viewed = inspect_tool.invoke({"command": ["view", "成绩.xlsx", "text", "--range", "Sheet1!A1:B2"]})
    assert viewed["ok"] is True, viewed
    assert "张三" in str(viewed["stdout"])
    assert "95" in str(viewed["stdout"])

    validated = inspect_tool.invoke({"command": ["validate", "成绩.xlsx"]})
    assert validated["ok"] is True, validated


# ── 图级端到端：审批 → 真实执行 → 生成文件回执（T5-3） ──────────


class _ScriptedModel:
    """按图执行顺序返回预设模型消息（与 test_graph_builder 同款替身）。"""

    def __init__(self, responses: list[AIMessage]) -> None:
        self._responses = list(responses)
        self.calls = 0

    def bind_tools(self, _tools: list[object]) -> _ScriptedModel:
        return self

    def invoke(self, _messages: list[object]) -> AIMessage:
        self.calls += 1
        if not self._responses:
            raise AssertionError("脚本化模型没有更多预设响应")
        return self._responses.pop(0)


def test_approved_edit_flow_creates_file_and_attaches_download_receipt(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """全链路：模型发起 officecli_edit → 审批门 → 真实 officecli 落盘 →
    最终回答消息携带 generated_files → API 层注册为受控下载附件。"""
    from langgraph.checkpoint.memory import InMemorySaver

    from api.files import attachments_for_generated_files
    from core.graph_builder import CollaborativeAgentGraph
    from core.state import (
        AgentRole,
        ToolApprovalAction,
        ToolApprovalDecision,
        message_generated_files,
    )

    binary = shutil.which("officecli")
    if binary is None:
        pytest.skip("officecli 二进制不可用")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("API_UPLOAD_DIR", str(tmp_path / "uploads"))
    filesystem = WorkspaceFileSystem(workspace)
    tools = create_office_tools(filesystem, OfficeCliSettings(binary=binary))
    model = _ScriptedModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "officecli_edit",
                        "args": {"command": ["create", "成绩.xlsx"]},
                        "id": "office-call-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="已为你生成 成绩.xlsx"),
        ]
    )
    graph = CollaborativeAgentGraph(
        model=model,  # type: ignore[arg-type]
        tools=list(tools),
        tool_permissions={
            "officecli_inspect": {AgentRole.SUPERVISOR},
            "officecli_edit": {AgentRole.SUPERVISOR},
        },
        checkpointer=InMemorySaver(),
        orchestration_mode="tool",
    )
    session_id = "office-e2e"

    paused = graph.run("创建成绩表", session_id, workspace_root=str(workspace))
    pending = graph.get_pending_tool_approval(session_id)

    assert paused["pending_tool_approval"] is not None
    assert pending is not None
    # 审批卡拿到的是校验后的数组型命令
    assert pending.request.arguments["command"] == ["create", "成绩.xlsx"]
    assert not (workspace / "成绩.xlsx").exists()  # 批准前不落盘

    result = graph.resume_tool_approval(
        session_id,
        ToolApprovalDecision(
            interrupt_id=pending.interrupt_id,
            action=ToolApprovalAction.CONFIRM,
        ),
    )

    # 真实 officecli 落盘 + 最终回答携带生成文件回执
    assert (workspace / "成绩.xlsx").is_file()
    final_message = result["messages"][-1]
    entries = message_generated_files(final_message)
    assert entries is not None and entries[0].name == "成绩.xlsx"

    # API 层注册为受控下载附件（幂等、版本化 file_id）
    attachments = attachments_for_generated_files("user-1", final_message)
    assert attachments is not None and len(attachments) == 1
    assert attachments[0].name == "成绩.xlsx"
    copied = tmp_path / "uploads" / "user-1" / attachments[0].file_id
    assert copied.is_file()
    assert copied.stat().st_size == (workspace / "成绩.xlsx").stat().st_size
