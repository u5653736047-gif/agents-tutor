"""pytest 共享夹具与导入路径配置。

把 backend/scripts/ 加入 sys.path，使测试可以直接 `from ingest_books import ...`
（scripts/ 不是已安装包，无法像 core 那样直接导入；集中在这里注入，
测试文件本身的 import 块保持纯净，不触发 E402/I001 类问题）。
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
