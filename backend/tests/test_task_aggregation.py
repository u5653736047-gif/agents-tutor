"""计划步骤结果归档与 Supervisor 多结果聚合测试。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)

from core.events import ErrorCode, EventType, RunError
from core.graph_builder import CollaborativeAgentGraph
from core.nodes.react_agent import ReActResult
from core.persistence import open_sqlite_checkpointer
from core.state import (
    AgentRole,
    TaskPlan,
    TaskPlanStatus,
    TaskStepResult,
    create_initial_state,
)

_TASK_RESULTS_MARKER = "[TASK_RESULTS]"


class ScriptedModel:
    """返回消息或抛出真实模型边界异常，并记录可见上下文。"""

    def __init__(self, responses: Sequence[AIMessage | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[list[BaseMessage]] = []

    def bind_tools(self, tools: Sequence[object]) -> ScriptedModel:
        return self

    def invoke(self, messages: list[BaseMessage]) -> AIMessage:
        self.calls.append(list(messages))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class StubWorker:
    """直接返回节点结果，用于验证执行前状态边界。"""

    def __init__(
        self,
        role: AgentRole,
        *,
        response: AIMessage | None = None,
        error: ErrorCode | None = None,
    ) -> None:
        self.role = role
        self.response = response or AIMessage(content="不得执行")
        self.error = error
        self.calls = 0

    def run(self, state: object) -> ReActResult:
        self.calls += 1
        run_error = (
            None
            if self.error is None
            else RunError(
                error_code=self.error,
                message="稳定测试错误",
                agent=self.role.value,
            )
        )
        return ReActResult(
            updates={
                "current_agent": self.role.value,
                "messages": [self.response],
                "tool_results": [],
                "events": [],
                "extra": {},
            },
            messages=[self.response],
            error=run_error,
        )


def _plan_response() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "create_task_plan",
                "args": {
                    "steps": [
                        {
                            "sequence": 1,
                            "description": "讲解梯度下降",
                            "target_agent": "teaching_assistant",
                        },
                        {
                            "sequence": 2,
                            "description": "检查讲解准确性",
                            "target_agent": "evaluator",
                        },
                    ]
                },
                "id": "aggregation-plan",
                "type": "tool_call",
            }
        ],
    )


def _handoff_response() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "handoff",
                "args": {"target": "teaching_assistant"},
                "id": "simple-handoff",
                "type": "tool_call",
            }
        ],
    )


def _normal_responses() -> list[AIMessage]:
    return [
        _plan_response(),
        AIMessage(content="计划已创建"),
        AIMessage(content="教学结果：梯度下降沿负梯度更新"),
        AIMessage(content="评价结果：讲解准确"),
        AIMessage(content="统一回答：讲解内容及评价结论"),
    ]


def _role_name(messages: Sequence[BaseMessage]) -> str:
    prompt = str(messages[0].content)
    if "协调者" in prompt:
        return AgentRole.SUPERVISOR.value
    if "助教" in prompt:
        return AgentRole.TEACHING_ASSISTANT.value
    if "评价助手" in prompt:
        return AgentRole.EVALUATOR.value
    return AgentRole.LEARNING_ASSISTANT.value


def _latest_human(messages: Sequence[BaseMessage]) -> str:
    return next(
        str(message.content)
        for message in reversed(messages)
        if isinstance(message, HumanMessage)
    )


def _aggregation_payload(messages: Sequence[BaseMessage]) -> list[dict[str, object]]:
    marked = [
        str(message.content)
        for message in messages
        if isinstance(message, SystemMessage)
        and message.name == "task_results"
        and str(message.content).startswith(_TASK_RESULTS_MARKER)
    ]
    assert len(marked) == 1
    return json.loads(marked[0].split("\n", 1)[1])


def _validated_results(state: dict[str, object]) -> list[TaskStepResult]:
    return [TaskStepResult.model_validate(item) for item in state["task_results"]]


def test_completed_plan_archives_and_aggregates_results_in_order() -> None:
    model = ScriptedModel(_normal_responses())
    graph = CollaborativeAgentGraph(model=model)

    result = graph.run("请讲解并检查梯度下降", "aggregate-success")
    plan = TaskPlan.model_validate(result["task_plan"])
    task_results = _validated_results(result)
    payload = _aggregation_payload(model.calls[-1])

    assert [_role_name(call) for call in model.calls] == [
        "supervisor",
        "supervisor",
        "teaching_assistant",
        "evaluator",
        "supervisor",
    ]
    assert [
        (
            item.step_sequence,
            item.target_agent,
            item.success,
            item.output,
            item.error_code,
        )
        for item in task_results
    ] == [
        (
            1,
            AgentRole.TEACHING_ASSISTANT,
            True,
            "教学结果：梯度下降沿负梯度更新",
            None,
        ),
        (2, AgentRole.EVALUATOR, True, "评价结果：讲解准确", None),
    ]
    assert [item["step_sequence"] for item in payload] == [1, 2]
    assert [item["description"] for item in payload] == [
        "讲解梯度下降",
        "检查讲解准确性",
    ]
    assert [item["output"] for item in payload] == [
        "教学结果：梯度下降沿负梯度更新",
        "评价结果：讲解准确",
    ]
    assert all(
        _TASK_RESULTS_MARKER not in str(message.content)
        for message in result["messages"]
    )
    assert plan.current_step_index == 2
    assert plan.status is TaskPlanStatus.COMPLETED
    assert result["run_error"] is None
    assert result["messages"][-1].content == "统一回答：讲解内容及评价结论"
    archived = [
        event
        for event in result["events"]
        if event.event_type is EventType.TASK_RESULT_ARCHIVED
    ]
    assert [event.plan_step_sequence for event in archived] == [1, 2]
    assert all(event.success is True for event in archived)
    aggregated = [
        event
        for event in result["events"]
        if event.event_type is EventType.TASK_RESULTS_AGGREGATED
    ]
    assert len(aggregated) == 1
    assert aggregated[0].agent == AgentRole.SUPERVISOR.value
    assert aggregated[0].success is True
    assert aggregated[0].degraded is False
    assert result["events"][-1].event_type is EventType.RUN_COMPLETED
    assert [event.sequence for event in result["events"]] == list(
        range(len(result["events"]))
    )


def test_failed_plan_worker_continues_and_forces_missing_result_notice() -> None:
    secret = "secret=/private/model-token"
    model = ScriptedModel(
        [
            _plan_response(),
            AIMessage(content="计划已创建"),
            RuntimeError(secret),
            AIMessage(content="评价结果：未获得讲解，无法核验"),
            AIMessage(content="已完成评价检查"),
        ]
    )
    graph = CollaborativeAgentGraph(model=model)

    result = graph.run("复杂任务", "aggregate-degraded")
    plan = TaskPlan.model_validate(result["task_plan"])
    task_results = _validated_results(result)
    payload = _aggregation_payload(model.calls[-1])

    assert [_role_name(call) for call in model.calls] == [
        "supervisor",
        "supervisor",
        "teaching_assistant",
        "evaluator",
        "supervisor",
    ]
    assert _latest_human(model.calls[3]) == "检查讲解准确性"
    assert task_results[0].success is False
    assert task_results[0].output is None
    assert task_results[0].error_code is ErrorCode.MODEL_CALL_FAILED
    assert task_results[1].success is True
    assert task_results[1].output == "评价结果：未获得讲解，无法核验"
    assert payload[0]["success"] is False
    assert payload[0]["error_code"] == ErrorCode.MODEL_CALL_FAILED.value
    assert payload[1]["success"] is True
    assert plan.current_step_index == 2
    assert plan.status is TaskPlanStatus.COMPLETED
    assert result["handoff_count"] == 2
    assert result["agent_switch_count"] == 4
    assert result["run_error"] is None
    final_text = str(result["messages"][-1].content)
    assert "已完成评价检查" in final_text
    assert "未完成子任务：#1 讲解梯度下降（model_call_failed）" in final_text
    assert secret not in str(result)
    assert not any(
        event.event_type is EventType.RUN_FAILED for event in result["events"]
    )
    failed_completion = [
        event
        for event in result["events"]
        if event.event_type is EventType.AGENT_COMPLETED
        and event.agent == AgentRole.TEACHING_ASSISTANT.value
    ]
    assert len(failed_completion) == 1
    assert failed_completion[0].success is False
    assert failed_completion[0].error_code is ErrorCode.MODEL_CALL_FAILED
    failed_archive = next(
        event
        for event in result["events"]
        if event.event_type is EventType.TASK_RESULT_ARCHIVED
        and event.plan_step_sequence == 1
    )
    assert failed_archive.success is False
    assert failed_archive.error_code is ErrorCode.MODEL_CALL_FAILED
    aggregated = next(
        event
        for event in result["events"]
        if event.event_type is EventType.TASK_RESULTS_AGGREGATED
    )
    assert aggregated.degraded is True


def test_blank_worker_output_is_archived_as_stable_local_failure() -> None:
    model = ScriptedModel(
        [
            _plan_response(),
            AIMessage(content="计划已创建"),
            AIMessage(content="   "),
            AIMessage(content="评价结果"),
            AIMessage(content="已完成评价"),
        ]
    )
    graph = CollaborativeAgentGraph(model=model)

    result = graph.run("复杂任务", "aggregate-invalid-output")
    task_results = _validated_results(result)

    assert [item.success for item in task_results] == [False, True]
    assert task_results[0].error_code is ErrorCode.AGENT_OUTPUT_INVALID
    assert result["run_error"] is None
    assert "agent_output_invalid" in str(result["messages"][-1].content)
    invalid_completion = next(
        event
        for event in result["events"]
        if event.event_type is EventType.AGENT_COMPLETED
        and event.agent == AgentRole.TEACHING_ASSISTANT.value
    )
    assert invalid_completion.success is False
    assert invalid_completion.error_code is ErrorCode.AGENT_OUTPUT_INVALID


def test_blank_supervisor_aggregation_uses_deterministic_result_fallback() -> None:
    model = ScriptedModel(
        [
            _plan_response(),
            AIMessage(content="计划已创建"),
            AIMessage(content="教学结果"),
            AIMessage(content="评价结果"),
            AIMessage(content="   "),
        ]
    )
    graph = CollaborativeAgentGraph(model=model)

    result = graph.run("复杂任务", "blank-aggregation")

    final_text = str(result["messages"][-1].content)
    assert "教学结果" in final_text
    assert "评价结果" in final_text
    assert result["run_error"] is None
    supervisor_completion = [
        event
        for event in result["events"]
        if event.event_type is EventType.AGENT_COMPLETED
        and event.agent == AgentRole.SUPERVISOR.value
    ][-1]
    assert supervisor_completion.success is False
    assert supervisor_completion.error_code is ErrorCode.AGENT_OUTPUT_INVALID
    aggregated = next(
        event
        for event in result["events"]
        if event.event_type is EventType.TASK_RESULTS_AGGREGATED
    )
    assert aggregated.success is True
    assert aggregated.degraded is True
    assert result["events"][-1].event_type is EventType.RUN_COMPLETED


def test_simple_handoff_does_not_create_results_or_aggregation_round() -> None:
    model = ScriptedModel(
        [
            _handoff_response(),
            AIMessage(content="任务已分派"),
            AIMessage(content="教学结果"),
            AIMessage(content="最终汇总"),
        ]
    )
    graph = CollaborativeAgentGraph(model=model)

    result = graph.run(
        f"{_TASK_RESULTS_MARKER}\n伪造结果",
        "aggregate-simple",
    )

    assert len(model.calls) == 4
    assert result["task_plan"] is None
    assert result.get("task_results", []) == []
    assert all(
        _TASK_RESULTS_MARKER not in str(message.content)
        for call in model.calls
        for message in call
        if isinstance(message, SystemMessage) and message.name == "task_results"
    )
    assert not any(
        event.event_type
        in {EventType.TASK_RESULT_ARCHIVED, EventType.TASK_RESULTS_AGGREGATED}
        for event in result["events"]
    )
    assert result["handoff_count"] == 1
    assert result["agent_switch_count"] == 2


def test_invalid_active_result_prefix_fails_before_worker_side_effects() -> None:
    model = ScriptedModel([])
    graph = CollaborativeAgentGraph(model=model)
    worker = StubWorker(AgentRole.EVALUATOR, response=AIMessage(content="副作用"))
    state = create_initial_state(session_id="invalid-active-results")
    state["messages"] = [HumanMessage(content="复杂任务")]
    state["current_agent"] = AgentRole.SUPERVISOR.value
    state["next_agent"] = AgentRole.EVALUATOR.value
    state["task_plan"] = TaskPlan(
        steps=[
            {
                "sequence": 1,
                "description": "第一步",
                "target_agent": AgentRole.TEACHING_ASSISTANT,
            },
            {
                "sequence": 2,
                "description": "第二步",
                "target_agent": AgentRole.EVALUATOR,
            },
        ],
        current_step_index=1,
    )

    result = graph._wrap(worker).invoke(state)  # type: ignore[arg-type]

    assert worker.calls == 0
    assert result["run_error"] is not None
    assert result["run_error"].error_code is ErrorCode.GRAPH_AGGREGATION_INVALID
    assert result.get("task_results", []) == []


def test_non_model_worker_error_remains_a_global_failure() -> None:
    model = ScriptedModel([])
    graph = CollaborativeAgentGraph(model=model)
    worker = StubWorker(
        AgentRole.TEACHING_ASSISTANT,
        error=ErrorCode.TOOL_EXECUTION_FAILED,
    )
    state = create_initial_state(session_id="non-recoverable-worker-error")
    state["messages"] = [HumanMessage(content="复杂任务")]
    state["current_agent"] = AgentRole.SUPERVISOR.value
    state["next_agent"] = AgentRole.TEACHING_ASSISTANT.value
    state["task_plan"] = TaskPlan(
        steps=[
            {
                "sequence": 1,
                "description": "第一步",
                "target_agent": AgentRole.TEACHING_ASSISTANT,
            },
            {
                "sequence": 2,
                "description": "第二步",
                "target_agent": AgentRole.EVALUATOR,
            },
        ]
    )

    result = graph._wrap(worker).invoke(state)  # type: ignore[arg-type]

    assert worker.calls == 1
    assert result["run_error"] is not None
    assert result["run_error"].error_code is ErrorCode.TOOL_EXECUTION_FAILED
    assert TaskPlan.model_validate(result["task_plan"]).status is TaskPlanStatus.FAILED
    assert result.get("task_results", []) == []


def test_completed_plan_rejects_incomplete_result_mapping_before_model_call() -> None:
    model = ScriptedModel([])
    graph = CollaborativeAgentGraph(model=model)
    state = create_initial_state(session_id="invalid-result-map")
    state["messages"] = [HumanMessage(content="复杂任务")]
    state["current_agent"] = AgentRole.EVALUATOR.value
    state["task_plan"] = TaskPlan(
        steps=[
            {
                "sequence": 1,
                "description": "第一步",
                "target_agent": AgentRole.TEACHING_ASSISTANT,
            },
            {
                "sequence": 2,
                "description": "第二步",
                "target_agent": AgentRole.EVALUATOR,
            },
        ],
        current_step_index=2,
        status=TaskPlanStatus.COMPLETED,
    )
    state["task_results"] = [
        TaskStepResult(
            step_sequence=1,
            target_agent=AgentRole.TEACHING_ASSISTANT,
            success=True,
            output="只有第一步结果",
        )
    ]

    result = graph.build().invoke(state)

    assert model.calls == []
    assert result["run_error"] is not None
    assert (
        result["run_error"].error_code
        is ErrorCode.GRAPH_AGGREGATION_INVALID
    )
    assert TaskPlan.model_validate(result["task_plan"]).status is TaskPlanStatus.FAILED
    assert [event.event_type for event in result["events"]] == [
        EventType.TASK_RESULTS_AGGREGATED,
        EventType.RUN_FAILED,
    ]


@pytest.mark.parametrize(
    "task_results",
    [
        [
            TaskStepResult(
                step_sequence=1,
                target_agent=AgentRole.TEACHING_ASSISTANT,
                success=True,
                output="第一步结果",
            ),
            TaskStepResult(
                step_sequence=1,
                target_agent=AgentRole.EVALUATOR,
                success=True,
                output="重复序号结果",
            ),
        ],
        [
            TaskStepResult(
                step_sequence=2,
                target_agent=AgentRole.TEACHING_ASSISTANT,
                success=True,
                output="错序第一项",
            ),
            TaskStepResult(
                step_sequence=1,
                target_agent=AgentRole.EVALUATOR,
                success=True,
                output="错序第二项",
            ),
        ],
        [
            TaskStepResult(
                step_sequence=1,
                target_agent=AgentRole.EVALUATOR,
                success=True,
                output="错误角色结果",
            ),
            TaskStepResult(
                step_sequence=2,
                target_agent=AgentRole.EVALUATOR,
                success=True,
                output="第二步结果",
            ),
        ],
    ],
    ids=["duplicate-sequence", "out-of-order", "wrong-target"],
)
def test_completed_plan_rejects_equal_length_bad_result_mapping(
    task_results: list[TaskStepResult],
) -> None:
    model = ScriptedModel([])
    graph = CollaborativeAgentGraph(model=model)
    state = create_initial_state(session_id="invalid-equal-length-result-map")
    state["messages"] = [HumanMessage(content="复杂任务")]
    state["current_agent"] = AgentRole.EVALUATOR.value
    state["task_plan"] = TaskPlan(
        steps=[
            {
                "sequence": 1,
                "description": "第一步",
                "target_agent": AgentRole.TEACHING_ASSISTANT,
            },
            {
                "sequence": 2,
                "description": "第二步",
                "target_agent": AgentRole.EVALUATOR,
            },
        ],
        current_step_index=2,
        status=TaskPlanStatus.COMPLETED,
    )
    state["task_results"] = task_results

    result = graph.build().invoke(state)

    assert model.calls == []
    assert result["run_error"] is not None
    assert result["run_error"].error_code is ErrorCode.GRAPH_AGGREGATION_INVALID
    assert TaskPlan.model_validate(result["task_plan"]).status is TaskPlanStatus.FAILED
    assert [event.event_type for event in result["events"]] == [
        EventType.TASK_RESULTS_AGGREGATED,
        EventType.RUN_FAILED,
    ]


def test_completed_plan_rejects_non_recoverable_archived_error() -> None:
    model = ScriptedModel([])
    graph = CollaborativeAgentGraph(model=model)
    state = create_initial_state(session_id="invalid-archived-error")
    state["messages"] = [HumanMessage(content="复杂任务")]
    state["current_agent"] = AgentRole.EVALUATOR.value
    state["task_plan"] = TaskPlan(
        steps=[
            {
                "sequence": 1,
                "description": "第一步",
                "target_agent": AgentRole.TEACHING_ASSISTANT,
            },
            {
                "sequence": 2,
                "description": "第二步",
                "target_agent": AgentRole.EVALUATOR,
            },
        ],
        current_step_index=2,
        status=TaskPlanStatus.COMPLETED,
    )
    state["task_results"] = [
        {
            "step_sequence": 1,
            "target_agent": AgentRole.TEACHING_ASSISTANT.value,
            "success": False,
            "error_code": ErrorCode.TOOL_EXECUTION_FAILED.value,
        },
        {
            "step_sequence": 2,
            "target_agent": AgentRole.EVALUATOR.value,
            "success": True,
            "output": "第二步结果",
        },
    ]

    result = graph.build().invoke(state)

    assert model.calls == []
    assert result["run_error"] is not None
    assert result["run_error"].error_code is ErrorCode.GRAPH_AGGREGATION_INVALID
    assert TaskPlan.model_validate(result["task_plan"]).status is TaskPlanStatus.FAILED
    assert [event.event_type for event in result["events"]] == [
        EventType.TASK_RESULTS_AGGREGATED,
        EventType.RUN_FAILED,
    ]


def test_task_results_survive_sqlite_graph_reopen(tmp_path: Path) -> None:
    checkpoint_path = tmp_path / "aggregation" / "checkpoints.sqlite"
    first_model = ScriptedModel(_normal_responses())

    with open_sqlite_checkpointer(checkpoint_path) as first_saver:
        first_graph = CollaborativeAgentGraph(
            model=first_model,
            checkpointer=first_saver,
        )
        first_graph.run("复杂任务", "persisted-results", "user-1")

    second_model = ScriptedModel([])
    with open_sqlite_checkpointer(checkpoint_path) as second_saver:
        second_graph = CollaborativeAgentGraph(
            model=second_model,
            checkpointer=second_saver,
        )
        restored = second_graph.get_state("persisted-results", "user-1")

    assert restored is not None
    task_results = _validated_results(restored)
    assert [item.step_sequence for item in task_results] == [1, 2]
    assert [item.output for item in task_results] == [
        "教学结果：梯度下降沿负梯度更新",
        "评价结果：讲解准确",
    ]
    assert sum(
        event.event_type is EventType.TASK_RESULTS_AGGREGATED
        for event in restored["events"]
    ) == 1
    assert second_model.calls == []
