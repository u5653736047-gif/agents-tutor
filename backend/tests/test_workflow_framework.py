"""PPT 框架增量测试（ppt-workflow-design §五：P1）。

覆盖：output_validator 结构门禁、requires_artifact 落盘闸、
extra_params/param_normalizer 参数管道、调度重试提示注入。
教案工作流零回归由既有测试套保证（这些增量字段教案均不声明）。
"""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from core.graph_builder import CollaborativeAgentGraph
from core.state import (
    AgentRole,
    WorkflowState,
    WorkflowStepStatus,
)
from core.workflows import (
    WorkflowDefinition,
    WorkflowStepDefinition,
    register_workflow,
)
from core.workflows.definition import sanitize_artifact_filename
from tests.test_graph_builder import ScriptedModel

_IDS = itertools.count(1)


_DEFINITION_LEVEL_KEYS = {"extra_params", "param_normalizer"}


def _definition(**kwargs: object) -> WorkflowDefinition:
    step_kwargs = {
        key: value
        for key, value in kwargs.items()
        if key not in _DEFINITION_LEVEL_KEYS
    }
    defn_kwargs = {
        key: value
        for key, value in kwargs.items()
        if key in _DEFINITION_LEVEL_KEYS
    }
    step = WorkflowStepDefinition(
        step_id="solo",
        worker_role=AgentRole.TEACHING_ASSISTANT,
        instruction_template="做{topic}",
        **step_kwargs,
    )
    return WorkflowDefinition(
        workflow_id=f"fw-framework-test-{next(_IDS)}",
        title="框架测试",
        steps=(step,),
        **defn_kwargs,
    )


def _worker_updates(graph, workflow, output, tmp_path=None):
    from core.nodes.react_agent import ReActResult

    events: list = []
    updates = graph._workflow_worker_updates(
        {"workflow": workflow},
        graph.agents[AgentRole.TEACHING_ASSISTANT],
        ReActResult(
            updates={"messages": [], "tool_results": []},
            messages=[AIMessage(content=output)] if output is not None else [],
        ),
        lambda *args, **kwargs: events.append(kwargs),
    )
    return updates


class TestOutputValidator:
    def test_failing_validator_marks_step_failed_without_staging(
        self, tmp_path: Path
    ) -> None:
        definition = _definition(
            output_validator=lambda text: "STRUCT-OK" in text,
        )
        register_workflow(definition)
        graph = CollaborativeAgentGraph(
            model=ScriptedModel([]),
            orchestration_mode="tool",
            enable_workflows=True,
        )
        workflow = definition.build_state({"topic": "t"})
        updates = _worker_updates(graph, workflow, "没有结构标记的输出")
        assert updates is not None
        result: WorkflowState = updates["workflow"]
        assert result.steps[0].status is WorkflowStepStatus.FAILED
        assert "agent_output_invalid" in (result.steps[0].summary or "")
        # 结构不合格的输出不进暂存
        assert "solo" not in result.step_outputs

    def test_passing_validator_completes_and_stages(
        self, tmp_path: Path
    ) -> None:
        definition = _definition(
            output_validator=lambda text: "STRUCT-OK" in text,
        )
        register_workflow(definition)
        graph = CollaborativeAgentGraph(
            model=ScriptedModel([]),
            orchestration_mode="tool",
            enable_workflows=True,
        )
        workflow = definition.build_state({"topic": "t"})
        updates = _worker_updates(graph, workflow, "STRUCT-OK 正文")
        result: WorkflowState = updates["workflow"]
        assert result.steps[0].status is WorkflowStepStatus.COMPLETED
        assert result.step_outputs.get("solo") == "STRUCT-OK 正文"


class TestRequiresArtifact:
    def _workflow(self, definition, tmp_path: Path, prepare) -> WorkflowState:
        workflow = definition.build_state({"topic": "t"})
        zone = tmp_path / "zone"
        zone.mkdir(parents=True, exist_ok=True)
        workflow = workflow.model_copy(
            update={"artifact_root": str(zone), "current_step_index": 0}
        )
        prepare(zone)
        return workflow

    def test_existing_nonempty_file_completes(self, tmp_path: Path) -> None:
        definition = _definition(
            requires_artifact=True,
            artifact_filename_template="out-{topic}.docx",
        )
        register_workflow(definition)
        graph = CollaborativeAgentGraph(
            model=ScriptedModel([]),
            orchestration_mode="tool",
            enable_workflows=True,
        )

        def prepare(zone: Path) -> None:
            target = zone / sanitize_artifact_filename("out-t.docx")
            target.write_text("content", encoding="utf-8")

        workflow = self._workflow(definition, tmp_path, prepare)
        updates = _worker_updates(graph, workflow, "报告完成")
        result: WorkflowState = updates["workflow"]
        assert result.steps[0].status is WorkflowStepStatus.COMPLETED

    def test_missing_file_fails_with_retryable_error(
        self, tmp_path: Path
    ) -> None:
        definition = _definition(
            requires_artifact=True,
            artifact_filename_template="out-{topic}.docx",
        )
        register_workflow(definition)
        graph = CollaborativeAgentGraph(
            model=ScriptedModel([]),
            orchestration_mode="tool",
            enable_workflows=True,
        )
        workflow = self._workflow(definition, tmp_path, lambda zone: None)
        updates = _worker_updates(graph, workflow, "谎报完成")
        result: WorkflowState = updates["workflow"]
        assert result.steps[0].status is WorkflowStepStatus.FAILED
        assert result.steps[0].summary is not None
        assert "agent_output_invalid" in result.steps[0].summary


class TestParamsPipeline:
    def test_undeclared_param_rejected(self) -> None:
        definition = _definition(extra_params=frozenset({"page_count"}))
        register_workflow(definition)
        with pytest.raises(ValueError, match="undeclared"):
            definition.build_state({"topic": "t", "rogue": "x"})

    def test_declared_extra_param_accepted(self) -> None:
        definition = _definition(extra_params=frozenset({"page_count"}))
        register_workflow(definition)
        workflow = definition.build_state({"topic": "t", "page_count": "12"})
        assert workflow.params["page_count"] == "12"

    def test_param_normalizer_applied_before_validation(
        self, tmp_path: Path
    ) -> None:
        definition = _definition(
            extra_params=frozenset({"page_count"}),
            param_normalizer=lambda params: {
                **params,
                "page_count": "12"
                if not str(params.get("page_count", "")).isdigit()
                else params["page_count"],
            },
        )
        register_workflow(definition)
        workflow = definition.build_state({"topic": "t", "page_count": "abc"})
        assert workflow.params["page_count"] == "12"

    def test_start_workflow_rejects_overlong_value(self, tmp_path: Path) -> None:
        graph = CollaborativeAgentGraph(
            model=ScriptedModel([]),
            orchestration_mode="tool",
            enable_workflows=True,
        )
        tool = next(
            item
            for item in graph.agents[
                AgentRole.SUPERVISOR
            ].tool_executor.registry.list_tools()
            if item.name == "start_workflow"
        )
        from core.graph_builder import _ACTIVE_PARENT_STATE
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
                {
                    "workflow_id": "lesson_plan",
                    "topic": "t",
                    "params": {"rogue": "x" * 201},
                }
            )
        finally:
            _ACTIVE_PARENT_STATE.reset(token)
        assert "不合法" in output


class TestRetryHint:
    def test_redispatch_appends_retry_hint(self) -> None:
        """RUNNING 步骤重入分派时注入重试提示（ppt-workflow-design §五-4）。"""
        graph = CollaborativeAgentGraph(
            model=ScriptedModel([]),
            orchestration_mode="tool",
            enable_workflows=True,
        )
        from core.workflows import get_workflow

        definition = get_workflow("lesson_plan")
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
        updates = graph._workflow_dispatch(
            {
                "session_id": "s",
                "run_id": "r",
                "events": [],
                "workflow": workflow,
                "handoff_count": 0,
                "agent_switch_count": 0,
            }
        )
        message = updates["messages"][0]
        assert "[系统] 这是重试" in str(message.content)
