"""固定工作流注册表（lesson-workflow-design §三）。

注册表是工作流唯一合法入口：Supervisor 的 start_workflow 工具与
_workflow_dispatch 调度节点都只认这里的 id——模型无法触发未注册的
工作流，也无法自造步骤顺序。
"""

from __future__ import annotations

from core.workflows.definition import (
    StepFailurePolicy,
    WorkflowDefinition,
    WorkflowStepDefinition,
    sanitize_artifact_filename,
)
from core.workflows.lesson_plan import (
    LESSON_PLAN_REVISE_ROUNDS,
    LESSON_PLAN_WORKFLOW_ID,
    lesson_plan_workflow,
    parse_review_verdict,
)

_WORKFLOWS: dict[str, WorkflowDefinition] = {}


def register_workflow(definition: WorkflowDefinition) -> None:
    """注册一个工作流定义；重复注册同 id 视为配置错误（fail-fast）。"""
    if definition.workflow_id in _WORKFLOWS:
        raise ValueError(f"workflow already registered: {definition.workflow_id}")
    _WORKFLOWS[definition.workflow_id] = definition


def get_workflow(workflow_id: str) -> WorkflowDefinition | None:
    return _WORKFLOWS.get(workflow_id)


def registered_workflow_ids() -> list[str]:
    return sorted(_WORKFLOWS)


register_workflow(lesson_plan_workflow())

__all__ = [
    "LESSON_PLAN_REVISE_ROUNDS",
    "LESSON_PLAN_WORKFLOW_ID",
    "StepFailurePolicy",
    "WorkflowDefinition",
    "WorkflowStepDefinition",
    "get_workflow",
    "lesson_plan_workflow",
    "parse_review_verdict",
    "register_workflow",
    "registered_workflow_ids",
    "sanitize_artifact_filename",
]
