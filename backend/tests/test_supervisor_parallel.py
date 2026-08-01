"""验证 Supervisor 任务分解与并行 fan-out/fan-in（任务 1.1.3）."""

import json
from typing import Any

from langgraph.graph import END
from langgraph.types import Send

from core.graph import build_graph, route_by_next_agent
from core.intent_router import IntentRouter, RuleBasedIntentRouter
from core.nodes import SupervisorNode
from core.state import (
    AgentRole,
    AgentState,
    TaskContext,
    TaskStatus,
    create_initial_state,
)


def _state_with_subtasks(
    intent: str,
    subtasks: list[str],
    *,
    metadata: dict[str, Any] | None = None,
) -> AgentState:
    """构造带子任务计划的任务状态."""
    state = create_initial_state(session_id="s1", user_id="u1")
    state["task_context"] = TaskContext(
        intent=intent,
        description="综合测试任务",
        subtasks=subtasks,
        status=TaskStatus.PENDING,
        metadata=metadata or {},
    )
    return state


class _SpyRouter:
    """记录 classify 调用参数，便于验证子任务独立分类."""

    def __init__(self) -> None:
        self.calls: list[TaskContext | None] = []
        self._inner: IntentRouter = RuleBasedIntentRouter()

    def classify(self, task: TaskContext | None) -> str:
        self.calls.append(task)
        return self._inner.classify(task)


def test_fan_out_dispatches_to_multiple_workers() -> None:
    """多子任务按各自意图并行分派到多个 worker，聚合齐后才结束."""
    state = _state_with_subtasks(
        "teach",
        ["答疑解惑", "制定学习计划", "批改作业"],
        metadata={"subtask_intents": ["teach", "learn", "evaluate"]},
    )

    result = build_graph().invoke(state, {"recursion_limit": 10})

    names = [message.name for message in result["messages"]]
    assert "teaching_assistant" in names
    assert "learning_assistant" in names
    assert "evaluator" in names
    assert len(result["subtask_results"]) == 3
    assert {r.worker for r in result["subtask_results"]} == {
        AgentRole.TEACHING_ASSISTANT,
        AgentRole.LEARNING_ASSISTANT,
        AgentRole.EVALUATOR,
    }
    assert {r.subtask for r in result["subtask_results"]} == {
        "答疑解惑",
        "制定学习计划",
        "批改作业",
    }
    # 汇总消息包含全部子任务 → 证明 Supervisor 等到聚合齐才输出并 END
    last = result["messages"][-1]
    assert last.name == "supervisor"
    assert "答疑解惑" in last.content
    assert "制定学习计划" in last.content
    assert "批改作业" in last.content
    assert result["next_agent"] == END
    assert result["task_context"].status == TaskStatus.COMPLETED


def test_fan_out_same_intent_dispatches_to_same_worker() -> None:
    """未指定子任务意图时继承父意图，同一 worker 并行处理全部子任务."""
    state = _state_with_subtasks("learn", ["学习规划", "疑难点答疑"])

    result = build_graph().invoke(state, {"recursion_limit": 10})

    names = [message.name for message in result["messages"]]
    assert names.count("learning_assistant") == 2
    assert len(result["subtask_results"]) == 2
    assert all(
        r.worker == AgentRole.LEARNING_ASSISTANT for r in result["subtask_results"]
    )
    assert result["next_agent"] == END


def test_fan_out_each_subtask_gets_independent_context() -> None:
    """每个 Send 载荷携带独立子任务上下文：子任务版描述/意图独立分类."""
    spy = _SpyRouter()
    state = _state_with_subtasks(
        "teach",
        ["答疑解惑", "制定学习计划", "批改作业"],
        metadata={"subtask_intents": ["teach", "learn", "evaluate"]},
    )

    build_graph(router=spy).invoke(state, {"recursion_limit": 10})

    # 路由器共被咨询 4 次：父任务 1 次（分派计划）+ 每个子任务各 1 次（独立分类）
    assert len(spy.calls) == 4
    parent, *subtask_calls = spy.calls
    assert parent.description == "综合测试任务"
    assert parent.intent == "teach"
    assert [call.description for call in subtask_calls] == ["答疑解惑", "制定学习计划", "批改作业"]
    assert [call.intent for call in subtask_calls] == ["teach", "learn", "evaluate"]
    assert [call.metadata.get("subtask") for call in subtask_calls] == [
        "答疑解惑",
        "制定学习计划",
        "批改作业",
    ]
    assert [call.metadata.get("subtask_index") for call in subtask_calls] == [0, 1, 2]


def test_single_subtask_keeps_single_path() -> None:
    """单子任务保持现有单路径：一次分派、一个结果、正常结束."""
    state = _state_with_subtasks("teach", ["子任务1"])

    result = build_graph().invoke(state, {"recursion_limit": 10})

    names = [message.name for message in result["messages"]]
    assert names.count("teaching_assistant") == 1
    assert len(result["subtask_results"]) == 1
    # 单路径无 fan-out 标记，SubtaskResult.subtask 记录整体任务描述
    assert result["subtask_results"][0].subtask == "综合测试任务"
    assert result["next_agent"] == END


def test_empty_subtasks_falls_back_to_single_path() -> None:
    """空子任务列表按单路径整体分派."""
    state = _state_with_subtasks("evaluate", [])

    result = build_graph().invoke(state, {"recursion_limit": 10})

    names = [message.name for message in result["messages"]]
    assert "evaluator" in names
    assert result["next_agent"] == END


def test_fan_out_plan_is_serializable_and_edge_rebuilds_sends() -> None:
    """写入状态的 fan-out 计划是纯数据（可 JSON 序列化），条件边重建 Send.

    回归：Send 是 LangGraph 运行时类型，直接写入状态会导致 checkpointer
    序列化失败；状态里只能出现 dict，Send 由 route_by_next_agent 重建。
    """
    node = SupervisorNode()
    state = _state_with_subtasks(
        "teach",
        ["答疑解惑", "制定学习计划"],
        metadata={"subtask_intents": ["teach", "learn"]},
    )

    update = node(state)

    plan = update["extra"]["fan_out"]
    assert update["extra"]["total_subtasks"] == 2
    # 纯数据：JSON 序列化往返不丢信息（checkpointer 兼容性）
    assert json.loads(json.dumps(plan, ensure_ascii=False)) == plan
    assert all(isinstance(item, dict) for item in plan)

    # 条件边读出计划并重建 Send，task_context 还原为 TaskContext 实例
    merged = dict(state)
    merged["extra"] = update["extra"]
    sends = route_by_next_agent(merged)
    assert isinstance(sends, list)
    assert all(isinstance(s, Send) for s in sends)
    assert [s.node for s in sends] == ["teaching_assistant", "learning_assistant"]
    ctx = sends[0].arg["task_context"]
    assert isinstance(ctx, TaskContext)
    assert ctx.description == "答疑解惑"
    assert sends[0].arg["session_id"] == "s1"
