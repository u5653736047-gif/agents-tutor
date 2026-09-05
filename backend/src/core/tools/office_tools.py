"""officecli CLI 子进程封装：只读 inspect 与审批门控 edit 两个 LangChain 工具。

安全模型（与 docs/officecli-integration-plan.md 第 3 节冻结决策一一对应）：

- 命令白名单：READ_VERBS / EDIT_VERBS；batch 子项再受 BATCH_ITEM_VERBS
  限制（拦截 raw / raw-set / add-part / dump 等可绕过顶层白名单的子命令）；
- 选项白名单默认拒绝：不在表内的选项一律拒绝，显式拒绝项带理由（如
  ``get --save`` 会把文档负载写到任意路径、``import --stdin`` 会挂死管道）；
- 文件参数全部经 WorkspaceFileSystem 解析并重写为授权绝对路径，``--prop``
  中的文件引用键（src/href）与路径形取值同样必须解析为工作区内文件；
- 写工具双保险：extras.requires_approval（图审批门）+ ``_APPROVED_OFFICE``
  运行时门（与 shell 工具同一模式），未批准上下文中的调用直接 PermissionError；
- 子进程 stdin 恒为 DEVNULL（batch/import 缺省读 stdin，继承会挂死）、
  Windows 下无窗口、注入 OFFICECLI_SKIP_UPDATE / NO_AUTO_RESIDENT /
  NO_AUTO_INSTALL 三个防御变量、输出有界截断；
- per-file 锁：同一文件的读/写命令串行（Windows 下 File.Replace 与并发
  读句柄会撞共享冲突），不同文件并行；锁仅单进程有效。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..filesystem import (
    WorkspaceFileError,
    WorkspaceFileSystem,
    active_workspace_filesystem,
)
from .artifact_scope import artifact_auto_approval_roots

_LOGGER = logging.getLogger(__name__)

# ── 3.1 命令白名单（冻结决策的唯一来源） ─────────────────────────
READ_VERBS = frozenset({"help", "load_skill", "view", "get", "query", "validate"})
EDIT_VERBS = frozenset(
    {"create", "set", "add", "remove", "move", "swap", "batch", "import", "merge"}
)
VIEW_MODES = frozenset({"text", "annotated", "outline", "stats", "issues"})
# batch --commands 数组中每个 item 的 command 允许值：显式排除
# raw/raw-set/add-part/dump（batch 分发器实际支持它们，不做子白名单
# 会被间接绕过——计划高危 H2）。
BATCH_ITEM_VERBS = frozenset(
    {"get", "query", "set", "add", "remove", "move", "swap", "view", "validate"}
)
# --prop 中承载外部文件路径的键，取值必须解析为工作区内文件（高危 H1）。
FILE_PROPS = frozenset({"src", "href"})

# ── 3.2 文件扩展名 ──────────────────────────────────────────────
OFFICE_EXTENSIONS = frozenset({".docx", ".xlsx", ".pptx"})
IMPORT_SOURCE_EXTENSIONS = frozenset({".csv", ".tsv"})
MERGE_DATA_EXTENSIONS = frozenset({".json"})

# ── 3.4 选项参数白名单（默认拒绝；已逐项对照 officecli 1.0.144 --help） ──
_ALLOWED_OPTIONS: dict[str, frozenset[str]] = {
    "help": frozenset(),
    "load_skill": frozenset(),
    "view": frozenset(
        {
            "--start",
            "--end",
            "--max-lines",
            "--type",
            "--limit",
            "--cols",
            "--page",
            "--range",
            "--json",
        }
    ),
    "get": frozenset({"--depth", "--json"}),
    "query": frozenset({"--find", "--compact", "--fields", "--json"}),
    "validate": frozenset({"--json"}),
    "create": frozenset({"--type", "--force", "--locale", "--minimal", "--json"}),
    "set": frozenset({"--prop", "--find", "--replace", "--json"}),
    "add": frozenset({"--type", "--from", "--index", "--after", "--before", "--prop", "--json"}),
    "remove": frozenset({"--shift", "--prop", "--json"}),
    "move": frozenset({"--to", "--index", "--after", "--before", "--prop", "--json"}),
    "swap": frozenset({"--json"}),
    "batch": frozenset({"--commands", "--stop-on-error", "--json"}),
    "import": frozenset({"--format", "--header", "--start-cell", "--json"}),
    "merge": frozenset({"--data", "--force", "--json"}),
}
# 显式拒绝项与理由：命中时错误信息直接告知模型原因，便于自我纠正。
_DENIED_OPTIONS: dict[str, dict[str, str]] = {
    "view": {
        "--browser": "会打开浏览器弹窗",
        "-o": "会把渲染结果落盘到任意路径",
        "--out": "会把渲染结果落盘到任意路径",
        "--render": "截图渲染路径不在本期范围",
        "--grid": "截图拼接不在本期范围",
        "--screenshot-width": "截图不在本期范围",
        "--screenshot-height": "截图不在本期范围",
        "--page-count": "会调用本机 Word 重排页码，慢且依赖桌面组件",
    },
    "get": {"--save": "会把图片/OLE 负载写到任意路径（只读工具侧的写原语）"},
    "set": {"--force": "会绕过文档保护"},
    "add": {"--force": "会绕过文档保护"},
    "batch": {
        "--input": "文件来源不受白名单控制，请改用 --commands 内联 JSON",
        "--stdin": "隐式 stdin 会挂死子进程管道",
        "--force": "保护绕过选项不在本期范围",
        "--best-effort": "非原子模式会留下半完成状态",
    },
    "import": {
        "--file": "选项形式的文件来源不受白名单控制，请改用第 3 个位置参数",
        "--stdin": "会阻塞读取子进程管道",
    },
}
# 不带值的开关选项（其余选项一律按「带值」解析）。
_FLAG_OPTIONS = frozenset(
    {
        "--json",
        "--force",
        "--minimal",
        "--header",
        "--stop-on-error",
        "--best-effort",
        "--browser",
        "--stdin",
        "--page-count",
        "--compact",
    }
)
# 允许重复出现的选项（目前只有 --prop）。
_REPEATABLE_OPTIONS = frozenset({"--prop"})
# 取值上限放宽到 32K 的选项（20 条 set 的 batch JSON 会超过单 token 2K
# 的常规上限——计划 M6）。
_BLOB_OPTIONS = frozenset({"--commands", "--data"})

# ── 命令形态限制（3.6/T1-3） ────────────────────────────────────
MAX_COMMAND_TOKENS = 48
MAX_TOKEN_CHARS = 2_000
MAX_BLOB_TOKEN_CHARS = 32_768
_MAX_BATCH_ITEMS = 64

# ── 3.5 环境变量与默认值 ────────────────────────────────────────
ENV_OFFICECLI_BINARY = "API_OFFICECLI_BINARY"
ENV_OFFICECLI_ENABLED = "API_OFFICECLI_ENABLED"
ENV_OFFICECLI_TIMEOUT_READ = "API_OFFICECLI_TIMEOUT_READ_SECONDS"
ENV_OFFICECLI_TIMEOUT_WRITE = "API_OFFICECLI_TIMEOUT_WRITE_SECONDS"
ENV_OFFICECLI_MAX_OUTPUT = "API_OFFICECLI_MAX_OUTPUT_BYTES"
DEFAULT_TIMEOUT_READ_SECONDS = 60
DEFAULT_TIMEOUT_WRITE_SECONDS = 120
DEFAULT_MAX_OUTPUT_BYTES = 128 * 1024
# 双层超时推导规则（计划 M2）：执行器注册值 = 子进程超时 + 本常量，
# 保证子进程先超时并返回自带诊断，而不是执行器给出泛化超时。
EXECUTOR_TIMEOUT_MARGIN_SECONDS = 5
# 预期版本以部署机实际安装版本为准；不一致只告警不阻断（计划 L2，
# 注意源码 csproj 标记 1.0.142 与已装二进制 1.0.144 不符，以二进制为准）。
EXPECTED_OFFICECLI_VERSION = "1.0.144"
_SELF_CHECK_TIMEOUT_SECONDS = 5
_SELF_CHECK_MAX_OUTPUT_BYTES = 8 * 1024

# 每次调用（含启动自检）必须注入的三个防御变量（3.6）：
# 禁后台更新、禁自动拉起 resident、禁自动安装。
_SUBPROCESS_ENV = {
    "OFFICECLI_SKIP_UPDATE": "1",
    "OFFICECLI_NO_AUTO_RESIDENT": "1",
    "OFFICECLI_NO_AUTO_INSTALL": "1",
}

# 「形如路径」启发式（3.4 第 2 条 + F1 修复）：命中时必须解析为工作区内
# 文件，兜住 add --type ole/video 等经由其他键携带文件的情况（高危 H1）。
# 判定故意收紧为四类「强路径特征」，避免误杀教学文本中含单个 "/" 的
# 普通内容（如 "TCP/IP 协议"、"A/B 测试"、"2026/08/18"、"输入/输出"）：
# 1. Windows 盘符前缀（C:/x、C:\x）；
# 2. 以 "/" 开头（POSIX 绝对路径，如 /etc/passwd）；
# 3. 以 "\\" 或 "//" 开头（UNC 共享路径 \\server\share / //server/share）；
# 4. 以常见资源扩展名结尾（image.png、data/图.png）。
# src/href 键本就无条件强制解析，非这四类的取值不再视为文件引用。
_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")
_ASSET_EXTENSIONS = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".webp",
        ".svg",
        ".tif",
        ".tiff",
        ".emf",
        ".wmf",
        ".mp4",
        ".avi",
        ".mov",
        ".wmv",
        ".mkv",
        ".webm",
        ".mp3",
        ".wav",
        ".m4a",
        ".flac",
        ".ogg",
        ".pdf",
        ".zip",
        ".bin",
        ".dat",
        ".ole",
        ".html",
        ".htm",
        ".xml",
        ".txt",
        ".md",
        ".ttf",
        ".otf",
        ".woff",
        ".woff2",
        *OFFICE_EXTENSIONS,
        *IMPORT_SOURCE_EXTENSIONS,
        *MERGE_DATA_EXTENSIONS,
    }
)

# T5-3：写工具成功后结果中携带的生成文件清单键；graph_builder 据此把
# 文件元数据挂到最终回答消息（additional_kwargs），API 层再转为下载附件。
GENERATED_FILES_RESULT_KEY = "generated_files"

# 3.7 审批运行时门（与 shell 工具同一模式的双保险）。
_APPROVED_OFFICE: ContextVar[bool] = ContextVar("approved_office_execution", default=False)

# 3.8 per-file 锁注册表：键是规范化后的文件绝对路径（Windows 大小写
# 不敏感，normcase 归一），inspect 与 edit 共用同一注册表。锁仅单进程
# 有效；演示规模下注册表无界增长可接受（README 注明单进程语义）。
_FILE_LOCKS: dict[str, threading.Lock] = {}
_FILE_LOCKS_GUARD = threading.Lock()


class _OfficeCommandError(ValueError):
    """命令白名单/选项/路径校验失败；消息可直接展示给模型（含修正指引）。"""


@dataclass(frozen=True, slots=True)
class OfficeCliSettings:
    """officecli 集成的装配配置（lifespan 启动时从环境变量读取一次）。

    - binary：解析后的 officecli 可执行文件绝对路径；
    - timeout_read_seconds / timeout_write_seconds：只读/写工具的子进程
      超时（执行器侧注册值由 app.py 按 +5 秒规则推导，不二次硬编码）；
    - max_output_bytes：单次 stdout+stderr 合并上限。
    """

    binary: str
    timeout_read_seconds: int = DEFAULT_TIMEOUT_READ_SECONDS
    timeout_write_seconds: int = DEFAULT_TIMEOUT_WRITE_SECONDS
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES


@dataclass(frozen=True, slots=True)
class _NormalizedCommand:
    """校验通过后的可执行命令。

    - argv：文件参数已重写为授权绝对路径的完整参数数组；
    - files：本次命令涉及的全部 Office 文件（用于 per-file 加锁）；
    - generated：命令会创建/修改的文件（T5-3 下载回执的数据源）。
    """

    argv: list[str]
    files: tuple[Path, ...]
    generated: tuple[Path, ...]


class _OfficeCliInput(BaseModel):
    """officecli 命令令牌数组（不含二进制名本身，首元素是动词）。"""

    model_config = ConfigDict(extra="forbid")

    command: list[str] = Field(min_length=1, max_length=MAX_COMMAND_TOKENS)

    @field_validator("command")
    @classmethod
    def reject_blank_or_nul(cls, value: list[str]) -> list[str]:
        for token in value:
            if not token.strip() or "\x00" in token:
                raise ValueError("command tokens must not be blank and may not contain NUL")
        return value


@contextmanager
def approved_office_execution() -> Iterator[None]:
    """仅在已批准的图审批门内放行 officecli_edit（与 shell 同一模式）。

    graph_builder 的审批恢复路径在 approved_shell_execution() 旁并列进入
    本上下文；即使未来出现新的执行入口，未批准上下文中的写调用也会被
    工具入口的 _APPROVED_OFFICE 检查拒绝（高危 H3）。
    """
    token = _APPROVED_OFFICE.set(True)
    try:
        yield
    finally:
        _APPROVED_OFFICE.reset(token)


# 工作流产物区自动授权（lesson-workflow-design §五）：ContextVar 通道
# 与上下文管理器在 artifact_scope（react_agent 写、executor 读）；此处
# 只消费。门检查据此放行「全部涉文件都在产物根内」的写命令；目录外
# 照旧拒绝。


def _within_any_root(path: Path | str, roots: Sequence[str]) -> bool:
    """绝对路径是否落在任一授权根内（resolve 后前缀比较，防 .. 逃逸）。"""
    try:
        resolved = Path(path).resolve()
    except OSError:
        return False
    for root in roots:
        try:
            if resolved.is_relative_to(Path(root).resolve()):
                return True
        except (OSError, ValueError):
            continue
    return False


# 无活动工作区时的占位默认：office_targets_within_roots 的真实调用路径
# （react_agent 豁免检查分支）恒处于 workspace_scope 内，占位实例不会被
# 用到；无 scope 时解析落在占位根下、产物根判定失败 → 不豁免（fail-closed）。
_FALLBACK_WORKSPACE = WorkspaceFileSystem(Path.cwd())


def office_targets_within_roots(
    command: Sequence[str],
    roots: Sequence[str],
) -> bool:
    """判定 officecli_edit 命令涉及的全部文件是否都落在授权根内。

    复用 `_normalize_office_command` 做权威解析（动词白名单/位置参数/
    路径解析同一套逻辑，不另写简化版）；解析失败视为不豁免（走人工
    审批，由审批准备阶段给出准确的稳定错误）。工作区解析依赖调用方
    处于 workspace_scope 上下文（与执行路径同一前提）。
    """
    try:
        normalized = _normalize_office_command(
            list(command),
            active_workspace_filesystem(_FALLBACK_WORKSPACE),
            writable=True,
        )
    except Exception:  # noqa: BLE001 - 解析失败一律不豁免，走人工审批
        return False
    if not normalized.files:
        return False
    return all(_within_any_root(path, roots) for path in normalized.files)


def officecli_enabled() -> bool:
    """读取 API_OFFICECLI_ENABLED：默认 0（完全不注册工具），显式开启才装配。"""
    return os.getenv(ENV_OFFICECLI_ENABLED, "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def load_officecli_settings() -> OfficeCliSettings:
    """从环境变量装配配置并解析二进制（显式开启时 fail-fast）。"""
    return OfficeCliSettings(
        binary=resolve_officecli_binary(),
        timeout_read_seconds=_positive_int_env(
            ENV_OFFICECLI_TIMEOUT_READ,
            DEFAULT_TIMEOUT_READ_SECONDS,
        ),
        timeout_write_seconds=_positive_int_env(
            ENV_OFFICECLI_TIMEOUT_WRITE,
            DEFAULT_TIMEOUT_WRITE_SECONDS,
        ),
        max_output_bytes=_positive_int_env(
            ENV_OFFICECLI_MAX_OUTPUT,
            DEFAULT_MAX_OUTPUT_BYTES,
        ),
    )


def resolve_officecli_binary() -> str:
    """解析 officecli 可执行文件并做启动自检，失败抛出带安装提示的 RuntimeError。

    - API_OFFICECLI_BINARY 为裸名称时走 PATH 查找；含路径分隔符时按路径
      直接校验；
    - 自检经 _run_officecli 通道运行 ``--version``（复用 stdin 关闭与三个
      防御环境变量，避免自检本身拉起更新子进程——计划 L3）；
    - 版本与预期不一致只告警不阻断（计划 T0-2 第 6 条）。
    """
    configured = os.getenv(ENV_OFFICECLI_BINARY, "officecli").strip() or "officecli"
    if os.sep in configured or (os.altsep is not None and os.altsep in configured):
        candidate = Path(configured).expanduser()
        if not candidate.is_file():
            raise RuntimeError(
                f"officecli 二进制不存在：{configured}。"
                "请安装 officecli（见 README「Office 文档工具」一节）或修正 "
                f"{ENV_OFFICECLI_BINARY}。"
            )
        binary = str(candidate.resolve())
    else:
        resolved = shutil.which(configured)
        if resolved is None:
            raise RuntimeError(
                f"PATH 中找不到 officecli 二进制：{configured}。"
                "请安装 officecli（见 README「Office 文档工具」一节），或经 "
                f"{ENV_OFFICECLI_BINARY} 指定绝对路径。"
            )
        binary = resolved
    result = _run_officecli(
        binary,
        ["--version"],
        cwd=None,
        timeout_seconds=_SELF_CHECK_TIMEOUT_SECONDS,
        max_output_bytes=_SELF_CHECK_MAX_OUTPUT_BYTES,
    )
    if result["timed_out"] or not result["ok"]:
        raise RuntimeError(
            "officecli 启动自检失败（--version 未正常返回）。"
            f"stderr: {str(result['stderr'])[:200]}"
        )
    stdout = str(result["stdout"])
    version_lines = stdout.strip().splitlines()
    version = version_lines[0].strip() if version_lines else ""
    if version != EXPECTED_OFFICECLI_VERSION:
        _LOGGER.warning(
            "officecli 版本与预期不一致：预期 %s，实际 %s（仅告警，不阻断启动）",
            EXPECTED_OFFICECLI_VERSION,
            version,
        )
    return binary


def _run_officecli(
    binary: str,
    argv: Sequence[str],
    *,
    cwd: Path | None,
    timeout_seconds: float,
    max_output_bytes: int,
) -> dict[str, object]:
    """运行一次 officecli 子进程并返回有界、稳定结构的结果。

    关键约束（3.6）：
    - stdin 恒为 DEVNULL——batch/import 缺省读 stdin，继承父进程管道会
      挂死到超时（高危 H4）；
    - 不经 shell；Windows 下 CREATE_NO_WINDOW；
    - 注入三个 OFFICECLI_* 防御变量；
    - stdout/stderr 合并截断到 max_output_bytes，返回 truncated 标记。
    """
    env = os.environ.copy()
    env.update(_SUBPROCESS_ENV)
    try:
        completed = subprocess.run(
            [binary, *argv],
            cwd=str(cwd) if cwd is not None else None,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        stdout, stdout_truncated = _coerce_and_truncate(error.stdout, max_output_bytes)
        stderr, stderr_truncated = _coerce_and_truncate(
            error.stderr,
            max(0, max_output_bytes - len(stdout.encode("utf-8"))),
        )
        return {
            "ok": False,
            "exit_code": None,
            "stdout": stdout,
            "stderr": stderr,
            "timed_out": True,
            "truncated": stdout_truncated or stderr_truncated,
        }
    except OSError as error:
        return {
            "ok": False,
            "exit_code": None,
            "stdout": "",
            "stderr": f"无法启动 officecli 子进程：{error}",
            "timed_out": False,
            "truncated": False,
        }
    stdout, stdout_truncated = _coerce_and_truncate(completed.stdout, max_output_bytes)
    stderr, stderr_truncated = _coerce_and_truncate(
        completed.stderr,
        max(0, max_output_bytes - len(stdout.encode("utf-8"))),
    )
    return {
        "ok": completed.returncode == 0,
        "exit_code": completed.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": False,
        "truncated": stdout_truncated or stderr_truncated,
    }


def create_office_tools(
    filesystem: WorkspaceFileSystem,
    settings: OfficeCliSettings,
) -> tuple[BaseTool, BaseTool]:
    """创建绑定同一工作区能力的 officecli_inspect / officecli_edit 工具对。"""

    def current() -> WorkspaceFileSystem:
        return active_workspace_filesystem(filesystem)

    @tool(
        "officecli_inspect",
        args_schema=_OfficeCliInput,
        extras={"category": "office", "read_only": True},
    )
    def officecli_inspect(command: list[str]) -> dict[str, object]:
        """只读查看工作区内 Office 文档（.docx/.xlsx/.pptx），command 是 officecli 参数数组。

        用法约定：
        - 先用 ["help"] 或 ["load_skill", "excel"] 了解能力，再用 get/query/view 读取；
        - 文件用工作区相对路径（如 "成绩.xlsx"）或已授权绝对路径；
        - view 支持 text/annotated/outline/stats/issues 模式；大表格用
          view text --range Sheet1!A1:C10 限制范围；大文档用 --max-lines/--start/--end 分页；
        - 需要结构化输出时加 --json。
        """
        active = current()
        try:
            normalized = _normalize_office_command(command, active, writable=False)
        except (_OfficeCommandError, WorkspaceFileError) as error:
            return _command_error_result(error)
        with _locked_files(normalized.files):
            return _run_officecli(
                settings.binary,
                normalized.argv,
                cwd=active.root,
                timeout_seconds=settings.timeout_read_seconds,
                max_output_bytes=settings.max_output_bytes,
            )

    @tool(
        "officecli_edit",
        args_schema=_OfficeCliInput,
        extras={
            "category": "office",
            "requires_approval": True,
            "status_from_ok": True,
        },
    )
    def officecli_edit(command: list[str]) -> dict[str, object]:
        """修改工作区内 Office 文档（.docx/.xlsx/.pptx），需用户批准后才执行。

        command 是 officecli 参数数组。用法约定：
        - 修改前先用 officecli_inspect 查看结构；多步修改合并为一次
          batch --commands（JSON 对象数组）；
        - 完成后用 officecli_inspect 的 validate 校验；每次写命令即时落盘；
        - 文件必须位于当前会话工作区；import 的目标文件仅支持 .xlsx；
        - create 新建文档；merge 用模板加 --data（工作区 .json 文件或内联
          JSON 对象）生成文档。
        """
        # 运行时审批门（高危 H3）：即使图装配缺陷或新增执行入口绕过审批
        # 节点，未批准上下文中的调用也无法写文件。唯一例外是工作流产物
        # 区自动授权（lesson-workflow-design §五）：命令涉及的全部文件
        # 都落在登记的产物目录内时免人工审批——产物目录是本工作流运行
        # 新建的隔离目录，不触用户既有文件；目录外写操作照旧硬拒绝。
        active = current()
        try:
            normalized = _normalize_office_command(command, active, writable=True)
        except (_OfficeCommandError, WorkspaceFileError) as error:
            return _command_error_result(error)
        if not _APPROVED_OFFICE.get():
            auto_roots = artifact_auto_approval_roots.get()
            if (
                auto_roots is None
                or not normalized.files
                or not all(
                    _within_any_root(path, auto_roots)
                    for path in normalized.files
                )
            ):
                raise PermissionError(
                    "officecli_edit requires an explicit user approval"
                )
        with _locked_files(normalized.files):
            result = _run_officecli(
                settings.binary,
                normalized.argv,
                cwd=active.root,
                timeout_seconds=settings.timeout_write_seconds,
                max_output_bytes=settings.max_output_bytes,
            )
        # T5-3：成功后附带生成文件清单，供图层面挂到最终回答消息、
        # API 层转为可下载附件。
        if result["ok"] and normalized.generated:
            result[GENERATED_FILES_RESULT_KEY] = _generated_file_entries(normalized.generated)
        return result

    return officecli_inspect, officecli_edit


# ── 命令解析与校验（T1-3：动词/选项/batch 三层白名单） ────────────

# 各动词位置参数个数（min, max），文件参数语义见 3.3 节。
_POSITIONAL_ARITY: dict[str, tuple[int, int]] = {
    "help": (0, 3),
    "load_skill": (0, 1),
    "view": (2, 2),
    "get": (1, 2),
    "query": (2, 2),
    "validate": (1, 1),
    "create": (1, 1),
    "set": (2, 2),
    "add": (2, 2),
    "remove": (2, 2),
    "move": (2, 2),
    "swap": (3, 3),
    "batch": (1, 1),
    "import": (2, 3),
    "merge": (2, 2),
}


def _normalize_office_command(
    raw_command: object,
    filesystem: WorkspaceFileSystem,
    *,
    writable: bool,
) -> _NormalizedCommand:
    """校验并把一条 officecli 命令规范化为可执行 argv。

    依次执行：形态/长度检查 → 动词白名单 → 选项白名单（默认拒绝）→
    位置参数个数与 view 模式校验 → 文件参数解析重写 → --prop / batch
    子项 / merge --data 的文件引用校验。任何一步失败抛
    _OfficeCommandError（消息可给模型）或 WorkspaceFileError。
    """
    if not isinstance(raw_command, list) or not raw_command:
        raise _OfficeCommandError(
            'command 必须是非空字符串数组，例如 ["view", "报告.docx", "text"]。'
        )
    if len(raw_command) > MAX_COMMAND_TOKENS:
        raise _OfficeCommandError(f"命令参数过多（上限 {MAX_COMMAND_TOKENS} 个）。")
    tokens: list[str] = []
    for token in raw_command:
        if not isinstance(token, str) or not token.strip():
            raise _OfficeCommandError("command 的每个元素都必须是非空字符串。")
        if "\x00" in token:
            raise _OfficeCommandError("命令参数不允许包含 NUL 字符。")
        tokens.append(token)

    verb = tokens[0].lower()
    allowed_verbs = EDIT_VERBS if writable else READ_VERBS
    if verb not in allowed_verbs:
        if verb in READ_VERBS and writable:
            raise _OfficeCommandError(
                f"动词 {verb} 是只读命令，请改用 officecli_inspect 工具。"
            )
        if verb in EDIT_VERBS and not writable:
            raise _OfficeCommandError(
                f"动词 {verb} 是写入命令，请改用 officecli_edit 工具（需用户批准）。"
            )
        raise _OfficeCommandError(
            f"不支持的动词 {tokens[0]!r}。本工具允许：{'、'.join(sorted(allowed_verbs))}；"
            "save/close/open/watch/raw/raw-set/dump/plugins/mcp 等命令不在集成范围内"
            "（编辑即时落盘，无需保存动作）。"
        )

    positionals, options = _parse_options(verb, tokens[1:])
    _check_positional_arity(verb, positionals)

    files: list[Path] = []
    generated: list[Path] = []
    if verb == "help" or verb == "load_skill":
        pass  # 无文件参数
    elif verb == "merge":
        template = _resolve_existing_office(filesystem, positionals[0], "merge 模板")
        output = _resolve_output_office(filesystem, positionals[1], "merge 输出")
        positionals[0] = str(template)
        positionals[1] = str(output)
        files.extend([template, output])
        generated.append(output)
        _normalize_merge_data(options, filesystem)
    elif verb == "import":
        # officecli V1：import 仅支持 .xlsx 目标（3.2 节）。
        workbook = _resolve_existing_office(filesystem, positionals[0], "import 目标工作簿")
        if workbook.suffix.lower() != ".xlsx":
            raise _OfficeCommandError("import 的目标文件仅支持 .xlsx。")
        positionals[0] = str(workbook)
        files.append(workbook)
        generated.append(workbook)
        if len(positionals) == 3:
            source = _resolve_existing_file(
                filesystem,
                positionals[2],
                IMPORT_SOURCE_EXTENSIONS,
                "import 数据源",
            )
            positionals[2] = str(source)
    elif verb == "create":
        target = _resolve_output_office(filesystem, positionals[0], "create 目标")
        positionals[0] = str(target)
        files.append(target)
        generated.append(target)
    else:
        main_file = (
            _resolve_existing_office(filesystem, positionals[0], f"{verb} 目标文件")
            if not writable
            else _resolve_writable_existing_office(
                filesystem, positionals[0], f"{verb} 目标文件"
            )
        )
        positionals[0] = str(main_file)
        files.append(main_file)
        if writable:
            generated.append(main_file)
        if verb == "view":
            mode = positionals[1].lower()
            if mode not in VIEW_MODES:
                raise _OfficeCommandError(
                    f"view 模式 {positionals[1]!r} 不允许；本期只支持文本模式："
                    f"{'、'.join(sorted(VIEW_MODES))}（html/截图/预览服务不接入）。"
                )
            positionals[1] = mode
        if verb == "batch":
            _normalize_batch_commands(options, filesystem)

    _validate_prop_options(options, filesystem)

    argv = [verb, *positionals]
    for name, value in options:
        argv.append(name)
        if value is not None:
            argv.append(value)
    return _NormalizedCommand(argv=argv, files=tuple(files), generated=tuple(generated))


def _parse_options(
    verb: str,
    tokens: list[str],
) -> tuple[list[str], list[tuple[str, str | None]]]:
    """把动词后的 token 拆成位置参数与选项，并按白名单校验（默认拒绝）。

    支持 ``--name value`` 与 ``--name=value`` 两种形式；开关选项不接受
    取值。返回 (位置参数列表, [(选项名, 取值或 None)])。
    """
    allowed = _ALLOWED_OPTIONS[verb]
    denied = _DENIED_OPTIONS.get(verb, {})
    positionals: list[str] = []
    options: list[tuple[str, str | None]] = []
    seen_single: set[str] = set()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("-") and token != "-":
            name, separator, inline_value = token.partition("=")
            name = name.lower()
            if name in denied:
                raise _OfficeCommandError(f"选项 {name} 不允许使用：{denied[name]}。")
            if name not in allowed:
                allowed_text = "、".join(sorted(allowed)) if allowed else "无"
                raise _OfficeCommandError(
                    f"动词 {verb} 不允许选项 {name}；该动词允许的选项：{allowed_text}。"
                )
            if name in _FLAG_OPTIONS:
                if separator:
                    raise _OfficeCommandError(f"选项 {name} 是开关，不接受取值。")
                options.append((name, None))
            else:
                if separator:
                    value = inline_value
                else:
                    index += 1
                    if index >= len(tokens):
                        raise _OfficeCommandError(f"选项 {name} 缺少取值。")
                    value = tokens[index]
                _check_token_chars(value, blob=name in _BLOB_OPTIONS)
                options.append((name, value))
            if name not in _REPEATABLE_OPTIONS:
                if name in seen_single:
                    raise _OfficeCommandError(f"选项 {name} 只能出现一次。")
                seen_single.add(name)
        else:
            _check_token_chars(token, blob=False)
            positionals.append(token)
        index += 1
    return positionals, options


def _check_token_chars(token: str, *, blob: bool) -> None:
    """单 token 长度上限；--commands/--data 取值按 M6 放宽到 32K。"""
    limit = MAX_BLOB_TOKEN_CHARS if blob else MAX_TOKEN_CHARS
    if len(token) > limit:
        raise _OfficeCommandError(f"单个参数过长（上限 {limit} 字符）。")


def _check_positional_arity(verb: str, positionals: list[str]) -> None:
    """按 3.3 节位置表校验参数个数。"""
    minimum, maximum = _POSITIONAL_ARITY[verb]
    if not minimum <= len(positionals) <= maximum:
        raise _OfficeCommandError(
            f"动词 {verb} 需要 {minimum}" + (f"~{maximum}" if maximum != minimum else "") +
            f" 个位置参数，实际收到 {len(positionals)} 个。"
        )


def _resolve_existing_office(
    filesystem: WorkspaceFileSystem,
    path: str,
    label: str,
) -> Path:
    """解析必须已存在的 Office 文件（扩展名白名单 .docx/.xlsx/.pptx）。"""
    return _resolve_existing_file(filesystem, path, OFFICE_EXTENSIONS, label)


def _resolve_existing_file(
    filesystem: WorkspaceFileSystem,
    path: str,
    extensions: frozenset[str],
    label: str,
) -> Path:
    """解析必须已存在的文件；扩展名不符时给出模型可读的修正指引。"""
    if Path(path.replace("\\", "/")).suffix.lower() not in extensions:
        raise _OfficeCommandError(
            f"{label}只允许 {'、'.join(sorted(extensions))} 扩展名：{path!r}。"
        )
    try:
        return filesystem.resolve_readable_file(path, allowed_extensions=extensions)
    except WorkspaceFileError as error:
        raise _OfficeCommandError(f"{label}无法解析：{error}") from error


def _resolve_output_office(
    filesystem: WorkspaceFileSystem,
    path: str,
    label: str,
) -> Path:
    """解析允许不存在（可创建）的 Office 输出文件。"""
    if Path(path.replace("\\", "/")).suffix.lower() not in OFFICE_EXTENSIONS:
        raise _OfficeCommandError(
            f"{label}只允许 {'、'.join(sorted(OFFICE_EXTENSIONS))} 扩展名：{path!r}。"
        )
    try:
        return filesystem.resolve_writable_file(
            path,
            allow_create=True,
            allowed_extensions=OFFICE_EXTENSIONS,
        )
    except WorkspaceFileError as error:
        raise _OfficeCommandError(f"{label}无法解析：{error}") from error


def _resolve_writable_existing_office(
    filesystem: WorkspaceFileSystem,
    path: str,
    label: str,
) -> Path:
    """解析必须已存在的可写 Office 文件（写工具的主文件）。"""
    if Path(path.replace("\\", "/")).suffix.lower() not in OFFICE_EXTENSIONS:
        raise _OfficeCommandError(
            f"{label}只允许 {'、'.join(sorted(OFFICE_EXTENSIONS))} 扩展名：{path!r}。"
        )
    try:
        return filesystem.resolve_writable_file(
            path,
            allow_create=False,
            allowed_extensions=OFFICE_EXTENSIONS,
        )
    except WorkspaceFileError as error:
        raise _OfficeCommandError(f"{label}无法解析：{error}") from error


def _looks_like_path(value: str) -> bool:
    """「形如路径」启发式（F1：仅匹配强路径特征，不误杀含 / 的普通文本）。

    命中四类之一即视为文件引用：盘符前缀（C:/x）、"/" 开头（/etc/passwd）、
    "\\" 或 "//" 开头（UNC 路径）、或以常见资源扩展名结尾（image.png、
    data/图.png）。含单个 "/" 的普通文本（TCP/IP、A/B 测试）不再命中。
    """
    if not value:
        return False
    if _DRIVE_PREFIX_RE.match(value):
        return True
    if value.startswith(("/", "\\")):
        return True
    return Path(value).suffix.lower() in _ASSET_EXTENSIONS


def _resolve_prop_file(filesystem: WorkspaceFileSystem, value: str, label: str) -> Path:
    """把 --prop 中的文件引用解析为工作区内已存在文件（高危 H1）。"""
    try:
        return filesystem.resolve_readable_file(value)
    except WorkspaceFileError as error:
        # F1：启发式已收紧到强路径特征，走到这里说明取值确实是路径形态；
        # 提示语只引导改指工作区文件，不再要求模型改写普通文本内容。
        raise _OfficeCommandError(
            f"{label} 的取值形如文件引用，但无法解析为当前会话工作区内的已存在文件"
            f"（{error}）。若确需嵌入资源，请先把文件放入当前会话工作区再引用"
            "其工作区相对路径。"
        ) from error


def _validate_prop_options(
    options: list[tuple[str, str | None]],
    filesystem: WorkspaceFileSystem,
) -> None:
    """校验顶层 --prop：FILE_PROPS 键解析并重写为授权绝对路径；其余键的
    路径形取值必须能解析为工作区内文件，否则拒绝（就地重写 options）。"""
    for index, (name, value) in enumerate(options):
        if name != "--prop" or value is None:
            continue
        key, separator, prop_value = value.partition("=")
        if not separator or not key.strip():
            raise _OfficeCommandError("--prop 取值必须是 key=value 形式。")
        if key.strip().lower() in FILE_PROPS:
            resolved = _resolve_prop_file(filesystem, prop_value, f"--prop {key.strip()}")
            options[index] = (name, f"{key}={resolved}")
        elif _looks_like_path(prop_value):
            # 非 FILE_PROPS 键只做存在性校验，不改写取值（见 3.4 第 2 条）
            _resolve_prop_file(filesystem, prop_value, f"--prop {key.strip()}")


def _normalize_batch_commands(
    options: list[tuple[str, str | None]],
    filesystem: WorkspaceFileSystem,
) -> None:
    """校验 batch --commands：JSON 对象数组 + 子命令白名单 + 子项 props
    文件引用校验；通过后就地重写为规范 JSON 字符串（高危 H1/H2）。"""
    commands_values = [value for name, value in options if name == "--commands"]
    if not commands_values:
        raise _OfficeCommandError("batch 必须携带 --commands（内联 JSON 数组）。")
    raw_value = commands_values[0]
    if raw_value is None:
        raise _OfficeCommandError("batch --commands 缺少取值。")
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError as error:
        raise _OfficeCommandError(
            f"batch --commands 必须是合法 JSON 数组：第 {error.lineno} 行解析失败。"
        ) from error
    if not isinstance(parsed, list) or not parsed:
        raise _OfficeCommandError("batch --commands 必须是非空 JSON 对象数组。")
    if len(parsed) > _MAX_BATCH_ITEMS:
        raise _OfficeCommandError(f"batch 子命令过多（上限 {_MAX_BATCH_ITEMS} 条）。")
    for item in parsed:
        if not isinstance(item, dict):
            raise _OfficeCommandError("batch --commands 数组的每一项都必须是对象。")
        item_command = item.get("command")
        if not isinstance(item_command, str) or item_command.lower() not in BATCH_ITEM_VERBS:
            raise _OfficeCommandError(
                f"batch 子命令 {item_command!r} 不允许；允许："
                f"{'、'.join(sorted(BATCH_ITEM_VERBS))}（raw/raw-set/add-part/dump "
                "被显式排除）。"
            )
        item["command"] = item_command.lower()
        # F2：顶层 view 的「仅文本模式」冻结决策在 batch 内部同样生效——
        # officecli 分发器支持 mode:"html"/"svg"，不拦截会被旁路绕过
        # （浪费上下文窗口）。mode 缺省时不强制（officecli 自有默认）。
        item_mode = item.get("mode")
        if item_command.lower() == "view" and item_mode is not None:
            if not isinstance(item_mode, str) or item_mode.lower() not in VIEW_MODES:
                raise _OfficeCommandError(
                    f"batch 子命令 view 的模式 {item_mode!r} 不允许；本期只支持文本"
                    f"模式：{'、'.join(sorted(VIEW_MODES))}。"
                )
            item["mode"] = item_mode.lower()
        item_props = item.get("props")
        if item_props is None:
            continue
        if not isinstance(item_props, dict):
            raise _OfficeCommandError("batch 子命令的 props 必须是 key=value 对象。")
        for prop_key, prop_value in item_props.items():
            if not isinstance(prop_key, str) or not isinstance(prop_value, str):
                raise _OfficeCommandError("batch 子命令的 props 键值都必须是字符串。")
            if prop_key.strip().lower() in FILE_PROPS:
                resolved = _resolve_prop_file(
                    filesystem, prop_value, f"batch 子命令 props.{prop_key}"
                )
                item_props[prop_key] = str(resolved)
            elif _looks_like_path(prop_value):
                _resolve_prop_file(filesystem, prop_value, f"batch 子命令 props.{prop_key}")
    rewritten = json.dumps(parsed, ensure_ascii=False)
    for index, (name, value) in enumerate(options):
        if name == "--commands":
            options[index] = (name, rewritten)
            return


def _normalize_merge_data(
    options: list[tuple[str, str | None]],
    filesystem: WorkspaceFileSystem,
) -> None:
    """校验 merge --data（必填）：工作区 .json 文件重写为授权绝对路径，
    否则必须是可解析为对象的內联 JSON（就地重写 options）。"""
    data_indexes = [index for index, (name, _value) in enumerate(options) if name == "--data"]
    if not data_indexes:
        raise _OfficeCommandError("merge 必须携带 --data（工作区 .json 文件或内联 JSON 对象）。")
    index = data_indexes[0]
    value = options[index][1]
    if value is None:
        raise _OfficeCommandError("merge --data 缺少取值。")
    if value.lower().endswith(".json"):
        resolved = _resolve_existing_file(
            filesystem,
            value,
            MERGE_DATA_EXTENSIONS,
            "merge --data",
        )
        options[index] = ("--data", str(resolved))
        return
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise _OfficeCommandError(
            "merge --data 既不是工作区内 .json 文件，也不是合法的内联 JSON 对象"
            f"（第 {error.lineno} 行解析失败）。"
        ) from error
    if not isinstance(parsed, dict):
        raise _OfficeCommandError("merge --data 的内联 JSON 必须是对象（key→value 映射）。")


# ── per-file 锁（3.8） ──────────────────────────────────────────


@contextmanager
def _locked_files(files: tuple[Path, ...]) -> Iterator[None]:
    """按规范化路径排序后依次加锁，读写同文件全串行、不同文件并行。

    排序获取保证多文件命令（merge 模板+输出）的加锁顺序一致，不会死锁。
    """
    locks: list[threading.Lock] = []
    with _FILE_LOCKS_GUARD:
        for key in sorted({os.path.normcase(str(path)) for path in files}):
            locks.append(_FILE_LOCKS.setdefault(key, threading.Lock()))
    with ExitStack() as stack:
        for lock in locks:
            stack.enter_context(lock)
        yield


# ── 结果辅助 ────────────────────────────────────────────────────


def _command_error_result(error: _OfficeCommandError | WorkspaceFileError) -> dict[str, object]:
    """把校验/路径失败转成稳定结构，模型可据此自我纠正。"""
    if isinstance(error, WorkspaceFileError):
        return error.as_result()
    return {"ok": False, "error_code": "invalid_command", "message": str(error)}


def _generated_file_entries(files: tuple[Path, ...]) -> list[dict[str, object]]:
    """收集生成文件的元数据（T5-3）；文件已被外部删除时静默跳过。"""
    entries: list[dict[str, object]] = []
    for path in files:
        try:
            stat = path.stat()
        except OSError:
            continue
        if not path.is_file():
            continue
        entries.append(
            {
                "path": str(path),
                "name": path.name,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        )
    return entries


def _coerce_and_truncate(value: str | bytes | None, budget_bytes: int) -> tuple[str, bool]:
    """把子进程输出统一成 str 并按 UTF-8 字节预算截断。"""
    if value is None:
        return "", False
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
    encoded = text.encode("utf-8")
    if len(encoded) <= budget_bytes:
        return text, False
    if budget_bytes <= 0:
        return "", True
    return encoded[:budget_bytes].decode("utf-8", errors="ignore"), True


def _positive_int_env(name: str, default: int) -> int:
    """读取正整数环境变量；非法值回退默认（与现状 env 读取的宽容口径一致）。"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        _LOGGER.warning("环境变量 %s=%r 不是整数，使用默认值 %s", name, raw, default)
        return default
    if value <= 0:
        _LOGGER.warning("环境变量 %s=%r 必须为正数，使用默认值 %s", name, raw, default)
        return default
    return value


__all__ = [
    "BATCH_ITEM_VERBS",
    "EDIT_VERBS",
    "EXECUTOR_TIMEOUT_MARGIN_SECONDS",
    "EXPECTED_OFFICECLI_VERSION",
    "FILE_PROPS",
    "GENERATED_FILES_RESULT_KEY",
    "IMPORT_SOURCE_EXTENSIONS",
    "MERGE_DATA_EXTENSIONS",
    "OFFICE_EXTENSIONS",
    "READ_VERBS",
    "VIEW_MODES",
    "OfficeCliSettings",
    "approved_office_execution",
    "create_office_tools",
    "load_officecli_settings",
    "officecli_enabled",
    "resolve_officecli_binary",
]
