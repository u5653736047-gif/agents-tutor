"""统一 ReAct Agent 的 LangGraph 编排测试。"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

import core.graph_builder as graph_builder_module
from core.events import ErrorCode, EventType, RunError
from core.filesystem import WorkspaceFileSystem
from core.graph_builder import CollaborativeAgentGraph
from core.nodes.react_agent import ReActAgentNode
from core.state import (
    AgentRole,
    HandoffApprovalAction,
    HandoffApprovalDecision,
    ToolApprovalAction,
    ToolApprovalDecision,
    create_initial_state,
)
from core.tools.shell_tool import create_shell_tool


@tool
def double(value: int) -> int:
    """返回输入数字的两倍。"""
    return value * 2


class ScriptedModel:
    """按图执行顺序返回预设模型消息。"""

    def __init__(self, responses: Sequence[AIMessage]) -> None:
        self.responses = list(responses)
        self.calls: list[list[BaseMessage]] = []
        self.bound_tool_names: list[str] = []

    def bind_tools(self, tools: Sequence[object]) -> ScriptedModel:
        self.bound_tool_names = [str(getattr(tool, "name", "")) for tool in tools]
        return self

    def invoke(self, messages: list[BaseMessage]) -> AIMessage:
        self.calls.append(list(messages))
        return self.responses.pop(0)


class FailingModel:
    """模拟携带敏感细节的模型调用错误。"""

    def invoke(self, messages: list[BaseMessage]) -> AIMessage:
        raise RuntimeError("secret=/srv/private/model-token")


class ToolAwareStreamingModel(FakeListChatModel):
    """FakeListChatModel 加上测试所需的工具绑定兼容层。"""

    def bind_tools(
        self,
        tools: Sequence[object],
        **kwargs: Any,
    ) -> ToolAwareStreamingModel:
        return self


def count_context_messages(messages: Sequence[BaseMessage]) -> int:
    return len(messages)


def handoff_response(target: str = "teaching_assistant") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "handoff",
                "args": {"target": target},
                "id": "handoff-approval",
                "type": "tool_call",
            }
        ],
    )


def subagent_response(
    tool_name: str = "ask_learning_assistant",
    task: str = "解释梯度下降",
) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": tool_name,
                "args": {"task": task},
                "id": "subagent-call",
                "type": "tool_call",
            }
        ],
    )


def shell_response(command: str = "echo approved") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "shell",
                "args": {
                    "command": command,
                    "cwd": ".",
                    "description": "verify the project",
                    "timeout_seconds": 10,
                },
                "id": "shell-call-1",
                "type": "tool_call",
            }
        ],
    )


def test_shell_approval_pauses_without_replaying_the_model(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    model = ScriptedModel(
        [
            shell_response(),
            AIMessage(content="命令完成，继续整合回答。"),
        ]
    )
    shell = create_shell_tool(WorkspaceFileSystem(workspace))
    graph = CollaborativeAgentGraph(
        model=model,
        tools=[shell],
        tool_permissions={"shell": {AgentRole.SUPERVISOR}},
        checkpointer=InMemorySaver(),
        orchestration_mode="tool",
    )
    session_id = "shell-approval"

    paused = graph.run(
        "检查项目",
        session_id,
        workspace_root=str(workspace),
    )
    pending = graph.get_pending_tool_approval(session_id)

    assert len(model.calls) == 1
    assert paused["pending_tool_approval"] is not None
    assert pending is not None
    assert pending.request.tool_call_id == "shell-call-1"
    assert pending.request.arguments["command"] == "echo approved"

    result = graph.resume_tool_approval(
        session_id,
        ToolApprovalDecision(
            interrupt_id=pending.interrupt_id,
            action=ToolApprovalAction.CONFIRM,
        ),
    )

    assert len(model.calls) == 2
    assert result["pending_tool_approval"] is None
    assert result["messages"][-1].content == "命令完成，继续整合回答。"
    assert sum(
        event.event_type is EventType.TOOL_COMPLETED
        and event.tool_call_id == "shell-call-1"
        for event in result["events"]
    ) == 1
    assert any("approved" in str(message.content) for message in model.calls[1])


def test_interrupt_identifier_supports_langgraph_0_4_shape() -> None:
    class LegacyInterrupt:
        interrupt_id = "legacy-interrupt"

    assert (
        graph_builder_module._interrupt_identifier(LegacyInterrupt())
        == "legacy-interrupt"
    )


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("max_handoffs", 0),
        ("max_handoffs", -1),
        ("max_agent_switches", 0),
        ("max_context_messages", 2),
        ("max_context_tokens", 0),
        ("tool_timeout_seconds", 0),
    ],
)
def test_graph_rejects_non_positive_limits(option: str, value: int) -> None:
    kwargs = {option: value}

    with pytest.raises(ValueError, match=option):
        CollaborativeAgentGraph(model=ScriptedModel([]), **kwargs)


def test_graph_forwards_context_window_to_every_agent() -> None:
    builder = CollaborativeAgentGraph(
        model=ScriptedModel([]),
        max_context_messages=7,
        max_context_tokens=100,
        context_token_counter=count_context_messages,
    )

    assert {agent.max_context_messages for agent in builder.agents.values()} == {7}
    assert {agent.max_context_tokens for agent in builder.agents.values()} == {100}
    assert {
        agent.context_token_counter for agent in builder.agents.values()
    } == {count_context_messages}


def test_handoff_interrupt_requires_checkpointer() -> None:
    model = ScriptedModel([])

    with pytest.raises(ValueError, match=r"interrupt_before_handoff.*checkpointer"):
        CollaborativeAgentGraph(
            model=model,
            interrupt_before_handoff=True,
        )

    assert model.calls == []


def test_tool_orchestration_waits_for_subagent_then_supervisor_integrates() -> None:
    """子代理是同步工具：完成结果回到 Supervisor 后，本轮才结束。"""
    model = ScriptedModel(
        [
            subagent_response(),
            AIMessage(content="子代理给出的梯度下降讲解"),
            AIMessage(content="整合回答：子代理给出的梯度下降讲解"),
        ]
    )
    graph = CollaborativeAgentGraph(model=model, orchestration_mode="tool")

    state = graph.run("请解释梯度下降", session_id="tool-session")

    assert "ask_learning_assistant" in model.bound_tool_names
    subagent_tool = graph.registry.get("ask_learning_assistant")
    assert subagent_tool is not None
    assert subagent_tool.extras.get("subagent") is True
    assert len(model.calls) == 3
    assert "助学助手" in str(model.calls[1][0].content)
    assert "子代理给出的梯度下降讲解" in str(model.calls[2])
    assert state["current_agent"] == AgentRole.SUPERVISOR.value
    assert state.get("pending_handoff") is None
    assert state.get("next_agent") is None
    assert state["messages"][-1].content == "整合回答：子代理给出的梯度下降讲解"
    assert [event.event_type for event in state["events"]] == [
        EventType.AGENT_STARTED,
        EventType.TOOL_STARTED,
        EventType.AGENT_STARTED,
        EventType.AGENT_COMPLETED,
        EventType.TOOL_COMPLETED,
        EventType.AGENT_COMPLETED,
        EventType.RUN_COMPLETED,
    ]
    assert [event.agent for event in state["events"][:6]] == [
        AgentRole.SUPERVISOR.value,
        AgentRole.SUPERVISOR.value,
        AgentRole.LEARNING_ASSISTANT.value,
        AgentRole.LEARNING_ASSISTANT.value,
        AgentRole.SUPERVISOR.value,
        AgentRole.SUPERVISOR.value,
    ]
    parent_tool_call_id = state["events"][1].tool_call_id
    assert parent_tool_call_id is not None
    assert [event.parent_tool_call_id for event in state["events"][2:4]] == [
        parent_tool_call_id,
        parent_tool_call_id,
    ]
    assert state["events"][4].tool_call_id == parent_tool_call_id
    assert state["events"][4].parent_tool_call_id is None


def test_graph_stream_exposes_model_chunks_with_agent_metadata() -> None:
    """真实 LangChain ChatModel 即使用 invoke，也由 LangGraph 逐 chunk 转发。"""
    graph = CollaborativeAgentGraph(
        model=ToolAwareStreamingModel(responses=["stream"]),
        orchestration_mode="tool",
    )

    items = list(graph.stream("hello", session_id="stream-session"))
    message_items = [data for mode, data in items if mode == "messages"]
    chunks = [data[0] for data in message_items]
    metadata = [data[1] for data in message_items]

    assert "".join(
        chunk.content for chunk in chunks if isinstance(chunk.content, str)
    ) == "stream"
    assert {item.get("agent_role") for item in metadata} == {
        AgentRole.SUPERVISOR.value
    }


def test_handoff_interrupts_before_worker_dispatch() -> None:
    model = ScriptedModel(
        [handoff_response(), AIMessage(content="分派提案已生成")]
    )
    graph = CollaborativeAgentGraph(
        model=model,
        checkpointer=InMemorySaver(),
        interrupt_before_handoff=True,
    )
    session_id = "approval-paused"
    user_id = "user-1"

    paused = graph.run("请解释梯度下降", session_id, user_id)
    snapshot = graph.build().get_state(graph._thread_config(session_id, user_id))
    pending = graph.get_pending_handoff(session_id, user_id=user_id)
    proposal = paused["pending_handoff"]

    assert len(model.calls) == 2
    assert all("协调者" in str(call[0].content) for call in model.calls)
    assert snapshot.next == ("handoff_approval",)
    assert len(snapshot.interrupts) == 1
    assert snapshot.interrupts[0].value == {
        "target_agent": "teaching_assistant",
        "task_content": "请解释梯度下降",
    }
    assert proposal is not None
    assert proposal.target_agent is AgentRole.TEACHING_ASSISTANT
    assert proposal.task_content == "请解释梯度下降"
    assert pending is not None
    assert pending.interrupt_id == graph_builder_module._interrupt_identifier(
        snapshot.interrupts[0]
    )
    assert pending.request == proposal
    assert paused["handoff_count"] == 0
    assert paused["agent_switch_count"] == 0
    assert len(paused["tool_results"]) == 1
    assert not any(
        event.event_type is EventType.AGENT_SWITCHED
        for event in paused["events"]
    )


def test_confirmed_handoff_dispatches_once_and_finishes() -> None:
    model = ScriptedModel(
        [
            handoff_response(),
            AIMessage(content="分派提案已生成"),
            AIMessage(content="教学结果"),
            AIMessage(content="最终汇总"),
        ]
    )
    graph = CollaborativeAgentGraph(
        model=model,
        checkpointer=InMemorySaver(),
        interrupt_before_handoff=True,
    )
    session_id = "approval-confirmed"
    user_id = "user-1"
    graph.run("请解释梯度下降", session_id, user_id)
    pending = graph.get_pending_handoff(session_id, user_id=user_id)
    assert pending is not None
    decision = HandoffApprovalDecision(
        interrupt_id=pending.interrupt_id,
        action=HandoffApprovalAction.CONFIRM,
    )

    result = graph.resume_handoff(session_id, decision, user_id=user_id)

    assert [
        "协调者" if "协调者" in str(call[0].content) else "助教"
        for call in model.calls
    ] == ["协调者", "协调者", "助教", "协调者"]
    assert len(result["tool_results"]) == 1
    assert result["pending_handoff"] is None
    assert result["handoff_count"] == 1
    assert result["agent_switch_count"] == 2
    assert result["messages"][-1].content == "最终汇总"
    assert [event.sequence for event in result["events"]] == list(
        range(len(result["events"]))
    )
    assert sum(
        event.event_type is EventType.AGENT_SWITCHED
        and event.agent == AgentRole.TEACHING_ASSISTANT.value
        for event in result["events"]
    ) == 1


def test_rejected_handoff_terminates_without_worker_dispatch() -> None:
    model = ScriptedModel(
        [handoff_response(), AIMessage(content="分派提案已生成")]
    )
    graph = CollaborativeAgentGraph(
        model=model,
        checkpointer=InMemorySaver(),
        interrupt_before_handoff=True,
    )
    session_id = "approval-rejected"
    user_id = "user-1"
    graph.run("请解释梯度下降", session_id, user_id)
    pending = graph.get_pending_handoff(session_id, user_id=user_id)
    assert pending is not None
    decision = HandoffApprovalDecision(
        interrupt_id=pending.interrupt_id,
        action=HandoffApprovalAction.REJECT,
    )

    result = graph.resume_handoff(session_id, decision, user_id=user_id)

    assert len(model.calls) == 2
    assert result["pending_handoff"] is None
    assert result["next_agent"] is None
    assert result["handoff_count"] == 0
    assert result["agent_switch_count"] == 0
    assert result["run_error"] is None
    assert result["events"][-1].event_type is EventType.RUN_COMPLETED
    assert graph.build().get_state(
        graph._thread_config(session_id, user_id)
    ).interrupts == ()


def test_modified_handoff_applies_new_target_and_task() -> None:
    model = ScriptedModel(
        [
            handoff_response(),
            AIMessage(content="分派提案已生成"),
            AIMessage(content="评价结果"),
            AIMessage(content="最终汇总"),
        ]
    )
    graph = CollaborativeAgentGraph(
        model=model,
        checkpointer=InMemorySaver(),
        interrupt_before_handoff=True,
    )
    session_id = "approval-modified"
    user_id = "user-1"
    graph.run("请解释梯度下降", session_id, user_id)
    pending = graph.get_pending_handoff(session_id, user_id=user_id)
    assert pending is not None
    decision = HandoffApprovalDecision(
        interrupt_id=pending.interrupt_id,
        action=HandoffApprovalAction.MODIFY,
        target_agent=AgentRole.EVALUATOR,
        task_content="只检查引用完整性",
    )

    result = graph.resume_handoff(session_id, decision, user_id=user_id)

    evaluator_call = model.calls[2]
    assert "评价助手" in str(evaluator_call[0].content)
    assert [
        str(message.content)
        for message in evaluator_call
        if isinstance(message, HumanMessage)
    ] == ["请解释梯度下降", "只检查引用完整性"]
    assert result["task_context"] is not None
    assert result["task_context"].description == "只检查引用完整性"
    switched = [
        event.agent
        for event in result["events"]
        if event.event_type is EventType.AGENT_SWITCHED
    ]
    assert switched == ["evaluator", "supervisor"]


def test_graph_forwards_tool_timeout_configuration_to_shared_executor() -> None:
    builder = CollaborativeAgentGraph(
        model=ScriptedModel([]),
        tools=[double],
        tool_permissions={"double": {AgentRole.EVALUATOR}},
        tool_timeout_seconds=2.0,
        tool_timeouts={"double": 0.25},
    )

    executors = {id(agent.tool_executor) for agent in builder.agents.values()}
    executor = next(iter(builder.agents.values())).tool_executor

    assert len(executors) == 1
    assert executor.timeout_seconds_for("double") == 0.25
    assert executor.timeout_seconds_for("handoff") == 2.0


def test_graph_registry_limits_handoff_to_supervisor() -> None:
    builder = CollaborativeAgentGraph(
        model=ScriptedModel([]),
        tools=[double],
        tool_permissions={"double": {AgentRole.EVALUATOR}},
    )
    registries = {
        id(agent.tool_executor.registry) for agent in builder.agents.values()
    }
    registry = next(iter(builder.agents.values())).tool_executor.registry

    assert len(registries) == 1
    assert registry.is_authorized("handoff", AgentRole.SUPERVISOR)
    assert not registry.is_authorized("handoff", AgentRole.TEACHING_ASSISTANT)
    assert registry.is_authorized("create_task_plan", AgentRole.SUPERVISOR)
    assert not registry.is_authorized(
        "create_task_plan", AgentRole.TEACHING_ASSISTANT
    )
    assert registry.is_authorized("double", AgentRole.EVALUATOR)
    assert not registry.is_authorized("double", AgentRole.SUPERVISOR)


@pytest.mark.parametrize("tool_permissions", [None, {}])
def test_graph_rejects_missing_permissions_for_business_tools(
    tool_permissions: dict[str, set[AgentRole]] | None,
) -> None:
    with pytest.raises(ValueError, match=r"缺少.*double"):
        CollaborativeAgentGraph(
            model=ScriptedModel([]),
            tools=[double],
            tool_permissions=tool_permissions,
        )


def test_graph_accepts_empty_tools_and_permissions() -> None:
    builder = CollaborativeAgentGraph(
        model=ScriptedModel([]),
        tools=[],
        tool_permissions={},
    )

    assert [tool.name for tool in builder.registry.list_tools()] == [
        "handoff",
        "create_task_plan",
        # S2-T1：意图识别工具与既有调度工具一起暴露给 Supervisor
        "detect_intent",
        # S2-T2：学生水平画像工具（仅 Supervisor 可用，与 detect_intent 同约定）
        "detect_level",
        # S2-T3：结构化评价工具（仅 evaluator 可用，与 detect_intent 同约定）
        "submit_evaluation",
        # 六大功能 P2-9：批改工具（仅 evaluator，与 submit_evaluation 同约定；
        # 学习记录工具是条件注册——无 store 注入时不出现在清单里）
        "grade_objective_answers",
        "submit_grading",
    ]


def test_graph_rejects_none_permission_for_business_tool() -> None:
    with pytest.raises(ValueError, match=r"tool_permissions.*double"):
        CollaborativeAgentGraph(
            model=ScriptedModel([]),
            tools=[double],
            tool_permissions={"double": None},  # type: ignore[dict-item]
        )


def test_graph_accepts_explicit_empty_role_set() -> None:
    builder = CollaborativeAgentGraph(
        model=ScriptedModel([]),
        tools=[double],
        tool_permissions={"double": set()},
    )

    assert all(
        not builder.registry.is_authorized("double", role) for role in AgentRole
    )


@pytest.mark.parametrize(
    "permission_name", ["doubl", "handoff", "create_task_plan"]
)
def test_graph_rejects_permissions_for_non_business_tools(
    permission_name: str,
) -> None:
    with pytest.raises(ValueError, match=permission_name):
        CollaborativeAgentGraph(
            model=ScriptedModel([]),
            tools=[double],
            tool_permissions={permission_name: {AgentRole.SUPERVISOR}},
        )


def test_graph_routes_worker_back_to_supervisor() -> None:
    model = ScriptedModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "handoff",
                        "args": {"target": "teaching_assistant"},
                        "id": "handoff-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="任务已分派"),
            AIMessage(content="教学结果"),
            AIMessage(content="最终汇总"),
        ]
    )
    builder = CollaborativeAgentGraph(model=model)
    app = builder.build()
    state = create_initial_state(session_id="graph-test")
    state["messages"] = [HumanMessage(content="请解释梯度下降")]

    result = app.invoke(state)

    assert {type(agent) for agent in builder.agents.values()} == {ReActAgentNode}
    assert set(builder.agents) == set(AgentRole)
    assert "handoff" in model.bound_tool_names
    assert result["current_agent"] == "supervisor"
    assert result["next_agent"] is None
    assert result["messages"][-1].content == "最终汇总"
    assert len(result["tool_results"]) == 1
    assert len(model.calls) == 4
    assert result["handoff_count"] == 1
    assert result["agent_switch_count"] == 2
    assert result["run_error"] is None
    assert [event.sequence for event in result["events"]] == list(
        range(len(result["events"]))
    )
    graph_events = [
        event
        for event in result["events"]
        if event.event_type
        in {EventType.AGENT_SWITCHED, EventType.RUN_COMPLETED}
    ]
    assert [event.event_type for event in graph_events] == [
        EventType.AGENT_SWITCHED,
        EventType.AGENT_SWITCHED,
        EventType.RUN_COMPLETED,
    ]
    assert [event.agent for event in graph_events[:2]] == [
        "teaching_assistant",
        "supervisor",
    ]
    assert graph_events[-1].success is True


def test_graph_stops_with_structured_error_at_handoff_limit() -> None:
    model = ScriptedModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "handoff",
                        "args": {"target": "teaching_assistant"},
                        "id": "handoff-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="first handoff"),
            AIMessage(content="worker result"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "handoff",
                        "args": {"target": "evaluator"},
                        "id": "handoff-2",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="second handoff"),
            AIMessage(content="must not run"),
            AIMessage(content="must not finish"),
        ]
    )
    builder = CollaborativeAgentGraph(model=model, max_handoffs=1)

    result = builder.run("test", session_id="handoff-limit")

    assert len(model.calls) == 5
    assert result["handoff_count"] == 1
    assert result["agent_switch_count"] == 2
    assert result["next_agent"] is None
    assert result["run_error"] is not None
    assert result["run_error"].error_code is ErrorCode.GRAPH_HANDOFF_LIMIT
    assert result["events"][-1].event_type is EventType.RUN_FAILED
    assert result["events"][-1].success is False
    assert result["events"][-1].error_code is ErrorCode.GRAPH_HANDOFF_LIMIT


def test_graph_stops_with_structured_error_at_switch_limit() -> None:
    model = ScriptedModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "handoff",
                        "args": {"target": "teaching_assistant"},
                        "id": "handoff-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="handoff"),
            AIMessage(content="worker result"),
            AIMessage(content="must not finish"),
        ]
    )
    builder = CollaborativeAgentGraph(model=model, max_agent_switches=1)

    result = builder.run("test", session_id="switch-limit")

    assert len(model.calls) == 3
    assert result["handoff_count"] == 1
    assert result["agent_switch_count"] == 1
    assert result["current_agent"] == "teaching_assistant"
    assert result["next_agent"] is None
    assert result["run_error"] is not None
    assert result["run_error"].error_code is ErrorCode.GRAPH_SWITCH_LIMIT
    assert result["events"][-1].event_type is EventType.RUN_FAILED
    assert result["events"][-1].error_code is ErrorCode.GRAPH_SWITCH_LIMIT


def test_graph_converts_agent_error_to_failed_run() -> None:
    secret = "/srv/private/model-token"
    builder = CollaborativeAgentGraph(model=FailingModel())

    result = builder.run("test", session_id="model-failure")

    assert result["current_agent"] == "supervisor"
    assert result["next_agent"] is None
    assert result["run_error"] is not None
    assert result["run_error"].error_code is ErrorCode.MODEL_CALL_FAILED
    assert result["run_error"].message == "模型调用失败"
    assert secret not in str(result)
    assert [event.event_type for event in result["events"]] == [
        EventType.AGENT_STARTED,
        EventType.AGENT_COMPLETED,
        EventType.RUN_FAILED,
    ]
    assert result["events"][-2].success is False
    assert result["events"][-1].success is False


def test_graph_converts_invalid_existing_target_without_calling_model() -> None:
    model = ScriptedModel([AIMessage(content="must not run")])
    builder = CollaborativeAgentGraph(model=model)
    state = create_initial_state(session_id="invalid-target")
    state["messages"] = [HumanMessage(content="test")]
    state["current_agent"] = AgentRole.SUPERVISOR.value
    state["next_agent"] = "rogue_agent"

    result = builder.build().invoke(state)

    assert model.calls == []
    assert result["next_agent"] is None
    assert result["run_error"] is not None
    assert result["run_error"].error_code is ErrorCode.GRAPH_INVALID_TARGET
    assert result["events"][-1].event_type is EventType.RUN_FAILED
    assert result["events"][-1].error_code is ErrorCode.GRAPH_INVALID_TARGET


def test_supervisor_clears_stale_next_agent_before_finishing() -> None:
    model = ScriptedModel([AIMessage(content="done")])
    builder = CollaborativeAgentGraph(model=model)
    state = create_initial_state(session_id="stale-route")
    state["messages"] = [HumanMessage(content="test")]
    state["current_agent"] = AgentRole.SUPERVISOR.value
    state["next_agent"] = AgentRole.SUPERVISOR.value

    result = builder.build().invoke(state)

    assert len(model.calls) == 1
    assert result["next_agent"] is None
    assert result["agent_switch_count"] == 0
    assert result["events"][-1].event_type is EventType.RUN_COMPLETED


def test_route_does_not_raise_for_invalid_target() -> None:
    state = create_initial_state()
    state["current_agent"] = AgentRole.SUPERVISOR.value
    state["next_agent"] = "rogue_agent"

    assert CollaborativeAgentGraph._route(state) == "end"


def test_graph_fails_fast_when_state_already_has_run_error() -> None:
    model = ScriptedModel([AIMessage(content="must not run")])
    builder = CollaborativeAgentGraph(model=model)
    existing_error = RunError(
        error_code=ErrorCode.MODEL_CALL_FAILED,
        message="existing failure",
        agent=AgentRole.SUPERVISOR.value,
    )
    state = create_initial_state(session_id="existing-error")
    state["messages"] = [HumanMessage(content="test")]
    state["run_error"] = existing_error

    result = builder.build().invoke(state)

    assert model.calls == []
    assert result["run_error"] == existing_error
    assert result["next_agent"] is None
