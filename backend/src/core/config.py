"""统一配置加载（任务 1.1.3 扩展）.

从项目根目录的 ``.env`` 文件加载环境变量，供所有需要请求模型 /
外部服务的模块统一使用。当前承载 DeepSeek LLM 的配置项。

设计约定：
- 模块导入时加载一次（幂等），``os.environ`` 中已存在的值优先，
  不覆盖（与 python-dotenv 默认行为一致）；
- 未找到 ``.env`` 文件时静默跳过，不影响任何功能；
- 未来新增模型 / 服务配置时在此集中声明键名。
"""

from __future__ import annotations

import os
from pathlib import Path

# DeepSeek LLM 配置键名（与项目根 .env 文件对应）
ENV_DEEPSEEK_BASE_URL = "DEEPSEEK_BASE_URL"
ENV_DEEPSEEK_API_KEY = "DEEPSEEK_API_KEY"
ENV_DEEPSEEK_MODEL = "DEEPSEEK_MODEL"

_loaded = False


def load_env() -> None:
    """从项目根 ``.env`` 加载环境变量（幂等，不覆盖已存在的值）."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    env_file = _find_env_file()
    if env_file is None:
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        _apply_line(line)


def _find_env_file() -> Path | None:
    """从当前工作目录向上查找最近的 ``.env`` 文件."""
    current = Path.cwd()
    for parent in (current, *current.parents):
        candidate = parent / ".env"
        if candidate.is_file():
            return candidate
    return None


def _apply_line(line: str) -> None:
    """解析单行 ``KEY=VALUE``；忽略空行与 ``#`` 注释，支持去引号."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return
    key, _, value = stripped.partition("=")
    key = key.strip()
    if not key:
        return
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    if key not in os.environ:
        os.environ[key] = value
