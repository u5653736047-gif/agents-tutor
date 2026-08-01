"""验证 Supervisor 确认闸门（HITL，任务 1.1.4）.

覆盖 确认 / 修正（override）/ 取消 三条恢复路径，
以及无 checkpointer 时不中断的回归行为。
"""

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END
from langgraph.types import Command

from core.graph import build_graph
from core.state import AgentState, TaskContext, TaskStatus, create_initial_state


def _state(
    intent: str = "teach",
    *,
    subtasks: list[str] | None = None,
    metadata: dict | None = None,
) -> AgentState:
    """构造带任务的初始状态."""
    state = create_initial_state(session_id="s1", user_id="u1")
    state["task_context"] = TaskContext(
        intent=intent,
        description="测试任务",
        subtasks=subtasks or ["子任务1"],
        status=TaskStatus.PENDING,
        metadata=metadata or {},
    )
    return state


def _first_interrupt_value(result: dict) -> dict:
    """提取首次中断的 interrupt 载荷（__interrupt__[0].value）."""
    interrupts = result.get("__interrupt__", [])
    assert interrupts, "期望图已中断，但结果中没有 __interrupt__ 字段"
    return interrupts[0].value


def test_hitl_interrupts_before_dispatch() -> None:
    """开启确认闸门后，Supervisor 在分派前暂停并携带完整计划."""
    graph = build_graph(checkpointer=MemorySaver())
    config = {"recursion_limit": 10, "configurable": {"thread_id": "t-introspect"}}

    first = graph.invoke(_state("teach"), config)

    plan = _first_interrupt_value(first)["plan"]
    assert _first_interrupt_value(first)["ask"] == "是否按计划分派？"
    assert plan["intent"] == "teach"
    assert plan["target"] == "teaching_assistant"
    assert plan["subtasks"] == ["子任务1"]


def test_hitl_confirm_dispatches_as_planned() -> None:
    """resume={"confirm": true} 按原计划分派并正常完成."""
    graph = build_graph(checkpointer=MemorySaver())
    config = {"recursion_limit": 10, "configurable": {"thread_id": "t-confirm"}}

    first = graph.invoke(_state("learn"), config)
    assert "__interrupt__" in first

    result = graph.invoke(Command(resume={"confirm": True}), config)

    names = [message.name for message in result["messages"]]
    assert "learning_assistant" in names
    assert result["next_agent"] == END
    assert result["task_context"].status == TaskStatus.COMPLETED


def test_hitl_confirm_with_fan_out_plan() -> None:
    """确认后按 fan-out 计划并行分派全部子任务."""
    state = _state(
        "teach",
        subtasks=["答疑解惑", "制定学习计划", "批改作业"],
        metadata={"subtask_intents": ["teach", "learn", "evaluate"]},
    )
    graph = build_graph(checkpointer=MemorySaver())
    config = {"recursion_limit": 10, "configurable": {"thread_id": "t-confirm-fanout"}}

    first = graph.invoke(state, config)
    plan = _first_interrupt_value(first)["plan"]
    assert plan["subtasks"] == ["答疑解惑", "制定学习计划", "批改作业"]

    result = graph.invoke(Command(resume={"confirm": True}), config)

    names = [message.name for message in result["messages"]]
    assert "learning_assistant" in names
    assert "evaluator" in names
    assert len(result["subtask_results"]) == 3
    assert result["next_agent"] == END


def test_hitl_override_reroutes_to_given_worker() -> None:
    """resume={"override": <合法节点名>} 改用指定目标分派."""
    graph = build_graph(checkpointer=MemorySaver())
    config = {"recursion_limit": 10, "configurable": {"thread_id": "t-override"}}

    first = graph.invoke(_state("teach"), config)
    assert "__interrupt__" in first

    result = graph.invoke(Command(resume={"override": "evaluator"}), config)

    names = [message.name for message in result["messages"]]
    assert "evaluator" in names
    assert "teaching_assistant" not in names
    assert result["next_agent"] == END


def test_hitl_override_cancels_fan_out_plan() -> None:
    """fan-out 计划被 override 时退化为单路径整体分派给指定目标."""
    state = _state(
        "teach",
        subtasks=["子任务A", "子任务B", "子任务C"],
        metadata={"subtask_intents": ["learn", "evaluate", "teach"]},
    )
    graph = build_graph(checkpointer=MemorySaver())
    config = {"recursion_limit": 10, "configurable": {"thread_id": "t-override-fanout"}}

    first = graph.invoke(state, config)
    assert _first_interrupt_value(first)["plan"]["subtasks"] == [
        "子任务A",
        "子任务B",
        "子任务C",
    ]

    result = graph.invoke(Command(resume={"override": "evaluator"}), config)

    names = [message.name for message in result["messages"]]
    assert "learning_assistant" not in names
    assert "teaching_assistant" not in names
    assert names.count("evaluator") == 1  # 仅执行消息（分派消息以 supervisor 名义发出）
    # 整体单路径：不产生并行子任务结果
    assert len(result["subtask_results"]) == 1
    assert result["subtask_results"][0].worker.value == "evaluator"
    assert result["next_agent"] == END


def test_hitl_override_falls_back_when_invalid() -> None:
    """override 目标不在合法节点名内时回退原计划."""
    graph = build_graph(checkpointer=MemorySaver())
    config = {"recursion_limit": 10, "configurable": {"thread_id": "t-override-invalid"}}

    first = graph.invoke(_state("teach"), config)
    assert "__interrupt__" in first

    result = graph.invoke(Command(resume={"override": "nonexistent"}), config)

    names = [message.name for message in result["messages"]]
    assert "teaching_assistant" in names
    assert result["next_agent"] == END


def test_hitl_cancel_writes_message_and_ends() -> None:
    """resume={"cancel": true} 写取消消息、标记任务取消并结束."""
    graph = build_graph(checkpointer=MemorySaver())
    config = {"recursion_limit": 10, "configurable": {"thread_id": "t-cancel"}}

    first = graph.invoke(_state("teach"), config)
    assert "__interrupt__" in first

    result = graph.invoke(Command(resume={"cancel": True}), config)

    names = [message.name for message in result["messages"]]
    assert names == ["supervisor"]
    assert "取消" in result["messages"][-1].content
    assert result["task_context"].status == TaskStatus.CANCELLED
    assert result["next_agent"] == END


def test_no_checkpointer_runs_without_interrupt() -> None:
    """未传 checkpointer 时行为与无 HITL 完全一致：不调用 interrupt."""
    result = build_graph().invoke(_state("learn"))

    assert "__interrupt__" not in result
    names = [message.name for message in result["messages"]]
    assert "learning_assistant" in names
    assert result["next_agent"] == END
