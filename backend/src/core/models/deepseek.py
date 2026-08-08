"""从项目 .env 创建 DeepSeek 聊天模型。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from langchain_openai import ChatOpenAI
from pydantic import SecretStr

DEFAULT_ENV_FILE = Path(__file__).resolve().parents[4] / ".env"  # 从本文件向上定位到项目根目录
ENV_KEYS = ("DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL", "DEEPSEEK_API_KEY")  # 三个必需配置项


@dataclass(frozen=True, slots=True)
class DeepSeekSettings:
    """DeepSeek 连接配置；repr 不显示 API Key。"""

    model: str
    base_url: str
    api_key: str = field(repr=False)

    @classmethod
    def from_env(cls, env_file: Path = DEFAULT_ENV_FILE) -> DeepSeekSettings:
        """环境变量优先，其次读取项目根目录的 .env。"""
        file_values = _read_env_file(env_file)  # .env 里的值作兜底
        values = {
            key: os.environ.get(key, file_values.get(key, "")).strip()  # 系统环境变量优先，.env 其次
            for key in ENV_KEYS
        }
        missing = [key for key in ENV_KEYS if not values[key]]  # 缺任一配置就整体报错
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
    config = settings or DeepSeekSettings.from_env()  # 没传配置就自动从环境/.env 读取
    return ChatOpenAI(
        model=config.model,  # 模型名，如 deepseek-chat
        base_url=config.base_url,  # 直连 DeepSeek 的 API 地址，不走 OpenAI
        api_key=SecretStr(config.api_key),  # SecretStr 防止日志泄露明文 Key
        temperature=0,  # 教学要确定性输出，关闭随机采样
        timeout=60,  # 单次请求最多等 60 秒
        max_retries=1,  # 失败只重试一次，别拖慢课堂
    )


def _read_env_file(path: Path) -> dict[str, str]:
    """读取本项目需要的简单 KEY=VALUE 配置。"""
    if not path.exists():
        return {}  # 没有 .env 就空着，靠系统环境变量兜底

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:  # 跳过空行、注释行和坏格式
            continue
        key, value = line.split("=", 1)  # 只切第一个等号，值里带等号也不影响
        values[key.strip()] = value.strip().strip("\"'")  # 去掉键值两端的空白和引号
    return values
