"""ppt_slides 模板主题注册表（ppt-template-theme-plan M2.1）。

模板资产路线的唯一事实来源：把设计（色板/字体/版式背景/装饰）烤进
预置 0 页纯母版 .pptx（`backend/assets/ppt-templates/`，构建脚本
`backend/scripts/build_ppt_templates.py` 幂等重建），导出管线从
「空白 create」改为「复制模板 → 绑定版式加页」。

选择语义（style_hint 消费，**永不失败**）：
- 空值 / 无关键词命中 → 返回首个（edu，默认主题）；
- 命中多个 → 取注册序首个（edu 置首）；
- 资产缺失 → `resolve_template_path` 返回 None（fail-closed），导出
  管线据此降级回空白 create 流程，工作流不失败。

版式映射按**版式显示名**定位（不按索引）：名称清单由构建脚本
`query slidelayout` 断言锁定，与资产内实际版式一致；运行时另有
「批失败且错误含 layout → 去 layout 重试」降级兜底。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# 页型 → 版式显示名。默认模板（officecli create）与两套主题资产都有
# 这五个版式（名称清单由构建脚本断言），因此降级路径（无模板资产、
# 空白 create）下同一份映射依然成立。
DEFAULT_LAYOUT_MAP: dict[str, str] = {
    "cover": "Title Slide",
    "closing": "Title Slide",
    "section": "Title Only",
    "content": "Title and Content",
}

# 资产目录：backend/assets/ppt-templates/（本模块位于
# backend/src/core/workflows/，parents[3] = backend）。
_ASSETS_SUBDIR = Path("assets") / "ppt-templates"


@dataclass(frozen=True, slots=True)
class PptTemplate:
    """一套模板主题的静态描述。

    layout_map：页型 → 版式显示名（ppt_export 分页批按页型读取）；
    keywords：style_hint 关键词命中集（小写包含匹配）。
    """

    template_id: str
    asset_filename: str
    keywords: tuple[str, ...]
    layout_map: dict[str, str]
    description: str


TEMPLATES: tuple[PptTemplate, ...] = (
    PptTemplate(
        template_id="edu",
        asset_filename="edu-theme.pptx",
        keywords=("教育", "教学", "课堂", "活泼", "清新"),
        layout_map=DEFAULT_LAYOUT_MAP,
        description="教育青：深青渐变封面 + 暖黄 accent，适合课堂教学课件（默认）",
    ),
    PptTemplate(
        template_id="academic",
        asset_filename="academic-theme.pptx",
        keywords=("学术", "论文", "严谨", "答辩"),
        layout_map=DEFAULT_LAYOUT_MAP,
        description="学术藏蓝：藏蓝渐变封面 + 哑金 accent，适合讲座与论文答辩",
    ),
)


def select_template(style_hint: str | None) -> PptTemplate:
    """按 style_hint 关键词选模板；空值/未命中 → 首个（默认主题）。

    启发式永不失败：style_hint 是「参考信息」，选错主题的代价远小于
    导出失败。
    """
    hint = (style_hint or "").strip().lower()
    if hint:
        for template in TEMPLATES:
            if any(keyword in hint for keyword in template.keywords):
                return template
    return TEMPLATES[0]


def assets_root_default() -> Path:
    """默认资产根（按模块位置解析，随仓库走，不依赖 cwd）。"""
    return Path(__file__).resolve().parents[3] / _ASSETS_SUBDIR


def resolve_template_path(
    template: PptTemplate,
    assets_root: Path | None = None,
) -> Path | None:
    """解析模板资产绝对路径；不存在 → None（fail-closed，交降级路径）。"""
    root = assets_root if assets_root is not None else assets_root_default()
    candidate = root / template.asset_filename
    if candidate.is_file():
        return candidate
    return None


__all__ = [
    "DEFAULT_LAYOUT_MAP",
    "TEMPLATES",
    "PptTemplate",
    "assets_root_default",
    "resolve_template_path",
    "select_template",
]
