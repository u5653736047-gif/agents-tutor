"""构建 ppt_slides 工作流的模板主题资产（ppt-template-theme-plan M1.2）。

用 officecli 命令序列把设计（色板/字体/版式背景/装饰/深色页白字）烤进
**0 页**的纯母版 .pptx 资产：导出管线复制模板后按版式名加页即继承全部
视觉，页数自验 `Slides == 计划页数` 不受影响。

幂等：先删旧文件再 create（resident 锁官方惯用法）。构建后自验
fail-fast：0 页断言、validate、五个版式名齐全（注册表 layout_map 的
依赖）、深色版式背景与白字占位符手术落点。任一失败非零退出。

用法：
    cd backend && .venv/Scripts/python.exe scripts/build_ppt_templates.py
    # 指定输出目录（自检/测试用）：
    python scripts/build_ppt_templates.py --output-dir <dir> [--only edu]

取证（2026-08-30 真实冒烟，.tplprobe3 已清理）：
- `set /theme --prop accent1/headingFont.ea` 设主题色板与中西文字体；
- `set /slidelayout[N] --prop background=C1-C2-角度` 渐变传播到绑定页；
- `add /slidelayout[N] --type shape` 装饰形状传播到绑定页；
- 版式占位符与页面自动实例化的占位符**按 type+idx 匹配**：默认
  Title Slide 版式是 ctrTitle+subTitle，页面 title prop 发出的是
  type=title、text prop 发出的是 type=body idx=1——直接 set 版式占位符
  颜色不继承，需 raw-set 手术：ctrTitle→title、移除 subTitle、注入
  带 `<a:lstStyle><a:lvl1pPr>` 白字的 body donor（subTitle 不移除会
  按 idx 遮蔽 body donor，实地取证）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

# 画布尺寸（officecli 默认 16:9）：装饰坐标统一按此计算，避开标题区
# （上 1/4）与正文区（中部），装饰贴边。
CANVAS_W_CM = 25.4
CANVAS_H_CM = 14.29

# officecli 默认模板版式顺序（2026-08-30 取证，构建后 query 断言锁定）：
# [1] Blank [2] Title Slide [3] Title and Content [4] Two Content [5] Title Only
LAYOUT_INDEX = {"blank": 1, "cover": 2, "content": 3, "two_content": 4, "section": 5}
LAYOUT_NAMES = ("Blank", "Title Slide", "Title and Content", "Two Content", "Title Only")

# 子进程环境：禁自动更新/自动 resident/自动安装（与 office_tools 白名单
# 同款），构建脚本自己起进程、不经后端工具链。
_OFFICECLI_ENV = {
    "OFFICECLI_SKIP_UPDATE": "1",
    "OFFICECLI_NO_AUTO_RESIDENT": "1",
    "OFFICECLI_NO_AUTO_INSTALL": "1",
}

# ── 白字占位符手术的 XML 片段（OOXML 标准机制：版式占位符的
# lstStyle/lvl1pPr 是 PowerPoint 自身给占位符定文字样式的位置） ──
_WHITE_LST_STYLE = (
    '<a:lstStyle><a:lvl1pPr><a:defRPr><a:solidFill>'
    '<a:srgbClr val="FFFFFF"/></a:solidFill></a:defRPr></a:lvl1pPr></a:lstStyle>'
)
# body donor：给 text prop 自动实例化的 body idx=1 占位符提供白色继承源；
# buClr 同步白化项目符号（渲染器对 bullet 用独立取色链）。buClr 与
# defRPr 必须合并在同一个 lvl1pPr 内（CT_TextParagraphProperties 序列：
# buClr 在 defRPr 之前），拆成两个 lvl1pPr 会违反 Schema（raw-set 自验拦截）。
_BODY_DONOR_XML = (
    '<p:sp><p:nvSpPr><p:cNvPr id="90" name="Body Style Donor"/>'
    '<p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr>'
    '<p:nvPr><p:ph type="body" idx="1"/></p:nvPr></p:nvSpPr>'
    '<p:spPr><a:xfrm><a:off x="1447800" y="2057400"/>'
    '<a:ext cx="6858000" cy="2895600"/></a:xfrm></p:spPr>'
    "<p:txBody><a:bodyPr/><a:lstStyle><a:lvl1pPr>"
    '<a:buClr><a:srgbClr val="FFFFFF"/></a:buClr>'
    "<a:defRPr>"
    '<a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill></a:defRPr></a:lvl1pPr>'
    '</a:lstStyle><a:p><a:endParaRPr lang="en-US"/></a:p></p:txBody></p:sp>'
)


def _white_title_surgery() -> list[dict[str, str]]:
    """Title Slide 版式的占位符手术（页 title/text prop 发出的占位符类型是 title/body）。"""
    return [
        {
            "xpath": "//p:nvPr/p:ph[@type='ctrTitle']",
            "action": "replace",
            "xml": '<p:ph type="title" />',
        },
        {
            "xpath": "//p:sp[p:nvSpPr/p:nvPr/p:ph[@type='title']]/p:txBody/a:lstStyle",
            "action": "replace",
            "xml": _WHITE_LST_STYLE,
        },
        {
            # subTitle 同为 idx=1，不移除会遮蔽 body donor 的继承匹配
            "xpath": "//p:sp[p:nvSpPr/p:nvPr/p:ph[@type='subTitle']]",
            "action": "remove",
            "xml": "",
        },
        {
            "xpath": "//p:cSld/p:spTree/p:sp[last()]",
            "action": "insertafter",
            "xml": _BODY_DONOR_XML,
        },
    ]


def _white_title_only_surgery() -> list[dict[str, str]]:
    """Title Only 版式：title 类型本就匹配，只需白字 + body donor。"""
    return [
        {
            "xpath": "//p:sp[p:nvSpPr/p:nvPr/p:ph[@type='title']]/p:txBody/a:lstStyle",
            "action": "replace",
            "xml": _WHITE_LST_STYLE,
        },
        {
            "xpath": "//p:cSld/p:spTree/p:sp[last()]",
            "action": "insertafter",
            "xml": _BODY_DONOR_XML,
        },
    ]


def _bar(x: float, y: float, w: float, h: float, color: str, name: str) -> dict[str, Any]:
    return {
        "props": {
            "x": f"{x}cm",
            "y": f"{y}cm",
            "w": f"{w}cm",
            "h": f"{h}cm",
            "fill": color,
            "line": "none",
            "name": name,
        }
    }


# ── 主题设计规范（ppt-template-theme-plan 1.1） ──────────────────
# 色板四级：主 accent1 / 渐变双色 cover_bg / 深色 dk2 / 中性 dk1-lt2。
THEMES: tuple[dict[str, Any], ...] = (
    {
        "template_id": "edu",
        "theme_name": "Edu Teal",
        "palette": {
            "dk1": "233230",
            "dk2": "114B42",
            "lt1": "FFFFFF",
            "lt2": "E9F4F1",
            "accent1": "1F7A6D",
            "accent2": "2FA39A",
            "accent3": "F2B134",
            "accent4": "E8674F",
            "accent5": "3F8FD2",
            "accent6": "6FAF46",
            "hyperlink": "1F7A6D",
        },
        "fonts": {
            "headingFont": "Segoe UI",
            "headingFont.ea": "微软雅黑",
            "bodyFont": "Calibri",
            "bodyFont.ea": "等线",
        },
        "cover_bg": "114B42-2FA39A-115",
        "section_bg": "114B42",
        "cover_bar": "F2B134",
        "section_band": "F2B134",
        "content_top_bar": "1F7A6D",
        "content_footer_line": "CBE2DC",
        "dark_layouts": (2, 5),
    },
    {
        "template_id": "academic",
        "theme_name": "Academic Navy",
        "palette": {
            "dk1": "212529",
            "dk2": "16294A",
            "lt1": "FFFFFF",
            "lt2": "F2F4F7",
            "accent1": "1F3864",
            "accent2": "2E5AAC",
            "accent3": "8A97A8",
            "accent4": "B08D2E",
            "accent5": "5B84B1",
            "accent6": "4E6E58",
            "hyperlink": "1F3864",
        },
        "fonts": {
            "headingFont": "Georgia",
            "headingFont.ea": "微软雅黑",
            "bodyFont": "Georgia",
            "bodyFont.ea": "宋体",
        },
        "cover_bg": "1F3864-2E5AAC-115",
        "section_bg": "1F3864",
        "cover_bar": "B08D2E",
        "section_band": "B08D2E",
        "content_top_bar": "1F3864",
        "content_footer_line": "C9D2DE",
        "dark_layouts": (2, 5),
    },
)


def resolve_binary() -> str:
    """API_OFFICECLI_BINARY 优先，否则 PATH 查找；找不到 fail-fast。"""
    configured = os.getenv("API_OFFICECLI_BINARY", "officecli").strip() or "officecli"
    if Path(configured).exists():
        return configured
    found = shutil.which(configured)
    if found is None:
        raise SystemExit(
            f"officecli 二进制不存在：{configured}。请安装 officecli 或设置 "
            "API_OFFICECLI_BINARY。"
        )
    return found


class Builder:
    def __init__(self, binary: str) -> None:
        self.binary = binary

    def run(self, argv: list[str], *, json_output: bool = True) -> dict[str, Any]:
        command = [self.binary, *argv]
        if json_output:
            command.append("--json")
        env = {**os.environ, **_OFFICECLI_ENV}
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=120,
            check=False,
        )
        payload: dict[str, Any] = {}
        try:
            payload = json.loads(result.stdout or "{}")
        except ValueError:
            payload = {}
        if result.returncode != 0 or (json_output and not payload.get("success")):
            raise SystemExit(
                f"officecli 命令失败：{' '.join(argv)}\n"
                f"stdout: {result.stdout[:500]}\nstderr: {result.stderr[:500]}"
            )
        if not json_output:
            return {"stdout": result.stdout, "stderr": result.stderr}
        return payload

    # ── 声明式构建步骤 ────────────────────────────────────────
    def set_theme(self, target: Path, spec: dict[str, Any]) -> None:
        props: dict[str, str] = {"name": spec["theme_name"], **spec["palette"], **spec["fonts"]}
        argv = ["set", str(target), "/theme"]
        for key, value in props.items():
            argv += ["--prop", f"{key}={value}"]
        self.run(argv)

    def set_background(self, target: Path, layout_index: int, background: str) -> None:
        self.run(
            [
                "set",
                str(target),
                f"/slidelayout[{layout_index}]",
                "--prop",
                f"background={background}",
            ]
        )

    def add_shape(self, target: Path, layout_index: int, shape: dict[str, Any]) -> None:
        argv = ["add", str(target), f"/slidelayout[{layout_index}]", "--type", "shape"]
        for key, value in shape["props"].items():
            argv += ["--prop", f"{key}={value}"]
        self.run(argv)

    def raw_set(self, target: Path, layout_index: int, step: dict[str, str]) -> None:
        argv = [
            "raw-set",
            str(target),
            f"/slideLayout[{layout_index}]",
            "--xpath",
            step["xpath"],
            "--action",
            step["action"],
        ]
        if step["xml"]:
            argv += ["--xml", step["xml"]]
        self.run(argv, json_output=False)

    def decorate_cover(self, target: Path, spec: dict[str, Any]) -> None:
        idx = LAYOUT_INDEX["cover"]
        self.set_background(target, idx, spec["cover_bg"])
        # 底部 accent 全幅色条：贴下边缘，避开标题（上 1/4）与正文（中部）
        self.add_shape(target, idx, _bar(0, 13.55, CANVAS_W_CM, 0.74, spec["cover_bar"], "CoverAccentBar"))
        for step in _white_title_surgery():
            self.raw_set(target, idx, step)

    def decorate_section(self, target: Path, spec: dict[str, Any]) -> None:
        idx = LAYOUT_INDEX["section"]
        self.set_background(target, idx, spec["section_bg"])
        # 左侧 accent 竖带：贴左边缘
        self.add_shape(target, idx, _bar(0, 0, 0.5, CANVAS_H_CM, spec["section_band"], "SectionAccentBand"))
        for step in _white_title_only_surgery():
            self.raw_set(target, idx, step)

    def decorate_content(self, target: Path, spec: dict[str, Any]) -> None:
        idx = LAYOUT_INDEX["content"]
        # 浅色内容页：顶部 accent 细条 + 页脚细线，正文区零遮挡
        self.add_shape(target, idx, _bar(0, 0, CANVAS_W_CM, 0.18, spec["content_top_bar"], "ContentTopBar"))
        self.add_shape(
            target, idx, _bar(0, 14.06, CANVAS_W_CM, 0.05, spec["content_footer_line"], "ContentFooterLine")
        )

    # ── 构建后自验（fail-fast） ───────────────────────────────
    def verify(self, target: Path, spec: dict[str, Any]) -> None:
        def _fail(message: str) -> None:
            raise SystemExit(f"模板自验失败（{spec['template_id']}）：{message}")

        stats = self.run(["view", str(target), "stats"], json_output=False)["stdout"]
        match = re.search(r"Slides:\s*(\d+)", stats)
        if match is None or int(match.group(1)) != 0:
            _fail(f"必须为 0 页纯母版，实际 stats={stats[:200]}")
        validated = self.run(["validate", str(target)])
        if validated.get("data", {}).get("count") != 0:
            _fail(f"validate 未通过：{str(validated)[:300]}")
        layouts = self.run(["query", str(target), "slidelayout"])
        names = [r["format"].get("name") for r in layouts.get("data", {}).get("results", [])]
        if sorted(filter(None, names)) != sorted(LAYOUT_NAMES):
            _fail(f"版式名与注册表 layout_map 依赖不符：{names}")
        for index in spec["dark_layouts"]:
            # background 字段只在文本输出回读（--json 的 format 不含它）
            got = self.run(
                ["get", str(target), f"/slidelayout[{index}]", "--depth", "1"],
                json_output=False,
            )["stdout"]
            match = re.search(r"background=(\S+)", got)
            background = match.group(1) if match else ""
            if not background or background in {"none", "transparent"}:
                _fail(f"版式 {index} 背景未生效：{background!r}")
        for index in spec["dark_layouts"]:
            raw = self.run(
                ["raw", str(target), f"/slideLayout[{index}]"], json_output=False
            )["stdout"]
            if "lvl1pPr" not in raw or 'val="FFFFFF"' not in raw:
                _fail(f"版式 {index} 白字占位符手术未生效")
        theme = self.run(["get", str(target), "/theme"], json_output=False)["stdout"]
        match = re.search(r"accent1=(\S+)", theme)
        expected = f"#{spec['palette']['accent1'].upper()}"
        if match is None or match.group(1) != expected:
            _fail(f"主题色板未生效：accent1={match.group(1) if match else None!r} 期望 {expected}")


def build_theme(binary: str, spec: dict[str, Any], target: Path) -> None:
    builder = Builder(binary)
    # 幂等：先删旧文件再 create（resident 持有时 create 报 file_locked，
    # 「unlink → create 自动顶掉锁」为官方惯用法）
    target.unlink(missing_ok=True)
    builder.run(["create", str(target)])
    builder.set_theme(target, spec)
    builder.decorate_cover(target, spec)
    builder.decorate_section(target, spec)
    builder.decorate_content(target, spec)
    builder.verify(target, spec)


def main() -> None:
    parser = argparse.ArgumentParser(description="构建 ppt_slides 模板主题资产")
    parser.add_argument(
        "--output-dir",
        default=str(Path(__file__).resolve().parents[1] / "assets" / "ppt-templates"),
        help="资产输出目录（默认 backend/assets/ppt-templates）",
    )
    parser.add_argument("--only", choices=["edu", "academic"], help="只构建指定主题")
    args = parser.parse_args()

    binary = resolve_binary()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    for spec in THEMES:
        if args.only and spec["template_id"] != args.only:
            continue
        target = output_dir / f"{spec['template_id']}-theme.pptx"
        build_theme(binary, spec, target)
        print(f"built: {target} ({target.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
