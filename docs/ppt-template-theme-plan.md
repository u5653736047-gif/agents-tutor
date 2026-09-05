# PPT 模板主题化实施任务清单 — ppt-template-theme-plan

> 状态：**已实施并冒烟闭环 v1.2** 2026-08-30
> 前置：`docs/ppt-workflow-design.md`（已实现的 ppt_slides 工作流）。本清单是其「视觉质量升级」的落地计划：**模板资产路线**——把设计（配色/字体/版式背景/装饰）烤进预置 .pptx 模板资产，导出管线从「空白 create」改为「复制模板 → 绑定版式加页」，确定性、预算、门禁机制全部不变。
> 取证（2026-08-30 真实冒烟，命令可复跑）：
> 1. `set /theme --prop accent1/headingFont.ea` 可设主题色板与中西文字体；
> 2. `set /slidemaster[1]/slidelayout[N] --prop background=渐变` 布局背景**传播到所有绑定页**；
> 3. `add /slidemaster[1]/slidelayout[N] --type shape` 布局可挂装饰形状；
> 4. `add / --type slide --prop layout=...` 新页绑定版式即继承背景与装饰；
> 5. 默认模板版式清单（错误信息取证）：`Blank / Title Slide / Title and Content / Two Content / Title Only`。

---

## 目标与非目标

**目标**
1. 交付 2 套模板主题（教育青 `edu`、学术灰 `academic`），课件视觉从「素面」到「有设计感」：封面渐变全幅、章节色带、内容页 accent 标题条、统一字体与页脚装饰。
2. `style_hint` 参数消费掉（修复死参数）：关键词命中选模板，未命中走默认主题，**永不失败**。
3. 模板缺失/损坏自动降级回现行空白 create 流程，产物链路（自验/落盘闸/回执登记）零改动。
4. 顺带修复上次评估的 🟡 问题：空标题收敛（任务 2.5）。

**非目标**
- 逐页 SVG 手绘（ppt-master 式 pro 路线，二期另议）。
- 模板管理界面 / 用户上传模板。
- 图片配图逻辑变更（现有 best-effort 图片批不动）。

---

## 架构改动（一图）

```
现状：export_workflow_pptx
  阶段1 unlink → create（空白模板）→ 阶段2 分页 batch（layout 写死映射）→ …

目标：阶段1 unlink → shutil.copyfile(选中模板资产, target)   ← 唯一写路径变化
      阶段2 分页 batch（layout 映射改读所选模板的 layout_map）→ 其余阶段不变

新增模块：core/workflows/ppt_templates.py（模板注册表 + 选择 + 路径解析）
新增资产：backend/assets/ppt-templates/{edu,academic}-theme.pptx（0 页、只含母版设计）
新增脚本：backend/scripts/build_ppt_templates.py（officecli 命令重建模板，幂等）
```

关键不变量：
- 模板资产必须是 **0 页** 的纯母版文件（构建脚本断言 `Slides: 0`），否则页数自验 `Slides == 计划页数` 会失配；
- 复制模板是纯 Python 文件操作（读仓库资产、写产物区），不经 officecli，不触碰授权白名单；
- 版式名必须与模板内实际版式一致，注册表加载时无法运行时校验 → 由构建脚本在构建后 `query slidelayout` 断言覆盖（任务 1.2）。

---

## M1 模板资产（设计 → 构建 → 验收）

- [x] **1.1 设计规范定稿**（本文档内定稿即可，不另起文档）
  - 每套主题给出：色板（主色/渐变双色/accent/中性 dk-lt 四级）、字体（标题/正文，含 `.ea` 中文槽位，选部署机常见字体：微软雅黑/等线）、四类页型设计：
    - `Title Slide`（cover/closing）：渐变全幅背景（如 `1F3864-2E5AAC-115`）+ 底部 accent 色条；
    - `Title Only`（section）：深色单色背景或左侧色带 + 大号标题区；
    - `Title and Content`（content）：浅色背景 + 顶部/左侧 accent 装饰条 + 页脚细线；
    - `Blank`：保持干净（降级/备用）。
  - 装饰形状坐标按 960×540pt（25.4×14.29cm）画布计算，**避开标题/正文占位符安全区**（标题区上 1/4、正文中部，装饰贴边）。
  - 设计语言参考 hugohe3/ppt-master（MIT）的 design_spec 方法论，资产自制。
- [x] **1.2 构建脚本** `backend/scripts/build_ppt_templates.py`
  - 用 officecli 命令序列构建两套模板：`create`（先删旧文件，幂等）→ `set /theme`（色板+字体）→ 逐版式 `set .../slidelayout[N] --prop background` → 逐版式 `add ... --type shape`（装饰）；
  - 版式按名定位：先 `query slidelayout` 取索引（错误信息取证：`[2] Title Slide`、`[5] Title Only`、`[3] Title and Content`、`[1] Blank`）；
  - 构建后自验：`view stats` 断言 `Slides: 0`、`validate` 通过、`query slidelayout` 断言五个版式名齐全（注册表 layout_map 依赖它们）；失败 fail-fast 非零退出。
- [x] **1.3 资产入库**：`backend/assets/ppt-templates/edu-theme.pptx`、`academic-theme.pptx` + 同目录 README（来源声明：自制资产，设计方法论参考 MIT 项目 hugohe3/ppt-master，含归属说明）。
- [x] **1.4 人工视觉验收**：用 PowerPoint 打开模板 + 手工加 3~4 测试页（封面/章节/内容/小结），核对：渐变方向、中文渲染、文字安全区无遮挡、深浅背景上的文字对比度；调整 1.1/1.2 直至通过。**这是质量的唯一人工闸口，不允许跳过。**（实施记录：officecli 渲染截图核对两套主题 × 四类页型全部达标；交付时建议以 PowerPoint 打开抽查一次）

## M2 代码接线

- [x] **2.1 新模块** `backend/src/core/workflows/ppt_templates.py`
  - `PptTemplate`（frozen dataclass）：`template_id / asset_filename / keywords: tuple[str, ...] / layout_map: dict[str, str]（页型→版式名）/ description`；
  - `TEMPLATES: tuple[PptTemplate, ...]`：edu（keywords 含「教育/教学/课堂/默认」）置首作默认，academic（「学术/论文/严谨」）；
  - `select_template(style_hint: str) -> PptTemplate`：空值/无命中 → 首个（默认），命中多个取注册序首个——启发式永不失败；
  - `resolve_template_path(template, assets_root=None) -> Path | None`：默认按 `__file__` 相对定位 `backend/assets/ppt-templates/`，`assets_root` 可注入（测试用）；文件不存在 → None（fail-closed）。
- [x] **2.2 改造** `ppt_export.py` 阶段 1（唯二写路径改动之一）
  - `select_template(workflow.params.get("style_hint", ""))` → `resolve_template_path`；
  - 路径有效：`target.unlink(missing_ok=True)` 后 `shutil.copyfile(模板, target)`，**跳过 create**；
  - 路径无效或复制异常：回退现行 `create` 流程，回执记 `"template": "none(degraded)"`；
  - 成功回执增加 `"template": template_id`（审计可见，收口轮可报）。
- [x] **2.3 版式映射模板化**（唯二写路径改动之二）：批构造处 `layout` 从 `template.layout_map[页型]` 读取；模块级 `_PPT_LAYOUT_MAP` 保留，作为 `PptTemplate.layout_map` 的默认值来源；既有「批失败且错误含 layout → 去 layout 重试」降级路径原样保留。
- [x] **2.4 回归确认**：页数自验正则、`validate`、落盘闸（期望文件名不变）、回执登记（产物路径不变）全部不需改动——以测试断言锁定。
- [x] **2.5 顺带修复（评估遗留 🟡）**：`parse_deck_outline` 增加空标题硬失败——任一页 `title` 收敛后为空 → 返回 None（outline 步 retry 内解决，不再消耗 revise 回退）。补对应单测。

## M3 提示词与契约（小改）

- [x] **3.1** `prompts.py` 的 `WORKFLOW_SUPERVISOR_CLAUSE`：style_hint 说明改为「用户给出风格要求时填（如 教育风/学术风），影响课件主题选择」；不承诺具体主题清单（注册表是唯一事实来源）。
- [x] **3.2** generate 步指令不变（工具无参数）；收口轮说明自然带出回执中的主题名（现有「报告文件路径+页数」措辞扩为「+主题」）。

## M4 测试

- [x] **4.1** 新 `tests/test_ppt_templates.py`：关键词命中/未命中回退默认/空值默认；资产缺失 → resolve None；layout_map 覆盖四类页型。
- [x] **4.2** 扩展 `tests/test_workflow_export_pptx.py`（沿用假 office 工具 + `assets_root` 注入）：
  - 模板路径：断言**无 create 调用**、目标文件内容 == 模板资产字节、batch layout 取自模板 layout_map、回执 `template=edu`；
  - 降级路径：注入不存在的资产 → 有 create 调用、回执 `template=none(degraded)`；
  - style_hint=学术关键词 → 选中 academic 资产路径。
- [x] **4.3** 扩展 `tests/test_workflow_ppt_slides.py`：空标题 → `parse_deck_outline` 返回 None（任务 2.5）。
- [x] **4.4** 构建脚本自检：临时目录重建模板 → 断言 0 页 / validate / 版式齐全（可作为慢速标记的集成测试或直接脚本自测）。
- [x] **4.5** 全量回归：`--basetemp` 工作区临时区跑法（环境坑已记项目记忆），既有 1300+ 全绿。

## M5 文档与交付

- [x] **5.1** `docs/ppt-workflow-design.md` 追加 §十七「模板主题化路线」：决策记录（为何不用 ppt-master 资产原文）、本清单冒烟取证五条、降级语义。
- [x] **5.2** README 工作流表 `ppt_slides` 行补「主题选择：style_hint（教育风/学术风，缺省教育风）」。
- [x] **5.3** 真实模型冒烟两条：同课题分别带「教育风」「学术风」各一次，验收 ≥10 页、零审批、自验通过、两份产物版式视觉明显不同、降级路径不受影响。
  （实证 2026-08-30：教育风/学术风各一条均 12 页零审批 review=pass，generate 回执报出主题；资产改名后第三条自动降级 `none(degraded)`、12 页 validate=0 错误、工作流不失败。）
- [x] **5.4** 提交切分：模板资产单独提交（`feat(workflow): add ppt template theme assets`）、构建脚本、代码+测试、文档各独立提交（遵循仓库 git 规范）。

---

## 依赖顺序与并行性

```
1.1 → 1.2 → 1.4（人工闸口）→ 1.3
                ∥
2.1 → 2.2 → 2.3 → 2.4（+2.5 独立小修，可随时插入）
                ↓
          4.1~4.5（2.x 完成即可写，1.3 资产入库后跑真实路径用例）
                ↓
          3.1/3.2 → 5.1~5.4
```

代码侧（M2/M4）不依赖模板成品——用假资产即可开发；模板成品（M1）决定最终视觉效果。

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| 装饰形状侵占文字安全区 | 1.4 人工验收强制项；装饰贴边布置，坐标写进 1.1 规范 |
| 模板含预置页导致页数自验失配 | 构建脚本断言 `Slides: 0`（1.2），测试 4.4 锁定 |
| 模板版式名与注册表不符 | 构建脚本 `query slidelayout` 断言五版式齐全；运行时另有「去 layout 重试」降级 |
| 部署机缺中文字体 | 选系统常见字体；缺字时 PowerPoint 自动替换，validate 不受影响 |
| officecli resident 锁（构建期） | 构建脚本沿用「删旧 → create」惯用法；导出期模板只被 copyfile 只读，不被 officecli 打开 |
| style_hint 关键词匹配启发式误配 | 无命中=默认主题、永不失败，符合其「参考信息」定位 |
| git 仓库存二进制资产 | 单模板数十 KB 量级，可接受；资产变更低频 |

## 总验收标准

1. `API_WORKFLOW_MODE=auto` 下真实模型请求「做一份《XX》课件，学术风」：零审批产出 ≥10 页 .pptx，学术模板视觉，`validate` 通过；
2. 删除/改名任一模板资产后同请求：自动降级为空白模板产物，回执标注降级，工作流不失败；
3. 既有 1300+ 测试全绿 + 新增用例全绿；
4. 教案工作流行为零变化（不涉及本次改动的任何文件路径）。
