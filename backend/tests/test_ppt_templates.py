"""ppt_templates 模板注册表测试（ppt-template-theme-plan M4.1）。

覆盖：关键词命中/未命中回退默认/空值默认；资产缺失 → resolve None
（fail-closed）；layout_map 覆盖四类页型；默认资产根可解析到已入库
资产（构建产物随仓库提交）。
"""

from __future__ import annotations

from pathlib import Path

from core.workflows.ppt_templates import (
    DEFAULT_LAYOUT_MAP,
    TEMPLATES,
    assets_root_default,
    resolve_template_path,
    select_template,
)

_EDU = TEMPLATES[0]
_ACADEMIC = TEMPLATES[1]


class TestSelectTemplate:
    def test_hits_keywords(self) -> None:
        assert select_template("学术风").template_id == "academic"
        assert select_template("论文答辩，要严谨").template_id == "academic"
        assert select_template("教育风").template_id == "edu"
        assert select_template("课堂教学用").template_id == "edu"

    def test_miss_falls_back_to_default(self) -> None:
        # 未命中 → 首个（edu 默认主题），启发式永不失败
        assert select_template("简洁商务风").template_id == "edu"
        assert select_template("完全不相干的描述").template_id == "edu"

    def test_empty_or_blank_falls_back_to_default(self) -> None:
        assert select_template("").template_id == "edu"
        assert select_template(None).template_id == "edu"
        assert select_template("   ").template_id == "edu"

    def test_multiple_hits_take_registry_order(self) -> None:
        # 同段文字命中多个 → 注册序首个（edu 置首）
        hint = "既要学术严谨又要教育活泼"
        assert select_template(hint).template_id == "edu"


class TestResolveTemplatePath:
    def test_missing_asset_returns_none(self, tmp_path: Path) -> None:
        assert resolve_template_path(_EDU, assets_root=tmp_path) is None

    def test_injected_root_resolves_asset(self, tmp_path: Path) -> None:
        asset = tmp_path / _EDU.asset_filename
        asset.write_bytes(b"fake")
        assert resolve_template_path(_EDU, assets_root=tmp_path) == asset

    def test_default_root_finds_bundled_assets(self) -> None:
        # 资产随仓库入库（构建脚本产物），默认根必须能解析到
        root = assets_root_default()
        assert root.name == "ppt-templates"
        for template in TEMPLATES:
            path = resolve_template_path(template)
            assert path is not None, template.template_id
            assert path.is_file() and path.stat().st_size > 0


class TestLayoutMapContract:
    def test_covers_all_page_types(self) -> None:
        for template in TEMPLATES:
            assert set(template.layout_map) == {"cover", "closing", "section", "content"}
            # 页型 → 版式名与共享默认映射一致（构建脚本断言五个版式名）
            assert template.layout_map == DEFAULT_LAYOUT_MAP

    def test_layout_names_within_bundled_assertion(self) -> None:
        # 版式名必须落在构建脚本 query slidelayout 断言的五版式清单内
        # （conftest 已把 backend/scripts/ 注入 sys.path）
        import build_ppt_templates

        assert set(DEFAULT_LAYOUT_MAP.values()) <= set(build_ppt_templates.LAYOUT_NAMES)
