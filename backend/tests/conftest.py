"""pytest 共享夹具与导入路径配置。

把 backend/scripts/ 加入 sys.path，使测试可以直接 `from ingest_books import ...`
（scripts/ 不是已安装包，无法像 core 那样直接导入；集中在这里注入，
测试文件本身的 import 块保持纯净，不触发 E402/I001 类问题）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


@pytest.fixture(autouse=True)
def _disable_rag_enhancement_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试环境默认关闭 RAG 增强组件（S5），需要时由测试自行覆盖。

    为什么默认关闭：生产默认 auto（API_KNOWLEDGE_REWRITE /
    API_KNOWLEDGE_RERANK 缺省即启用），但
    - 重排器 auto 构造会联网下载 Cross-Encoder 模型（首次约 280MB），
      而本仓库测试 venv 已安装 fastembed——不关闭则所有 lifespan 测试
      都会触发模型下载，违背「测试不碰真实模型/不联网」的既定约定
      （见 test_api_knowledge_wiring.py 文件头）；
    - 改写器 auto 在配置了 key 的测试里会装配出指向假 key 的模型实例，
      虽无网络调用，但默认关闭让 /healthz 诊断断言保持确定（全 False）。
    生效顺序：本夹具（function 级 autouse）先于测试体内的 monkeypatch
    执行，测试需要启用时用 monkeypatch.setenv 覆盖即可（后者生效）。
    """
    monkeypatch.setenv("API_KNOWLEDGE_REWRITE", "off")
    monkeypatch.setenv("API_KNOWLEDGE_RERANK", "off")
