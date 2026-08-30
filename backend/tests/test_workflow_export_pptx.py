"""export_workflow_pptx 测试（ppt-workflow-design P3 / §六）。

假 office 工具捕获命令序列；覆盖：暂存缺失 fail-closed、分块批量
（9 页 → 2 批）、页数自验失配删除产物、图片批 best-effort 与弃图计数；
模板主题化（ppt-template-theme-plan M4.2）：模板复制跳过 create、
layout 取自模板 layout_map、回执标注主题、资产缺失降级。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain_core.tools import tool as langchain_tool
from pydantic import BaseModel, ConfigDict

from core.graph_builder import _ACTIVE_PARENT_STATE, CollaborativeAgentGraph
from core.state import create_initial_state
from core.workflows import get_workflow
from core.workflows.ppt_export import create_export_workflow_pptx_tool
from tests.test_graph_builder import ScriptedModel


def _graph() -> CollaborativeAgentGraph:
    return CollaborativeAgentGraph(
        model=ScriptedModel([]),
        orchestration_mode="tool",
        enable_workflows=True,
    )


def _fake_office(name: str, responses: list[dict[str, Any]]):
    captured: list[dict[str, Any]] = []

    class _Args(BaseModel):
        model_config = ConfigDict(extra="forbid")

        command: list[str]

    @langchain_tool(name, args_schema=_Args)
    def fake(command: list[str]) -> dict[str, Any]:
        """Fake office tool capturing issued commands."""
        captured.append({"command": command})
        index = min(len(captured) - 1, len(responses) - 1)
        return responses[index]

    return fake, captured


def _outline(pages: int, with_notes: bool = True, image: str | None = None) -> str:
    slides: list[dict[str, Any]] = []
    for i in range(1, pages + 1):
        slide: dict[str, Any] = {
            "layout": "cover" if i == 1 else "content",
            "title": f"第{i}页",
            "points": ["要点一", "要点二"],
        }
        if with_notes and i > 1:
            slide["notes"] = f"第{i}页讲稿"
        if image and i == 3:
            slide["image"] = image
        slides.append(slide)
    return json.dumps(
        {"deck_title": "测试课件", "audience": "", "slides": slides},
        ensure_ascii=False,
    )


def _outline_with_all_page_types(pages: int = 10) -> str:
    """四类页型齐备的大纲：cover / section / content… / closing。"""
    slides: list[dict[str, Any]] = []
    for i in range(1, pages + 1):
        layout = "content"
        if i == 1:
            layout = "cover"
        elif i == 2:
            layout = "section"
        elif i == pages:
            layout = "closing"
        slides.append(
            {
                "layout": layout,
                "title": f"第{i}页",
                "points": ["要点一"],
            }
        )
    return json.dumps(
        {"deck_title": "测试课件", "audience": "", "slides": slides},
        ensure_ascii=False,
    )


def _make_asset(assets: Path, filename: str, payload: bytes) -> Path:
    assets.mkdir(parents=True, exist_ok=True)
    asset = assets / filename
    asset.write_bytes(payload)
    return asset


def _run_export(
    tmp_path: Path,
    staged: str | None,
    edit_responses: list[dict[str, Any]],
    inspect_responses: list[dict[str, Any]],
    *,
    image_on_disk: bool = False,
    assets_root: Path | None = None,
    style_hint: str | None = None,
):
    definition = get_workflow("ppt_slides")
    assert definition is not None
    zone = tmp_path / "zone"
    zone.mkdir(parents=True, exist_ok=True)
    params: dict[str, str] = {"topic": "光合作用", "grade_hint": "", "page_count": "12"}
    if style_hint is not None:
        params["style_hint"] = style_hint
    workflow = definition.build_state(params, artifact_root=str(zone))
    outputs = {"outline": staged} if staged is not None else {}
    workflow = workflow.model_copy(update={"step_outputs": outputs})
    if image_on_disk:
        (tmp_path / "imgs").mkdir(exist_ok=True)
        (tmp_path / "imgs" / "diagram.png").write_bytes(b"png")
    parent = create_initial_state(
        session_id="s",
        user_id="u",
        run_id="run-1",
        workspace_root=str(tmp_path),
    )
    parent["workflow"] = workflow
    fake_edit, edit_calls = _fake_office("officecli_edit", edit_responses)
    fake_inspect, inspect_calls = _fake_office(
        "officecli_inspect", inspect_responses
    )
    export_tool = create_export_workflow_pptx_tool(
        fake_edit,
        fake_inspect,
        parent_state=_ACTIVE_PARENT_STATE,
        assets_root=assets_root,
    )
    token = _ACTIVE_PARENT_STATE.set(parent)
    try:
        output = export_tool.invoke({})
    finally:
        _ACTIVE_PARENT_STATE.reset(token)
    return json.loads(output), edit_calls, inspect_calls, zone


class TestExportWorkflowPptx:
    def test_missing_staged_outline_fails_closed(self, tmp_path: Path) -> None:
        receipt, edit_calls, _i, _z = _run_export(
            tmp_path,
            None,
            [],
            [],
            assets_root=tmp_path / "assets-missing",
        )
        assert receipt["ok"] is False
        assert "暂存大纲" in receipt["error"]
        assert edit_calls == []

    def test_happy_path_builds_batched_commands(self, tmp_path: Path) -> None:
        receipt, edit_calls, inspect_calls, zone = _run_export(
            tmp_path,
            _outline(10),
            [
                {"ok": True, "message": "created"},
                {"ok": True},
                {"ok": True},
            ],
            [
                {"ok": True, "stdout": "Slides: 10 | Slides without title: 0"},
                {"ok": True, "stdout": "Validation passed"},
            ],
            assets_root=tmp_path / "assets-missing",
        )
        assert receipt["ok"] is True
        assert receipt["slides"] == 10
        assert receipt["notes_count"] == 9
        assert receipt["images_embedded"] == 0
        # 降级路径（无模板资产）：create + 2 批（10 页 → 8+2）
        assert len(edit_calls) == 3
        assert edit_calls[0]["command"][0] == "create"
        assert receipt["template"] == "none(degraded)"
        batch_one = edit_calls[1]["command"]
        assert batch_one[0] == "batch"
        assert "--stop-on-error" in batch_one
        commands = json.loads(batch_one[batch_one.index("--commands") + 1])
        # 8 页 slide 子项 + 7 条讲稿子项
        assert len(commands) == 15
        assert commands[0]["type"] == "slide"
        # 封面页无讲稿：顺序为 slide1、slide2、notes2、slide3、notes3…
        assert commands[1]["type"] == "slide"
        assert commands[2]["parent"] == "/slide[2]"
        assert commands[2]["type"] == "notes"
        # 自验命令序列：stats → validate
        assert inspect_calls[0]["command"] == [
            "view",
            str(zone / "课件-光合作用.pptx"),
            "stats",
        ]
        assert inspect_calls[1]["command"][0] == "validate"

    def test_slide_count_mismatch_deletes_and_fails(
        self, tmp_path: Path
    ) -> None:
        receipt, _e, _i, zone = _run_export(
            tmp_path,
            _outline(10),
            [
                {"ok": True, "message": "created"},
                {"ok": True},
                {"ok": True},
            ],
            [{"ok": True, "stdout": "Slides: 3"}],
            assets_root=tmp_path / "assets-missing",
        )
        assert receipt["ok"] is False
        assert "实际页数 3" in receipt["error"]
        # 自验失败不留半成品
        assert not (zone / "课件-光合作用.pptx").exists()

    def test_image_batch_best_effort_and_skip_counting(
        self, tmp_path: Path
    ) -> None:
        receipt, edit_calls, _i, _z = _run_export(
            tmp_path,
            _outline(10, image="imgs/missing.png"),
            [
                {"ok": True, "message": "created"},
                {"ok": True},
                {"ok": True},
                {"ok": True},
            ],
            [
                {"ok": True, "stdout": "Slides: 10"},
                {"ok": True, "stdout": "Validation passed"},
            ],
            assets_root=tmp_path / "assets-missing",
        )
        assert receipt["ok"] is True
        assert receipt["images_skipped"] == 1
        assert receipt["images_embedded"] == 0
        # 不存在图片 → 无图片批（3 条 edit 调用：create + 2 文字批）
        assert len(edit_calls) == 3

    def test_image_on_disk_embedded_via_best_effort_batch(
        self, tmp_path: Path
    ) -> None:
        receipt, edit_calls, _i, _z = _run_export(
            tmp_path,
            _outline(10, image="imgs/diagram.png"),
            [
                {"ok": True, "message": "created"},
                {"ok": True},
                {"ok": True},
                {"ok": True},
            ],
            [
                {"ok": True, "stdout": "Slides: 10"},
                {"ok": True, "stdout": "Validation passed"},
            ],
            image_on_disk=True,
            assets_root=tmp_path / "assets-missing",
        )
        assert receipt["ok"] is True
        assert receipt["images_embedded"] == 1
        image_batch = edit_calls[3]["command"]
        assert image_batch[0] == "batch"
        assert "--best-effort" in image_batch
        image_commands = json.loads(
            image_batch[image_batch.index("--commands") + 1]
        )
        assert image_commands[0]["type"] == "picture"
        assert image_commands[0]["parent"] == "/slide[3]"


class TestTemplateThemedExport:
    """模板主题化（ppt-template-theme-plan M4.2）：假资产注入 assets_root。"""

    def test_template_copy_skips_create_and_stamps_receipt(
        self, tmp_path: Path
    ) -> None:
        assets = tmp_path / "assets"
        _make_asset(assets, "edu-theme.pptx", b"EDU-FAKE-ASSET")
        receipt, edit_calls, _i, zone = _run_export(
            tmp_path,
            _outline_with_all_page_types(10),
            [{"ok": True}, {"ok": True}],
            [
                {"ok": True, "stdout": "Slides: 10"},
                {"ok": True, "stdout": "Validation passed"},
            ],
            assets_root=assets,
        )
        assert receipt["ok"] is True
        assert receipt["template"] == "edu"
        # 目标文件即模板资产字节（复制而非 create）
        assert (zone / "课件-光合作用.pptx").read_bytes() == b"EDU-FAKE-ASSET"
        # 无 create 调用：edit 序列就是 2 个文字批
        assert len(edit_calls) == 2
        assert edit_calls[0]["command"][0] == "batch"
        batch_one = json.loads(
            edit_calls[0]["command"][edit_calls[0]["command"].index("--commands") + 1]
        )
        # layout 取自模板 layout_map：cover → Title Slide，section → Title Only
        assert batch_one[0]["props"]["layout"] == "Title Slide"
        assert batch_one[1]["props"]["layout"] == "Title Only"
        assert batch_one[2]["props"]["layout"] == "Title and Content"

    def test_style_hint_selects_academic_asset(self, tmp_path: Path) -> None:
        assets = tmp_path / "assets"
        _make_asset(assets, "academic-theme.pptx", b"ACADEMIC-FAKE-ASSET")
        receipt, _e, _i, zone = _run_export(
            tmp_path,
            _outline(10),
            [{"ok": True}, {"ok": True}],
            [
                {"ok": True, "stdout": "Slides: 10"},
                {"ok": True, "stdout": "Validation passed"},
            ],
            assets_root=assets,
            style_hint="学术风，要严谨",
        )
        assert receipt["ok"] is True
        assert receipt["template"] == "academic"
        assert (zone / "课件-光合作用.pptx").read_bytes() == b"ACADEMIC-FAKE-ASSET"

    def test_missing_asset_degrades_to_create(self, tmp_path: Path) -> None:
        receipt, edit_calls, _i, _z = _run_export(
            tmp_path,
            _outline(10),
            [
                {"ok": True, "message": "created"},
                {"ok": True},
                {"ok": True},
            ],
            [
                {"ok": True, "stdout": "Slides: 10"},
                {"ok": True, "stdout": "Validation passed"},
            ],
            assets_root=tmp_path / "assets-missing",
        )
        assert receipt["ok"] is True
        assert receipt["template"] == "none(degraded)"
        # 降级路径：第一个 edit 调用是 create
        assert edit_calls[0]["command"][0] == "create"

    def test_default_assets_root_uses_bundled_template(
        self, tmp_path: Path
    ) -> None:
        # 不注入 assets_root（生产路径）：默认根解析到入库资产 → 无 create
        receipt, edit_calls, _i, _z = _run_export(
            tmp_path,
            _outline(10),
            [{"ok": True}, {"ok": True}],
            [
                {"ok": True, "stdout": "Slides: 10"},
                {"ok": True, "stdout": "Validation passed"},
            ],
        )
        assert receipt["ok"] is True
        assert receipt["template"] == "edu"
        assert all(call["command"][0] == "batch" for call in edit_calls)
