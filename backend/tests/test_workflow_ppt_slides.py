"""ppt_slides 工作流测试（ppt-workflow-design P2）。

覆盖：parse_deck_outline 的宽容提取/硬失败/收敛截断；工作流定义注册
（结构门禁与落盘闸声明正确）；revise 回退目标；编排层 outline 失败
retry 与重试提示、generate 落盘闸重试。
"""

from __future__ import annotations

import json
from pathlib import Path

from core.graph_builder import CollaborativeAgentGraph
from core.state import (
    AgentRole,
    WorkflowState,
    WorkflowStatus,
    WorkflowStepStatus,
)
from core.workflows import get_workflow
from core.workflows.ppt_slides import (
    PPT_PAGE_HARD_MAX,
    PPT_PAGE_HARD_MIN,
    parse_deck_outline,
)
from tests.test_graph_builder import ScriptedModel


def _slide(title: str = "页标题", layout: str = "content", **extra: object) -> dict:
    slide: dict = {"layout": layout, "title": title, "points": ["要点一", "要点二"]}
    slide.update(extra)
    return slide


def _outline_json(pages: int, **slide_extra: object) -> str:
    slides = []
    for i in range(1, pages + 1):
        slide = _slide(f"第{i}页")
        slide.update(slide_extra)
        slides.append(slide)
    cover = _slide("封面")
    cover["layout"] = "cover"
    slides[0] = cover
    return json.dumps(
        {"deck_title": "测试课件", "audience": "初一", "slides": slides},
        ensure_ascii=False,
    )


class TestParseDeckOutline:
    def test_parses_and_converges_valid_outline(self) -> None:
        text = "前言 " + _outline_json(12) + " 后记"
        parsed = parse_deck_outline(text)
        assert parsed is not None
        assert parsed["deck_title"] == "测试课件"
        assert parsed["audience"] == "初一"
        assert len(parsed["slides"]) == 12
        assert parsed["slides"][0]["layout"] == "cover"

    def test_hard_fails_on_unparseable_or_too_few_pages(self) -> None:
        assert parse_deck_outline("完全不是 JSON") is None
        assert parse_deck_outline('{"slides": [1, 2]}') is None
        assert parse_deck_outline({"slides": []}) is None  # 非 str → None
        below = parse_deck_outline(_outline_json(PPT_PAGE_HARD_MIN - 1))
        assert below is None

    def test_hard_fails_when_any_title_collapses_to_empty(self) -> None:
        # 空标题硬失败（ppt-template-theme-plan 2.5）：任一页 title 收敛
        # 后为空 → 整体 None，outline 步 retry 内解决，不进暂存
        payload = json.loads(_outline_json(10))
        payload["slides"][4]["title"] = "   "
        assert parse_deck_outline(json.dumps(payload, ensure_ascii=False)) is None
        payload["slides"][4] = {"layout": "content", "points": ["要点一"]}
        assert parse_deck_outline(json.dumps(payload, ensure_ascii=False)) is None

    def test_truncates_pages_over_hard_max(self) -> None:
        parsed = parse_deck_outline(_outline_json(PPT_PAGE_HARD_MAX + 5))
        assert parsed is not None
        assert len(parsed["slides"]) == PPT_PAGE_HARD_MAX

    def test_converges_field_overflows(self) -> None:
        text = _outline_json(
            10,
            title="标" * 60,
            points=[f"要点{i}" * 30 for i in range(9)],
            notes="备" * 300,
            image="imgs/a.png",
        )
        text = text.replace('"layout": "content"', '"layout": "whatever"', 1)
        parsed = parse_deck_outline(text)
        assert parsed is not None
        slide = parsed["slides"][1]
        assert len(slide["title"]) == 40
        assert len(slide["points"]) == 6
        assert all(len(point) <= 60 for point in slide["points"])
        assert len(slide["notes"]) == 150
        # 非法 layout 归一 content；content 页 image 保留
        assert slide["layout"] == "content"
        assert slide["image"] == "imgs/a.png"

    def test_image_dropped_for_non_content_layout(self) -> None:
        text = _outline_json(10, image="imgs/a.png")
        slides = json.loads(text)["slides"]
        text = json.dumps(
            {
                "deck_title": "t",
                "slides": [
                    {**slide, "layout": "cover" if i == 1 else slide["layout"]}
                    for i, slide in enumerate(slides)
                ],
            },
            ensure_ascii=False,
        )
        parsed = parse_deck_outline(text)
        assert parsed is not None
        assert "image" not in parsed["slides"][1]


class TestPptSlidesDefinition:
    def test_registered_with_gates_and_params(self) -> None:
        definition = get_workflow("ppt_slides")
        assert definition is not None
        steps = {step.step_id: step for step in definition.steps}
        assert set(steps) == {"collect", "outline", "generate", "review"}
        assert steps["outline"].output_validator is not None
        assert steps["outline"].on_failure == "retry"
        assert steps["generate"].requires_artifact is True
        assert steps["generate"].artifact_filename_template == "课件-{topic}.pptx"
        assert definition.extra_params == frozenset({"page_count", "style_hint"})
        # 参数管道：page_count 缺省/非法规整为 12
        workflow = definition.build_state({"topic": "光合作用", "grade_hint": ""})
        assert workflow.params["page_count"] == "12"
        workflow = definition.build_state(
            {"topic": "光合作用", "grade_hint": "", "page_count": "99"}
        )
        assert workflow.params["page_count"] == "16"
        # 修订策略：review 判 revise → 回退 outline
        assert definition.revise_policy is not None
        assert (
            definition.revise_policy(3, '{"verdict": "revise"}') == 1
        )
        assert definition.revise_policy(3, '{"verdict": "pass"}') is None


class TestPptOrchestration:
    def _graph(self) -> CollaborativeAgentGraph:
        return CollaborativeAgentGraph(
            model=ScriptedModel([]),
            orchestration_mode="tool",
            enable_workflows=True,
        )

    def test_outline_failure_retries_with_hint(self) -> None:
        graph = self._graph()
        definition = get_workflow("ppt_slides")
        assert definition is not None
        workflow = definition.build_state(
            {"topic": "光合作用", "grade_hint": "", "page_count": "12"}
        )
        workflow = workflow.model_copy(
            update={
                "steps": [
                    workflow.steps[0].model_copy(
                        update={
                            "status": WorkflowStepStatus.COMPLETED,
                            "attempts": 1,
                        }
                    ),
                    workflow.steps[1].model_copy(
                        update={
                            "status": WorkflowStepStatus.FAILED,
                            "attempts": 1,
                            "summary": "步骤失败：agent_output_invalid",
                        }
                    ),
                    *workflow.steps[2:],
                ],
                "current_step_index": 1,
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
        assert updates["next_agent"] == "teaching_assistant"
        assert updates["iteration_budget"] == 6
        message = str(updates["messages"][0].content)
        assert "[系统] 这是重试" in message
        assert "12" in message

    def test_generate_disk_gate_blocks_completion(self, tmp_path: Path) -> None:
        graph = self._graph()
        definition = get_workflow("ppt_slides")
        assert definition is not None
        zone = tmp_path / "zone"
        zone.mkdir()
        workflow = definition.build_state(
            {"topic": "光合作用", "grade_hint": ""},
            artifact_root=str(zone),
        )
        workflow = workflow.model_copy(
            update={
                "steps": [
                    workflow.steps[0].model_copy(
                        update={
                            "status": WorkflowStepStatus.COMPLETED,
                            "attempts": 1,
                        }
                    ),
                    workflow.steps[1].model_copy(
                        update={
                            "status": WorkflowStepStatus.COMPLETED,
                            "attempts": 1,
                            "summary": '{"deck_title": "t", "slides": []}',
                        }
                    ),
                    workflow.steps[2].model_copy(
                        update={
                            "status": WorkflowStepStatus.RUNNING,
                            "attempts": 1,
                        }
                    ),
                    workflow.steps[3],
                ],
                "current_step_index": 2,
                "step_outputs": {"outline": json.dumps({"deck_title": "t"})},
            }
        )
        updates = graph._workflow_worker_updates(
            {"workflow": workflow},
            graph.agents[AgentRole.TEACHING_ASSISTANT],
            __import__("core.nodes.react_agent", fromlist=["ReActResult"]).ReActResult(
                updates={"messages": [], "tool_results": []},
                messages=[
                    __import__("langchain_core.messages", fromlist=["AIMessage"]).AIMessage(
                        content="课件已生成"
                    )
                ],
            ),
            lambda *args, **kwargs: None,
        )
        assert updates is not None
        result: WorkflowState = updates["workflow"]
        # 磁盘上没有 课件-光合作用.pptx → 落盘闸拦下，步骤 FAILED
        assert result.steps[2].status is WorkflowStepStatus.FAILED
        assert result.status is WorkflowStatus.RUNNING
