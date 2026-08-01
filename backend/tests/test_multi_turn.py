"""验证多轮对话场景：跨轮 fan-in 计数隔离与新请求不被吞（P0 回归）.

- 连续两轮 fan-out：第二轮聚合判断不受第一轮残留 subtask_results 污染；
- 任务终结（COMPLETED / CANCELLED）后用户发新消息：入口 ingest 节点重建
  TaskContext，Supervisor 正常分派而非直接 END。
"""

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END
from langgraph.types import Command

from core.graph import build_graph
from core.state import AgentState, TaskContext, TaskStatus, create_initial_state


def _state_with_task(
    intent: str,
    subtasks: list[str],
    *,
    metadata: dict | None = None,
) -> AgentState:
    """构造带子任务计划的任务状态."""
    state = create_initial_state(session_id="s1", user_id="u1")
    state["task_context"] = TaskContext(
        intent=intent,
        description="多轮测试任务",
        subtasks=subtasks,
        status=TaskStatus.PENDING,
        metadata=metadata or {},
    )
    return state


def _next_turn_state(result: AgentState, text: str) -> AgentState:
    """在上一轮结果上追加一条用户消息，构造下一轮输入."""
    state = dict(result)
    state["messages"] = [*result["messages"], HumanMessage(content=text)]
    return AgentState(state)


def test_second_fan_out_round_not_polluted_by_first() -> None:
    """连续两轮 fan-out：第二轮 fan-in 只统计本轮 task_id 的结果."""
    graph = build_graph()
    round1 = graph.invoke(
        _state_with_task("learn", ["第一轮A", "第一轮B"]), {"recursion_limit": 10}
    )
    assert len(round1["subtask_results"]) == 2

    task2 = TaskContext(
        intent="evaluate",
        description="第二轮任务",
        subtasks=["第二轮A", "第二轮B"],
        status=TaskStatus.PENDING,
    )
    state2 = _next_turn_state(round1, "再来一轮")
    state2["task_context"] = task2
    round2 = graph.invoke(state2, {"recursion_limit": 15})

    # 两轮结果都保留在累积通道中
    assert len(round2["subtask_results"]) == 4
    # 本轮结果恰好 2 条且属于 task2（若按全局长度计数会提前聚合错配）
    current = [r for r in round2["subtask_results"] if r.task_id == task2.task_id]
    assert len(current) == 2
    assert {r.subtask for r in current} == {"第二轮A", "第二轮B"}
    # 汇总消息只统计本轮：数量为 2 且不含第一轮子任务文本
    last = round2["messages"][-1]
    assert last.name == "supervisor"
    assert "全部 2 个子任务已完成" in last.content
    assert "第二轮A" in last.content
    assert "第二轮B" in last.content
    assert "第一轮A" not in last.content
    assert round2["next_agent"] == END


def test_new_message_after_completion_dispatches_new_task() -> None:
    """任务完成后用户发新消息（不带新 task_context）：ingest 重建任务并正常分派."""
    graph = build_graph()
    round1 = graph.invoke(_state_with_task("teach", ["子任务1"]))
    assert round1["task_context"].status == TaskStatus.COMPLETED
    ta_count_round1 = [
        m.name for m in round1["messages"]
    ].count("teaching_assistant")

    round2 = graph.invoke(_next_turn_state(round1, "请讲解反向传播"))

    # 新任务被创建（而非残留 COMPLETED 导致直接 END）
    new_task = round2["task_context"]
    assert new_task.description == "请讲解反向传播"
    assert new_task.task_id != round1["task_context"].task_id
    assert new_task.status == TaskStatus.COMPLETED
    # 兜底意图分派给助教 Agent，且确实再次执行
    ta_count_round2 = [
        m.name for m in round2["messages"]
    ].count("teaching_assistant")
    assert ta_count_round2 == ta_count_round1 + 1
    assert round2["next_agent"] == END


def test_new_message_after_cancel_dispatches_new_task() -> None:
    """任务被取消后用户发新消息：同样重建任务并重新走确认闸门."""
    graph = build_graph(checkpointer=MemorySaver())
    config = {"recursion_limit": 10, "configurable": {"thread_id": "t-cancel-retry"}}

    first = graph.invoke(_state_with_task("teach", ["子任务1"]), config)
    assert "__interrupt__" in first
    cancelled = graph.invoke(Command(resume={"cancel": True}), config)
    assert cancelled["task_context"].status == TaskStatus.CANCELLED

    # 新一轮：ingest 检测到新用户消息 + 终结任务 → 重建任务 → 再次中断等待确认
    second = graph.invoke(
        {"messages": [HumanMessage(content="换个问题重新来")]}, config
    )
    assert "__interrupt__" in second

    result = graph.invoke(Command(resume={"confirm": True}), config)
    new_task = result["task_context"]
    assert new_task.description == "换个问题重新来"
    assert new_task.status == TaskStatus.COMPLETED
    names = [m.name for m in result["messages"]]
    assert "teaching_assistant" in names
    assert result["next_agent"] == END


def test_no_new_message_keeps_terminal_shortcut() -> None:
    """任务终结且没有新用户消息时，Supervisor 仍直接 END（原有行为回归）."""
    state = _state_with_task("learn", ["子任务1"])
    state["task_context"] = state["task_context"].model_copy(
        update={"status": TaskStatus.COMPLETED}
    )

    result = build_graph().invoke(state)

    names = [m.name for m in result["messages"]]
    assert names == ["supervisor"]
    assert result["next_agent"] == END
