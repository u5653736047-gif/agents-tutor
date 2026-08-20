"""学习记录包：功能 3/4/6 的公共数据底座（六大功能计划 P0-4）。

对外只暴露存储与作用域两个入口（详见 store.py 模块注释）：
- LearningRecordStore：SQLite 单表记录与聚合（预警规则内置）；
- learning_scope：图执行上下文注入的 user_id/session_id 作用域
  （工具层读取，模型不可见不可控）。
"""

from .store import (
    LEARNING_OUTCOMES,
    LEARNING_RECORD_KINDS,
    LearningRecordStore,
    learning_scope,
)

__all__ = [
    "LEARNING_OUTCOMES",
    "LEARNING_RECORD_KINDS",
    "LearningRecordStore",
    "learning_scope",
]
