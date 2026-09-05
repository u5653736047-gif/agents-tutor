"""固定工作流编排测试（lesson-workflow-design M2）。

覆盖：
1. 定义与注册表：预算上限、step_id 唯一、required_params、文件名清洗、
   format_instruction、revise 策略解析；
2. start_workflow 工具：未知 id / 缺工作区拒绝（模型可读 JSON）、
   正常启动产物目录与状态；
3. 端到端（脚本模型）：start_workflow → 四步按序执行 → Supervisor
   收口；事件族齐备；步骤状态与共享消息累积；
4. 调度节点失败策略：abort 熔断、retry 一次、continue 跳过、revise
   回退一轮；
5. 开关等价性：enable_workflows=False 时无 start_workflow 工具、
   Supervisor 角色卡无工作流条款、_route 不进调度节点。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from core.events import ErrorCode, EventType
from core.graph_builder import _ACTIVE_PARENT_STATE, CollaborativeAgentGraph
from core.state import (
    AgentRole,
    WorkflowState,
    WorkflowStatus,
    WorkflowStepState,
    WorkflowStepStatus,
    create_initial_state,
)
from core.workflows import (
    get_workflow,
    register_workflow,
    registered_workflow_ids,
)
from core.workflows.definition import (
    WORKFLOW_ITERATION_HARD_CAP,
    WorkflowDefinition,
    WorkflowStepDefinition,
    sanitize_artifact_filename,
)
from core.workflows.lesson_plan import (
    LESSON_PLAN_WORKFLOW_ID,
    parse_review_verdict,
)
from tests.test_graph_builder import ScriptedModel

# ── 1. 定义与注册表 ──────────────────────────────────────────────


def test_workflow_definition_rejects_duplicate_step_ids() -> None:
    step = WorkflowStepDefinition(
        step_id="same",
        worker_role="teaching_assistant",
        instruction_template="做{topic}",
    )
    with pytest.raises(ValueError):
        WorkflowDefinition(
            workflow_id="dup",
            title="重复步骤",
            steps=(step, step),
        )


def test_workflow_step_budget_capped_by_hard_limit() -> None:
    with pytest.raises(ValueError):
        WorkflowStepDefinition(
            step_id="greedy",
            worker_role="teaching_assistant",
            instruction_template="做",
            iteration_budget=WORKFLOW_ITERATION_HARD_CAP + 1,
        )


def test_required_params_union_of_templates_excluding_scheduler_keys() -> None:
    definition = get_workflow(LESSON_PLAN_WORKFLOW_ID)
    assert definition is not None
    assert definition.required_params() == {"topic", "grade_hint"}


def test_sanitize_artifact_filename_replaces_unsafe_characters() -> None:
    assert (
        sanitize_artifact_filename('教案: 反向传播/BP?<v2>.docx')
        == "教案- 反向传播-BP-v2-.docx"
    )
    with pytest.raises(ValueError):
        sanitize_artifact_filename("。。。")


def test_lesson_plan_revise_policy_parses_review_verdict() -> None:
    definition = get_workflow(LESSON_PLAN_WORKFLOW_ID)
    assert definition is not None
    assert definition.revise_policy is not None
    assert (
        definition.revise_policy(
            3,
            '{"verdict": "revise", "revision_points": ["缺评价设计"]}',
        )
        == 1
    )
    assert definition.revise_policy(3, '{"verdict": "pass"}') is None
    assert definition.revise_policy(3, "模型没按格式输出") is None
    assert definition.revise_policy(0, '{"verdict": "revise"}') is None


def test_parse_review_verdict_tolerates_wrapped_json() -> None:
    parsed = parse_review_verdict(
        '校验完成 {"verdict": "revise", "revision_points": ["缺段"]} 以上'
    )
    assert parsed is not None
    assert parsed["verdict"] == "revise"


def test_registry_rejects_duplicate_registration() -> None:
    step = WorkflowStepDefinition(
        step_id="only",
        worker_role="learning_assistant",
        instruction_template="做",
    )
    definition = WorkflowDefinition(
        workflow_id="temp-dup-test",
        title="临时",
        steps=(step,),
    )
    register_workflow(definition)
    with pytest.raises(ValueError):
        register_workflow(definition)
    assert "temp-dup-test" in registered_workflow_ids()


# ── 2. start_workflow 工具 ───────────────────────────────────────


def _graph(**kwargs: Any) -> CollaborativeAgentGraph:
    return CollaborativeAgentGraph(
        model=ScriptedModel([]),
        orchestration_mode="tool",
        enable_workflows=True,
        **kwargs,
    )


def test_start_workflow_tool_rejects_unknown_id() -> None:
    graph = _graph()
    tool = next(
        item
        for item in graph.agents[AgentRole.SUPERVISOR].tool_executor.registry.list_tools()
        if item.name == "start_workflow"
    )
    output = tool.invoke({"workflow_id": "nope", "topic": "反向传播"})
    assert "未知的工作流" in output


def test_start_workflow_tool_requires_workspace(tmp_path) -> None:
    graph = _graph()
    tool = next(
        item
        for item in graph.agents[AgentRole.SUPERVISOR].tool_executor.registry.list_tools()
        if item.name == "start_workflow"
    )
    output = tool.invoke({"workflow_id": "lesson_plan", "topic": "反向传播"})
    assert "未绑定工作区" in output


def test_start_workflow_tool_builds_state_and_artifact_dir(tmp_path) -> None:
    graph = _graph()
    tool = next(
        item
        for item in graph.agents[AgentRole.SUPERVISOR].tool_executor.registry.list_tools()
        if item.name == "start_workflow"
    )
    # 上一版用例验证了缺工作区的拒绝；此处直接构造带工作区的父状态。
    from core.state import create_initial_state

    parent = create_initial_state(
        session_id="s",
        user_id="u",
        run_id="run-1",
        workspace_root=str(tmp_path),
    )
    token = _ACTIVE_PARENT_STATE.set(parent)
    try:
        output = tool.invoke(
            {"workflow_id": "lesson_plan", "topic": "反向传播"}
        )
    finally:
        _ACTIVE_PARENT_STATE.reset(token)
    # 工具返回值 = WorkflowState JSON + 尾部行为指令（宽容提取首个对象）
    decoded, _ = json.JSONDecoder().raw_decode(output.lstrip())
    workflow = WorkflowState.model_validate(decoded)
    assert "[系统]" in output
    assert workflow.status is WorkflowStatus.RUNNING
    assert [step.step_id for step in workflow.steps] == [
        "collect",
        "draft",
        "generate",
        "review",
    ]
    assert workflow.params["topic"] == "反向传播"
    assert workflow.params["grade_hint"] == ""
    assert workflow.artifact_root is not None
    assert (tmp_path / ".workflow-artifacts" / "run-1").is_dir()


# ── 3. 端到端（脚本模型）─────────────────────────────────────────


def _start_call() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "start_workflow",
                "args": {
                    "workflow_id": "lesson_plan",
                    "topic": "反向传播",
                },
                "id": "start-call",
                "type": "tool_call",
            }
        ],
    )


def _text(text: str) -> AIMessage:
    return AIMessage(content=text)


def test_workflow_runs_steps_and_finalizes(tmp_path) -> None:
    model = ScriptedModel(
        [
            _start_call(),
            _text("正在启动教案工作流。"),
            # collect（teaching_assistant，预算 8）
            _text("素材稿：反向传播核心概念……"),
            # draft
            _text("教案全文：一、教学目标……"),
            # generate
            _text("已生成文档：.workflow-artifacts/run/教案-反向传播.docx"),
            # review（evaluator）
            _text('{"verdict": "pass", "summary": "结构完整"}'),
            # Supervisor 收口
            _text("教案已生成，请下载查看。"),
        ]
    )
    graph = CollaborativeAgentGraph(
        model=model,
        orchestration_mode="tool",
        enable_workflows=True,
    )
    state = graph.run(
        "帮我准备反向传播的教案",
        session_id="wf-e2e",
        workspace_root=str(tmp_path),
    )

    workflow = WorkflowState.model_validate(state["workflow"])
    assert workflow.status is WorkflowStatus.COMPLETED
    assert workflow.current_step_index == 4
    assert [step.status for step in workflow.steps] == [
        WorkflowStepStatus.COMPLETED
    ] * 4
    assert all(step.attempts == 1 for step in workflow.steps)
    assert workflow.steps[0].summary is not None
    assert "素材稿" in workflow.steps[0].summary

    event_types = [event.event_type for event in state["events"]]
    assert EventType.WORKFLOW_STARTED in event_types
    assert event_types.count(EventType.WORKFLOW_STEP_STARTED) == 4
    assert event_types.count(EventType.WORKFLOW_STEP_COMPLETED) == 4
    assert EventType.WORKFLOW_COMPLETED in event_types
    assert EventType.RUN_COMPLETED in event_types
    # 步骤产物在共享 messages 中累积（失忆根因的修正验证）：
    joined = "\n".join(
        str(message.content) for message in state["messages"]
    )
    assert "素材稿" in joined
    assert "教案全文" in joined
    # 收口 Supervisor 的最终回答在历史中
    assert "教案已生成" in joined


def test_supervisor_binds_start_workflow_only_when_enabled() -> None:
    enabled = _graph()
    disabled = CollaborativeAgentGraph(
        model=ScriptedModel([]),
        orchestration_mode="tool",
    )
    supervisor_tools = {
        str(getattr(item, "name", ""))
        for item in enabled.agents[AgentRole.SUPERVISOR].tool_executor.registry.list_tools()
    }
    assert "start_workflow" in supervisor_tools
    disabled_tools = {
        str(getattr(item, "name", ""))
        for item in disabled.agents[AgentRole.SUPERVISOR].tool_executor.registry.list_tools()
    }
    assert "start_workflow" not in disabled_tools
    # 开关等价性：禁用时角色卡不含工作流条款
    from core.nodes.prompts import TOOL_ORCHESTRATION_SUPERVISOR_PROMPT

    assert disabled.agents[AgentRole.SUPERVISOR].system_prompt == (
        TOOL_ORCHESTRATION_SUPERVISOR_PROMPT
    )
    assert enabled.agents[AgentRole.SUPERVISOR].system_prompt != (
        TOOL_ORCHESTRATION_SUPERVISOR_PROMPT
    )


def test_dispatch_node_registered_only_when_enabled() -> None:
    """开关等价性（图结构）：禁用时不注册 workflow_dispatch 节点。"""
    enabled = _graph().build().get_graph().nodes
    disabled = (
        CollaborativeAgentGraph(
            model=ScriptedModel([]),
            orchestration_mode="tool",
        )
        .build()
        .get_graph()
        .nodes
    )
    assert "workflow_dispatch" in enabled
    assert "workflow_dispatch" not in disabled


# ── 4. 调度节点失败策略 ──────────────────────────────────────────


def _workflow_with_failed_step(
    step_index: int,
    attempts: int = 1,
) -> WorkflowState:
    definition = get_workflow(LESSON_PLAN_WORKFLOW_ID)
    assert definition is not None
    steps = [
        WorkflowStepState(
            step_id=step.step_id,
            worker_role=step.worker_role,
            status=(
                WorkflowStepStatus.FAILED if i == step_index
                else WorkflowStepStatus.COMPLETED
            ),
            attempts=attempts if i == step_index else 1,
            summary=(
                "步骤失败：react_iteration_limit" if i == step_index else "完成"
            ),
        )
        for i, step in enumerate(definition.steps)
    ]
    return WorkflowState(
        workflow_id=LESSON_PLAN_WORKFLOW_ID,
        status=WorkflowStatus.RUNNING,
        steps=steps,
        current_step_index=step_index,
        params={"topic": "反向传播", "grade_hint": ""},
    )


def test_dispatch_aborts_workflow_when_first_step_fails() -> None:
    graph = _graph()
    state = {
        "session_id": "s",
        "run_id": "r",
        "events": [],
        "workflow": _workflow_with_failed_step(0),
    }
    updates = graph._workflow_dispatch(state)
    workflow = WorkflowState.model_validate(updates["workflow"])
    assert workflow.status is WorkflowStatus.FAILED
    assert updates["run_error"].error_code is ErrorCode.WORKFLOW_BUDGET_EXCEEDED
    event_types = [event.event_type for event in updates["events"]]
    assert EventType.WORKFLOW_FAILED in event_types


def test_dispatch_retries_failed_step_once() -> None:
    graph = _graph()
    state = {
        "session_id": "s",
        "run_id": "r",
        "events": [],
        "workflow": _workflow_with_failed_step(1, attempts=1),
    }
    updates = graph._workflow_dispatch(state)
    workflow = WorkflowState.model_validate(updates["workflow"])
    # 重试：同一步骤重新分派，attempts 递增到 2
    assert workflow.status is WorkflowStatus.RUNNING
    assert updates["next_agent"] == "teaching_assistant"
    assert workflow.steps[1].attempts == 2
    assert workflow.current_step_index == 1
    assert updates["iteration_budget"] == 4
    event_types = [event.event_type for event in updates["events"]]
    assert EventType.WORKFLOW_STEP_RETRY in event_types

    # 重试再失败（attempts=2）→ abort 收口
    exhausted = _workflow_with_failed_step(1, attempts=2)
    updates2 = graph._workflow_dispatch(
        {"session_id": "s", "run_id": "r", "events": [], "workflow": exhausted}
    )
    assert WorkflowState.model_validate(updates2["workflow"]).status is (
        WorkflowStatus.FAILED
    )


def test_dispatch_continues_past_failed_review() -> None:
    graph = _graph()
    # review（index 3）失败，策略 continue → SKIPPED 后工作流完成收口
    workflow = _workflow_with_failed_step(3, attempts=1)
    updates = graph._workflow_dispatch(
        {"session_id": "s", "run_id": "r", "events": [], "workflow": workflow}
    )
    result = WorkflowState.model_validate(updates["workflow"])
    assert result.status is WorkflowStatus.COMPLETED
    assert result.steps[3].status is WorkflowStepStatus.SKIPPED
    assert updates["next_agent"] == AgentRole.SUPERVISOR.value


def test_dispatch_applies_revise_fallback_once() -> None:
    graph = _graph()
    definition = get_workflow(LESSON_PLAN_WORKFLOW_ID)
    assert definition is not None
    steps = [
        WorkflowStepState(
            step_id=step.step_id,
            worker_role=step.worker_role,
            status=WorkflowStepStatus.COMPLETED,
            attempts=1,
            summary=(
                '{"verdict": "revise", "revision_points": ["缺评价设计"]}'
                if step.step_id == "review"
                else "完成"
            ),
        )
        for step in definition.steps
    ]
    workflow = WorkflowState(
        workflow_id=LESSON_PLAN_WORKFLOW_ID,
        status=WorkflowStatus.RUNNING,
        steps=steps,
        current_step_index=3,
        params={"topic": "反向传播", "grade_hint": ""},
    )
    updates = graph._workflow_dispatch(
        {"session_id": "s", "run_id": "r", "events": [], "workflow": workflow}
    )
    result = WorkflowState.model_validate(updates["workflow"])
    # 回退到 draft（index 1），draft/generate/review 重置并当场分派 draft
    assert result.current_step_index == 1
    assert result.attempts == 1
    assert [step.status for step in result.steps] == [
        WorkflowStepStatus.COMPLETED,
        WorkflowStepStatus.RUNNING,
        WorkflowStepStatus.PENDING,
        WorkflowStepStatus.PENDING,
    ]
    assert result.steps[1].attempts == 1
    assert updates["next_agent"] == "teaching_assistant"

    # 第二次 revise 不再回退（预算 1）：review 完成 → 收口
    workflow2 = WorkflowState(
        workflow_id=LESSON_PLAN_WORKFLOW_ID,
        status=WorkflowStatus.RUNNING,
        steps=steps,
        current_step_index=3,
        attempts=1,
        params={"topic": "反向传播", "grade_hint": ""},
    )
    updates2 = graph._workflow_dispatch(
        {"session_id": "s", "run_id": "r", "events": [], "workflow": workflow2}
    )
    result2 = WorkflowState.model_validate(updates2["workflow"])
    assert result2.status is WorkflowStatus.COMPLETED


def test_dispatch_budget_guard_catches_runaway() -> None:
    graph = _graph()
    definition = get_workflow(LESSON_PLAN_WORKFLOW_ID)
    assert definition is not None
    steps = [
        WorkflowStepState(
            step_id=step.step_id,
            worker_role=step.worker_role,
            status=WorkflowStepStatus.PENDING,
            attempts=99,
        )
        for step in definition.steps
    ]
    workflow = WorkflowState(
        workflow_id=LESSON_PLAN_WORKFLOW_ID,
        status=WorkflowStatus.RUNNING,
        steps=steps,
        current_step_index=0,
    )
    updates = graph._workflow_dispatch(
        {"session_id": "s", "run_id": "r", "events": [], "workflow": workflow}
    )
    assert WorkflowState.model_validate(updates["workflow"]).status is (
        WorkflowStatus.FAILED
    )


# ── 5. Worker 簿记与审批暂停状态 ─────────────────────────────────


def test_worker_updates_mark_paused_approval() -> None:
    graph = _graph()
    definition = get_workflow(LESSON_PLAN_WORKFLOW_ID)
    assert definition is not None
    workflow = definition.build_state({"topic": "t", "grade_hint": ""})
    workflow = workflow.model_copy(
        update={
            "steps": [
                workflow.steps[0].model_copy(
                    update={
                        "status": WorkflowStepStatus.RUNNING,
                        "attempts": 1,
                    }
                ),
                *workflow.steps[1:],
            ],
            "current_step_index": 0,
        }
    )
    agent = graph.agents[AgentRole.TEACHING_ASSISTANT]

    from core.nodes.react_agent import ReActResult
    from core.state import ToolApprovalRequest

    paused_result = ReActResult(
        updates={"pending_tool_approval": ToolApprovalRequest(
            tool_call_id="call-1",
            tool_name="officecli_edit",
            agent_role=AgentRole.TEACHING_ASSISTANT,
            arguments={},
        )},
        metadata={"paused_for_tool_approval": True},
    )
    events: list = []
    updates = graph._workflow_worker_updates(
        {"workflow": workflow},
        agent,
        paused_result,
        lambda *args, **kwargs: events.append((args, kwargs)),
    )
    assert updates is not None
    paused = WorkflowState.model_validate(updates["workflow"])
    assert paused.status is WorkflowStatus.PAUSED_APPROVAL
    assert paused.steps[0].status is WorkflowStepStatus.RUNNING


def test_worker_updates_ignore_other_roles() -> None:
    graph = _graph()
    definition = get_workflow(LESSON_PLAN_WORKFLOW_ID)
    assert definition is not None
    workflow = definition.build_state({"topic": "t", "grade_hint": ""})
    from core.nodes.react_agent import ReActResult

    events: list = []
    updates = graph._workflow_worker_updates(
        {"workflow": workflow},
        graph.agents[AgentRole.EVALUATOR],
        ReActResult(updates={}, messages=[]),
        lambda *args, **kwargs: events.append((args, kwargs)),
    )
    assert updates is None
    assert events == []


# ── 6. Worker 轮可恢复错误路由（稳定性冒烟 2026-08-30 回归锁） ──────
# 根因：工作流 Worker 迭代超限/模型调用失败曾在这道闸被提前判死
# （整轮 run_error），步骤永远停在 RUNNING，on_failure 策略从未执行
# ——真实冒烟 4 条教案 3 条冻结在 review。修复后：错误交工作流簿记
# 落 FAILED → 调度节点执行 retry/continue。


def _workflow_at_review_step() -> WorkflowState:
    definition = get_workflow(LESSON_PLAN_WORKFLOW_ID)
    assert definition is not None
    workflow = definition.build_state({"topic": "t", "grade_hint": ""})
    return workflow.model_copy(
        update={
            "steps": [
                workflow.steps[i].model_copy(
                    update={"status": WorkflowStepStatus.COMPLETED, "attempts": 1}
                )
                for i in range(3)
            ]
            + [
                workflow.steps[3].model_copy(
                    update={"status": WorkflowStepStatus.RUNNING, "attempts": 1}
                )
            ],
            "current_step_index": 3,
        }
    )


def _worker_error_state(workflow: WorkflowState, role: AgentRole) -> dict:
    state = create_initial_state(
        session_id="s", user_id="u", run_id="r", workspace_root="w"
    )
    state["messages"] = [HumanMessage(content="开始工作流")]
    state["current_agent"] = role.value
    state["next_agent"] = role.value
    state["workflow"] = workflow
    return state


def test_workflow_worker_iteration_limit_lands_step_failed_then_skipped() -> None:
    from tests.test_task_aggregation import StubWorker

    graph = _graph()
    worker = StubWorker(AgentRole.EVALUATOR, error=ErrorCode.REACT_ITERATION_LIMIT)
    result = graph._wrap(worker).invoke(
        _worker_error_state(_workflow_at_review_step(), AgentRole.EVALUATOR)
    )  # type: ignore[arg-type]

    # 不再整轮判死：错误落步骤 FAILED，交调度节点执行 on_failure
    assert result["run_error"] is None
    workflow = WorkflowState.model_validate(result["workflow"])
    assert workflow.status is WorkflowStatus.RUNNING
    assert workflow.steps[3].status is WorkflowStepStatus.FAILED

    # review on_failure=continue → SKIPPED → 工作流照常收口
    updates = graph._workflow_dispatch(
        {"session_id": "s", "run_id": "r", "events": [], "workflow": workflow}
    )
    closed = WorkflowState.model_validate(updates["workflow"])
    assert closed.status is WorkflowStatus.COMPLETED
    assert closed.steps[3].status is WorkflowStepStatus.SKIPPED
    assert updates["next_agent"] == "supervisor"


def test_workflow_worker_model_failure_on_retryable_step_redispatches() -> None:
    from tests.test_task_aggregation import StubWorker

    graph = _graph()
    definition = get_workflow(LESSON_PLAN_WORKFLOW_ID)
    assert definition is not None
    workflow = definition.build_state({"topic": "t", "grade_hint": ""})
    workflow = workflow.model_copy(
        update={
            "steps": [
                workflow.steps[0].model_copy(
                    update={"status": WorkflowStepStatus.COMPLETED, "attempts": 1}
                ),
                workflow.steps[1].model_copy(
                    update={"status": WorkflowStepStatus.RUNNING, "attempts": 1}
                ),
                *workflow.steps[2:],
            ],
            "current_step_index": 1,
        }
    )
    worker = StubWorker(
        AgentRole.TEACHING_ASSISTANT, error=ErrorCode.MODEL_CALL_FAILED
    )
    result = graph._wrap(worker).invoke(
        _worker_error_state(workflow, AgentRole.TEACHING_ASSISTANT)
    )  # type: ignore[arg-type]

    assert result["run_error"] is None
    failed = WorkflowState.model_validate(result["workflow"])
    assert failed.steps[1].status is WorkflowStepStatus.FAILED

    # draft on_failure=retry → 重试重新分派（调度节点落回 RUNNING、
    # attempts 递增到 2，与既有 test_dispatch_retries_failed_step_once 同口径）
    updates = graph._workflow_dispatch(
        {"session_id": "s", "run_id": "r", "events": [], "workflow": failed}
    )
    redispatched = WorkflowState.model_validate(updates["workflow"])
    assert redispatched.steps[1].status is WorkflowStepStatus.RUNNING
    assert redispatched.steps[1].attempts == 2
    assert updates["next_agent"] == "teaching_assistant"


def test_workflow_error_exemption_scoped_to_current_step_worker() -> None:
    from tests.test_task_aggregation import StubWorker

    graph = _graph()
    # review 在跑，但出错的是 teaching_assistant（非当前步骤 Worker）
    # → 不豁免，保持整轮判死（豁免范围不放大）
    worker = StubWorker(
        AgentRole.TEACHING_ASSISTANT, error=ErrorCode.REACT_ITERATION_LIMIT
    )
    result = graph._wrap(worker).invoke(
        _worker_error_state(_workflow_at_review_step(), AgentRole.TEACHING_ASSISTANT)
    )  # type: ignore[arg-type]

    assert result["run_error"] is not None
    assert result["run_error"].error_code is ErrorCode.REACT_ITERATION_LIMIT
