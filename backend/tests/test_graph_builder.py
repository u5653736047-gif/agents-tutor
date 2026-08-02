"""统一 ReAct Agent 的 LangGraph 编排测试。"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.tools import tool

from core.events import ErrorCode, EventType, RunError
from core.graph_builder import CollaborativeAgentGraph
from core.nodes.react_agent import ReActAgentNode
from core.state import AgentRole, create_initial_state


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


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("max_handoffs", 0),
        ("max_handoffs", -1),
        ("max_agent_switches", 0),
        ("max_context_messages", 2),
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
    )

    assert {agent.max_context_messages for agent in builder.agents.values()} == {7}


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

    assert [tool.name for tool in builder.registry.list_tools()] == ["handoff"]


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


@pytest.mark.parametrize("permission_name", ["doubl", "handoff"])
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
