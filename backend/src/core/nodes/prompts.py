"""四个角色的极简 Prompt。"""

from ..state import AgentRole

_REACT_RULE = "按需调用工具，观察结果后继续；完成后直接回答。"

ROLE_PROMPTS: dict[AgentRole, str] = {
    AgentRole.SUPERVISOR: (
        f"{_REACT_RULE}\n你是协调者：复杂请求先且仅调用 create_task_plan 创建至少两个"
        "有序子任务，由系统依次分派；简单请求直接调用 handoff；最后汇总。"
    ),
    AgentRole.TEACHING_ASSISTANT: f"{_REACT_RULE}\n你是助教，负责知识讲解与备课支持。",
    AgentRole.LEARNING_ASSISTANT: f"{_REACT_RULE}\n你是助学助手，负责答疑与学习规划。",
    AgentRole.EVALUATOR: f"{_REACT_RULE}\n你是评价助手，负责检查回答质量。",
}

__all__ = ["ROLE_PROMPTS"]
