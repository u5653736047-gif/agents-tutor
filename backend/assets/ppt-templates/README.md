# PPT 模板主题资产

`ppt_slides` 工作流导出管线（`export_workflow_pptx`）的预置母版资产。
两套均为 **0 页纯母版**（只含主题色板、字体、版式背景与装饰形状），
由 `backend/scripts/build_ppt_templates.py` 用 officecli 命令序列构建，
可随时幂等重建；**不要手工编辑**（手工改动会被下次构建覆盖，且可能
破坏「Slides: 0」「版式名清单」等导出管线依赖的断言）。

| 资产 | template_id | 设计语言 | 关键词（style_hint 命中） |
|---|---|---|---|
| `edu-theme.pptx` | `edu` | 教育青：深青渐变封面 + 暖黄 accent 条，微软雅黑/等线 | 教育/教学/课堂/默认 |
| `academic-theme.pptx` | `academic` | 学术藏蓝：藏蓝渐变封面 + 哑金 accent 条，Georgia/宋体 | 学术/论文/严谨 |

## 来源声明

- 资产为**本项目自制**（构建脚本生成，非复制第三方文件）。
- 设计方法论（design spec：先定色板/字体/页型语言，再逐版式落地）
  参考 MIT 许可的开源项目 [hugohe3/ppt-master](https://github.com/hugohe3/ppt-master)
  的设计思路，未使用其任何资产原文。归属与许可归 upstream 项目。

## 约束（改动前必读）

1. **0 页**：页数自验 `Slides == 计划页数` 依赖模板不含预置页（构建脚本断言）。
2. **五个版式名**：`Blank / Title Slide / Title and Content / Two Content /
   Title Only` —— 注册表 `layout_map` 按名定位（构建脚本 `query slidelayout` 断言）。
3. **深色版式（Title Slide / Title Only）的白色标题/正文**依赖构建脚本
   注入的占位符手术（`ctrTitle→title`、移除 `subTitle`、注入带白色
   `lstStyle` 的 body donor）——手工删改这些隐藏占位符会让深色页文字
   退回黑色、不可读。
4. 重建命令：`cd backend && .venv/Scripts/python.exe scripts/build_ppt_templates.py`
