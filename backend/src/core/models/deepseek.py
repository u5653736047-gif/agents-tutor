"""从项目 .env 创建 DeepSeek 聊天模型。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from langchain_openai import ChatOpenAI
from pydantic import SecretStr

DEFAULT_ENV_FILE = Path(__file__).resolve().parents[4] / ".env"
ENV_KEYS = ("DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL", "DEEPSEEK_API_KEY")


@dataclass(frozen=True, slots=True)
class DeepSeekSettings:
    """DeepSeek 连接配置；repr 不显示 API Key。"""

    model: str
    base_url: str
    api_key: str = field(repr=False)

    @classmethod
    def from_env(cls, env_file: Path = DEFAULT_ENV_FILE) -> DeepSeekSettings:
        """环境变量优先，其次读取项目根目录的 .env。"""
        file_values = _read_env_file(env_file)
        values = {
            key: os.environ.get(key, file_values.get(key, "")).strip()
            for key in ENV_KEYS
        }
        missing = [key for key in ENV_KEYS if not values[key]]
        if missing:
            raise ValueError(f"缺少 DeepSeek 配置：{', '.join(missing)}")
        return cls(
            model=values["DEEPSEEK_MODEL"],
            base_url=values["DEEPSEEK_BASE_URL"],
            api_key=values["DEEPSEEK_API_KEY"],
        )


def create_deepseek_model(
    settings: DeepSeekSettings | None = None,
) -> ChatOpenAI:
    """创建兼容 OpenAI Tool Calling 协议的 DeepSeek 模型。"""
    config = settings or DeepSeekSettings.from_env()
    return ChatOpenAI(
        model=config.model,
        base_url=config.base_url,
        api_key=SecretStr(config.api_key),
        temperature=0,
        timeout=60,
        max_retries=1,
    )


def _read_env_file(path: Path) -> dict[str, str]:
    """读取本项目需要的简单 KEY=VALUE 配置。"""
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values
