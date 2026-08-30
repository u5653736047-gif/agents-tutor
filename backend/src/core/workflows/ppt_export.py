"""`export_workflow_pptx` 确定性导出工厂（ppt-workflow-design §六）。

五阶段流水线：读暂存（parse_deck_outline 再解析防御）→ 删旧建新
（resident 锁官方惯用法，幂等重入；有模板资产时改为**复制模板**跳过
create，缺失/损坏降级回 create）→ 分页批量（≤8 页/批，batch 原子，
讲稿紧随本页同批；版式取自所选模板的 layout_map）→ 图片批
（best-effort，失败不阻断）→ 双重自验（页数精确相等 + validate）。
任一自验不过：删除产物、返回 ok=false——`requires_artifact` 落盘闸
据此判定 generate 步骤成败，模型零正文参数、零命令构造。

本模块只依赖 tools.office_tools 与 workflows.definition / workflows
.ppt_templates；graph_builder 通过 `create_export_workflow_pptx_tool(
parent_state=...)` 注入父状态 ContextVar（避免 workflows →
graph_builder 反向依赖）。
"""

from __future__ import annotations

import json
import re
import shutil
from contextvars import ContextVar
from pathlib import Path
from typing import Any, cast

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, ConfigDict, ValidationError

from core.state import AgentState, WorkflowState

from .definition import sanitize_artifact_filename
from .ppt_slides import parse_deck_outline
from .ppt_templates import (
    DEFAULT_LAYOUT_MAP,
    resolve_template_path,
    select_template,
)

# batch 硬约束推导见 ppt-workflow-design §六-1：单 token ≤32768 字符、
# 子项 ≤64；单页子项 ≈ 500~700 字符，8 页/批留 4 倍以上余量。
_PPT_EXPORT_CHUNK_SIZE = 8
# 图片增强项阈值：任何图片失败不得阻断课件导出（超限静默弃图并计数）。
_PPT_IMAGE_MAX_BYTES = 5 * 1024 * 1024
_PPT_IMAGE_MAX_COUNT = 6
# 版式映射写死（ppt-workflow-design §六-3）：不信任模型选择。现在由
# ppt_templates.DEFAULT_LAYOUT_MAP 承载（模板资产与默认空白模板都有
# 这五个版式，降级路径下同一份映射依然成立）；保留模块级别名作为
# 默认值来源（ppt-template-theme-plan 2.3）。
_PPT_LAYOUT_MAP = DEFAULT_LAYOUT_MAP
# 降级（模板缺失/复制失败）时回执里的 template 标注。
_DEGRADED_TEMPLATE_LABEL = "none(degraded)"


def create_export_workflow_pptx_tool(
    office_edit: BaseTool,
    office_inspect: BaseTool,
    *,
    parent_state: ContextVar[AgentState | None],
    assets_root: Path | None = None,
) -> BaseTool:
    """构造 export_workflow_pptx 工具（注册为 Worker 可用，无正文参数）。

    assets_root 供测试注入假资产目录；生产默认按模块位置解析
    backend/assets/ppt-templates/。
    """

    class _ExportPptxInput(BaseModel):
        model_config = ConfigDict(extra="forbid")

    def export_workflow_pptx() -> str:
        from ..tools.office_tools import approved_office_execution

        parent = parent_state.get()
        raw_workflow = None if parent is None else parent.get("workflow")
        workflow: WorkflowState | None = None
        if raw_workflow is not None:
            try:
                workflow = WorkflowState.model_validate(raw_workflow)
            except ValidationError:
                workflow = None
        staged = (
            None if workflow is None else workflow.step_outputs.get("outline")
        )
        outline = parse_deck_outline(staged)
        if workflow is None or not workflow.artifact_root or outline is None:
            return json.dumps(
                {"ok": False, "error": "暂存大纲缺失或解析失败，无法导出课件"},
                ensure_ascii=False,
            )
        root = Path(workflow.artifact_root)
        topic = workflow.params.get("topic", "课件")
        target = root / sanitize_artifact_filename(f"课件-{topic}.pptx")

        def _invoke(tool_instance: BaseTool, command: list[str]) -> dict[str, Any]:
            raw = tool_instance.invoke({"command": command})
            parsed: Any = raw
            if isinstance(raw, str):
                try:
                    parsed = json.loads(raw)
                except ValueError:
                    parsed = {"ok": False, "message": raw[:300]}
            return cast(dict[str, Any], parsed)

        def _fail(message: str) -> str:
            target.unlink(missing_ok=True)
            return json.dumps(
                {"ok": False, "error": message}, ensure_ascii=False
            )

        # 模板选择（ppt-template-theme-plan 2.2）：关键词命中选主题，
        # 未命中走默认主题；资产缺失 → 降级空白 create，永不失败。
        template = select_template(workflow.params.get("style_hint", ""))
        template_path = resolve_template_path(template, assets_root)
        template_label = template.template_id
        if template_path is None:
            template_label = _DEGRADED_TEMPLATE_LABEL

        with approved_office_execution():
            # 阶段 1：删旧建新（resident 持有时 create 报 file_locked，
            # 「unlink → create 自动顶掉锁」为官方惯用法；幂等重入）。
            # 有模板资产时跳过 create，直接复制 0 页纯母版（纯 Python
            # 文件操作，不经 officecli、不触碰授权白名单）；复制失败
            # 降级回 create。
            target.unlink(missing_ok=True)
            if template_path is not None:
                try:
                    shutil.copyfile(template_path, target)
                except OSError:
                    target.unlink(missing_ok=True)
                    template_path = None
                    template_label = _DEGRADED_TEMPLATE_LABEL
            if template_path is None:
                created = _invoke(office_edit, ["create", str(target)])
                if not created.get("ok"):
                    return _fail(f"创建演示文稿失败：{str(created)[:300]}")
            # 阶段 2：分页批量（≤8 页/批，原子；讲稿紧随本页同批，
            # 页号 = 批起始序号 + 批内位置，批内顺序执行索引确定）
            slides = outline["slides"]
            for chunk_start in range(
                0, len(slides), _PPT_EXPORT_CHUNK_SIZE
            ):
                chunk = slides[chunk_start : chunk_start + _PPT_EXPORT_CHUNK_SIZE]
                items: list[dict[str, Any]] = []
                for offset, slide in enumerate(chunk):
                    page_number = chunk_start + offset + 1
                    props: dict[str, Any] = {
                        # 版式取自所选模板的 layout_map（ppt-template-
                        # theme-plan 2.3）；降级时同一份映射依然成立。
                        "layout": template.layout_map.get(
                            str(slide.get("layout")),
                            template.layout_map["content"],
                        ),
                        "title": slide.get("title", ""),
                    }
                    text = "\n".join(slide.get("points", []))
                    if text:
                        props["text"] = text
                    items.append(
                        {
                            "command": "add",
                            "parent": "/",
                            "type": "slide",
                            "props": props,
                        }
                    )
                    notes = slide.get("notes")
                    if notes:
                        items.append(
                            {
                                "command": "add",
                                "parent": f"/slide[{page_number}]",
                                "type": "notes",
                                "props": {"text": notes},
                            }
                        )
                batch_payload = json.dumps(items, ensure_ascii=False)
                batch_result = _invoke(
                    office_edit,
                    [
                        "batch",
                        str(target),
                        "--json",
                        "--stop-on-error",
                        "--commands",
                        batch_payload,
                    ],
                )
                if not batch_result.get("ok") and "layout" in str(
                    batch_result
                ).lower():
                    # 部署机模板缺版式的降级（ppt-workflow-design §六-3）：
                    # 去 layout prop 重试一次——title/text prop 自带占位
                    # 形状，内容不依赖布局实例化（冒烟取证 4）。
                    stripped = [
                        {**item, "props": {k: v for k, v in item.get("props", {}).items() if k != "layout"}}
                        for item in items
                    ]
                    batch_result = _invoke(
                        office_edit,
                        [
                            "batch",
                            str(target),
                            "--json",
                            "--stop-on-error",
                            "--commands",
                            json.dumps(stripped, ensure_ascii=False),
                        ],
                    )
                if not batch_result.get("ok"):
                    return _fail(
                        f"写入课件页失败（第 {chunk_start + 1} 页起）："
                        f"{str(batch_result)[:300]}"
                    )
            # 阶段 3：图片批（best-effort；预检失败/超限静默弃图并计数）
            images_embedded = 0
            images_skipped = 0
            image_items: list[dict[str, Any]] = []
            workspace_root = (
                None if parent is None else parent.get("workspace_root")
            )
            for offset, slide in enumerate(slides):
                image = slide.get("image")
                if not image or not isinstance(workspace_root, str):
                    continue
                candidate = (Path(workspace_root) / image).resolve()
                inside = candidate.is_relative_to(
                    Path(workspace_root).resolve()
                )
                if (
                    not inside
                    or not candidate.is_file()
                    or candidate.stat().st_size > _PPT_IMAGE_MAX_BYTES
                    or images_embedded >= _PPT_IMAGE_MAX_COUNT
                ):
                    images_skipped += 1
                    continue
                image_items.append(
                    {
                        "command": "add",
                        "parent": f"/slide[{offset + 1}]",
                        "type": "picture",
                        "props": {
                            "src": str(candidate),
                            "alt": slide.get("title", "配图"),
                        },
                    }
                )
                images_embedded += 1
            if image_items:
                _invoke(
                    office_edit,
                    [
                        "batch",
                        str(target),
                        "--json",
                        "--best-effort",
                        "--commands",
                        json.dumps(image_items, ensure_ascii=False),
                    ],
                )
            # 阶段 4：双重自验（页数精确相等 + validate）
            stats = _invoke(office_inspect, ["view", str(target), "stats"])
            slide_match = re.search(
                r"Slides:\s*(\d+)", str(stats.get("stdout", ""))
            )
            actual_slides = int(slide_match.group(1)) if slide_match else -1
            if actual_slides != len(slides):
                return _fail(
                    f"写入自验失败：实际页数 {actual_slides} != 计划页数 "
                    f"{len(slides)}；请勿声称导出成功"
                )
            validated = _invoke(office_inspect, ["validate", str(target)])
            if not validated.get("ok"):
                return _fail(
                    f"写入自验失败（validate）：{str(validated)[:300]}"
                )
        # 阶段 5：回执（产物登记由 _workflow_worker_updates 解析回执完成）
        return json.dumps(
            {
                "ok": True,
                "pptx": str(target),
                "slides": len(slides),
                "deck_title": outline.get("deck_title", ""),
                "template": template_label,
                "notes_count": sum(
                    1 for slide in slides if slide.get("notes")
                ),
                "images_embedded": images_embedded,
                "images_skipped": images_skipped,
            },
            ensure_ascii=False,
        )

    return tool(
        "export_workflow_pptx",
        args_schema=_ExportPptxInput,
        description=(
            "把暂存的课件大纲（outline 步骤产出）确定性导出为产物目录内"
            "的 pptx 文件并自动验证页数。无需提供内容参数；调用成功后"
            "报告返回值中的文件路径与页数即可。"
        ),
    )(export_workflow_pptx)


__all__ = ["create_export_workflow_pptx_tool"]
