# PPT 制作工作流设计 — ppt-workflow-design

> 状态：**设计稿 v0.1（待评审）** 2026-08-29
> 前置：`docs/lesson-workflow-design.md`（教案工作流，已实现并冒烟通过）。本稿完全复用其框架基座（声明式定义、注册表触发、步骤状态机、产物区授权、步骤输出暂存、事件族、排队语义），只新增 PPT 特有的机制。
> 需求：智能体在工作流模式下稳定、自动地完成 **不少于 10 页** 的教学课件（.pptx），产物**可生成、可校验、可复核、可恢复**。
> 取证：officecli pptx 命令序列已于 2026-08-29 在本仓库工作区真实冒烟验证（create / add slide / batch / notes / view stats / validate，见 §六「冒烟取证」）。

---

## 一、目标与非目标

**目标**
1. 注册第二个工作流 `ppt_slides`：素材收集 → 分页大纲 → 确定性导出 → 质量校验，全程零审批（产物区自动授权），产出 ≥10 页 .pptx 课件。
2. 针对 PPT 相对教案的四类新增复杂度，各配一个确定性机制（见 §二对照表），**不把复杂度下放给模型自由发挥**。
3. 框架扩展保持通用最小：新增的三个机制（输出结构校验、产物落盘闸、参数声明）对教案工作流零影响。
4. 产物四层校验闭环：导出工具自验（机械）→ 结构门禁（机械）→ review 步骤（模型评审）→ 收口回执（交付）。

**非目标**
- 截图/渲染预览、动画与版式美化、图表生成（officecli 能力存在，但不在固定工作流的确定性范围内）。
- 网络图片抓取（素材图片只认会话工作区内已存在的文件，见 §七）。
- 运行时动态调整页数/结构；模板商店；前端 PPT 专属组件（复用既有工作流进度块）。

---

## 二、复杂度对照：为什么不是「教案工作流换个后缀名」

| # | PPT 新增复杂度 | 教案工作流的对应物 | 本稿的确定性机制 |
|---|---------------|-------------------|-----------------|
| 1 | **分页结构**：≥10 页、每页标题+要点+讲稿，页数/版式必须达标 | 单一连续正文（六段） | outline 步骤输出**严格 JSON Schema**；`parse_deck_outline` 解析+越界收敛；导出工具按页构造命令，自验「写入页数 == 计划页数」 |
| 2 | **长内容稳定性**：大纲+讲稿 2~5 万字符，不能经 CLI 参数搬运 | 教案全文经 `step_outputs` 暂存 + 无参导出工具 | 同一模式：大纲 JSON 暂存 → `export_workflow_pptx` 无正文参数；写入按 **≤8 页/批** 分块（§六-2 有预算推导） |
| 3 | **生成工具可靠性**：pptx 无 `add --type markdown` 直通路径（与 docx 不同），批量写入有 32K/64 项限制，resident 会锁文件 | 单条 `add markdown` 一次写入 | 导出工具五阶段确定性流水线：建文件 → 分页批量（原子回滚）→ 讲稿同批 → 图片独立批（失败跳过不阻断）→ 双重自验（页数 + validate）；重入幂等用「删旧文件 → create」惯用法 |
| 4 | **多轮质量复核**：页数不足、缺封面/小结、内容与素材错位都可能在生成后才暴露 | review 判 revise → 回退 draft 一次 | 同一有界回退机制：review 判 revise → 回退 **outline**（不是 generate）重跑，`max_revise_rounds=1`；另加两个机械门禁防「模型谎报完成」（§五） |

核心立场与教案工作流一致：**步骤顺序、预算、失败策略、写入命令全部代码写死；模型只负责填内容**。

---

## 三、状态机（步骤定义）

| # | step_id | Worker | 职责 | 迭代预算 | 失败策略 | 产物模板 |
|---|---------|--------|------|:---:|---------|---------|
| 1 | `collect` | teaching_assistant | 多路检索知识库（概念/案例/课标）；盘点工作区内可嵌入的图片素材清单 | 8 | abort | — |
| 2 | `outline` | teaching_assistant | 输出严格 JSON 课件大纲（分页结构 + 要点 + 讲稿 + 图片绑定），经 `output_validator` 门禁 | 6 | retry(1) | — |
| 3 | `generate` | teaching_assistant | 只调用一次 `export_workflow_pptx`（无正文参数）；`requires_artifact` 门禁 | 6 | retry(1) | `课件-{topic}.pptx` |
| 4 | `review` | evaluator | officecli_inspect 只读核对 + 对照素材稿评审，输出一行 verdict JSON | 5 | continue | — |
| 5 | （收口） | Supervisor | 整合说明 + 下载回执；不属于工作流步骤（与教案同） | — | — | — |

预算守卫（复用 `_workflow_dispatch` 既有公式 `graph_builder.py:2835`）：
`attempt_budget = 4 步 × 2 × (1 轮 revise + 1) = 16` 次分派，超界熔断 `WORKFLOW_BUDGET_EXCEEDED`。

各步骤指令模板要点（完整文案在实现时按 `lesson_plan.py` 体例写死）：

- **collect**：与教案 collect 同构，追加一条「用文件工具列出工作区内 `.png/.jpg/.jpeg` 文件并给出一句话内容描述；只列真实存在的文件，列不出就写明『无可用图片素材』」。**禁止**建议联网下载图片。
- **outline**：素材稿与素材清单已在对话历史——直接使用，不重新检索。按 §四 Schema 输出**且只输出**一个 JSON 对象；显式声明所有字数/条数上限与页数要求（`{page_count}` 占位符）；明确「封面页与小结页计入总页数」。
- **generate**：与教案 generate 同构——大纲已由系统暂存，**不要复述正文、不要手写 officecli 命令**，只调一次 `export_workflow_pptx`；返回 `ok=false` 时如实报告错误，不声称成功。
- **review**：可用 `officecli_inspect`（`view outline` / `view text` / `view stats` / `validate`，只读）核对实际文件；校验清单见 §八-3；判定口径与教案一致——只对结构性缺失判 revise（页数不足、缺封面/小结、整页无要点、内容与素材明显错位），文字打磨与版式偏好不构成 revise。

---

## 四、大纲 JSON Schema（outline 步骤的唯一输出契约）

```json
{
  "deck_title": "≤40字",
  "audience": "年级/课程提示，≤30字，可为空串",
  "slides": [
    {
      "layout": "cover | section | content | closing",
      "title": "≤40字（必填）",
      "points": ["每条≤60字", "2~6条；cover/section 可为空数组"],
      "notes": "讲稿≤150字，可省略",
      "image": "工作区相对路径，可省略；仅 content 页可用"
    }
  ]
}
```

`parse_deck_outline(text) -> PptDeckOutline | None`（实现于 `core/workflows/ppt_slides.py`，宽容解析策略与 `parse_review_verdict` 同——正则取首个 JSON 对象、`json.loads`、结构校验）：

- 解析失败 / 非对象 / `slides` 非数组 → 返回 `None` → 步骤判失败（与 review 的「按 pass 处理」不同：大纲是导出的前置条件，**硬失败**，靠 retry 重出）。
- 页数越界收敛（**收敛而非拒绝**，避免模型反复卡在 ±1 页）：`len(slides) < 10` → 判失败（触发 retry，指令已声明下限）；`> 16` → 截断到 16 并在 summary 记录。
- 单页越界收敛（确定性截断，永不失败）：`title[:40]`、`points[:6]`、每条 `point[:60]`、`notes[:150]`；`layout` 非法 → 归一为 `content`；`image` 只保留字符串，存在性在导出阶段才校验（§七）。
- 解析成功的规范化结果以 `model_dump_json` 回写暂存（`step_outputs["outline"]`），导出工具读到的永远是收敛后的合法结构。

页数参数：`page_count`（触发参数，默认 12，允许 10~16）只作为 outline 指令里的**目标值**；机械门禁只认硬边界 [10, 16]，±2 页容差不构成失败（防回退环空转）。

---

## 五、框架扩展（三处通用最小增量，对教案零回归）

均落在 `WorkflowStepDefinition` / `_workflow_worker_updates`，教案工作流不声明新字段即行为不变。

### 1. `output_validator`：步骤输出结构门禁

```python
output_validator: Callable[[str], bool] | None = None
```

`_workflow_worker_updates`（`graph_builder.py:2676`）在暂存前调用：声明了校验器且返回 `False` → 该步按 `AGENT_OUTPUT_INVALID` 记 FAILED（不进暂存）。
- `outline` 挂 `lambda s: parse_deck_outline(s) is not None`；
- 教案 draft **不挂**（自由正文无结构契约，保持现状）。
- 必要性：否则模型输出坏 JSON 时步骤照常 COMPLETED，导出工具读到垃圾暂存才失败，且失败无法归因、无法触发本步 retry——这是教案模式里没有的结构性缺口，PPT 必须堵上。

### 2. `requires_artifact`：产物落盘闸（防「谎报完成」）

```python
requires_artifact: bool = False
```

generate 步骤声明为 `True`。`_workflow_worker_updates` 落终态前做**磁盘存在性**判定（不信任模型输出、也不只信任回执登记）：

- 期望文件名 = 该步 `artifact_filename_template` 按 `workflow.params` 格式化 + `sanitize_artifact_filename`；
- `artifact_root / 期望文件名` **存在且非空** → COMPLETED；否则 FAILED（`AGENT_OUTPUT_INVALID`），触发 on_failure retry。
- 为什么必须是磁盘判定而不是回执登记：`_workflow_worker_updates` 的产物登记来自每个 `officecli_edit` 成功结果的 `generated_files`（`graph_builder.py:2747`）——即使导出工具最终自验失败，前面成功的 `create`/`batch` 调用也已登记了半成品文件。只有「磁盘上存在**通过自验后保留**的文件」才是产物事实（导出工具自验失败时会删除自己的文件，见 §六-2 阶段 5）。
- 与既有 `export_workflow_docx` 的「段落数自验」同哲学：不给模型任何谎报空间。

### 3. `start_workflow` 参数泛化

现工具 schema 硬编码 `topic/grade_level`（`graph_builder.py:990`）。新增可选字段：

```python
params: dict[str, str] | None = None   # 值 ≤200 字符
```

校验规则（确定性）：`WorkflowDefinition` 新增 `extra_params: frozenset[str]`（声明允许键）；未声明的键、超长值 → 工具返回结构化错误（与现有「未知工作流 id」同款），不启动。合并进 `WorkflowState.params` 后，指令模板即可用 `{page_count}` / `{style_hint}` 占位符。

- `ppt_slides` 声明 `extra_params = {"page_count", "style_hint"}`；`lesson_plan` 声明空集 → 行为不变。
- `page_count` 在 start_workflow 内确定性规整：非数字或缺省 → "12"；越界截断到 [10, 16]。模型无法注入非法值进模板。
- `style_hint` 仅为 outline 指令的措辞提示（如「简洁商务风」），**不进入导出路径**——版式映射是写死的（§六-3），保证确定性。

### 4. 重试提示注入（顺带小增量）

`_workflow_dispatch` 分派 `attempts > 0` 的重试时，在步骤指令尾部追加一行系统提示：「这是重试：上一次输出未通过结构校验或未产出有效产物，请严格遵循本步格式要求。」
重试分发的是同一模板，模型只能从历史消息自行归因；一行显式提示显著提高重试成功率（教案 draft retry 同样受益）。

---

## 六、确定性导出：`export_workflow_pptx`

与 `export_workflow_docx`（`graph_builder.py:1075`）并列注册为 teaching_assistant 的第二个无正文参数导出工具。执行上下文同为 `approved_office_execution()`（工作流自身确定性写入，非模型写操作）。

### 1. 为什么必须分块批量（预算推导）

后端白名单的硬约束（`office_tools.py`）：
- `--commands` 属 `_BLOB_OPTIONS`：单 token ≤ **32,768** 字符；
- batch 子项 ≤ **64**；子命令动词限 `BATCH_ITEM_VERBS`（`add` 在列）；
- batch 默认**原子**：任一子项失败整体回滚（冒烟取证：`--best-effort` 才是部分生效）。

单页子项体积估算：`{"command":"add","parent":"/","type":"slide","props":{"layout":"Title and Content","title":"≤40字","text":"6×(60字+换行)"}}` ≈ 500~700 字符；含讲稿子项 +200。**8 页/批 ≈ 6~7K 字符**，留 4 倍以上余量；20 页课件 = 3 批，子项 24 个 ≪ 64。取 `_PPT_EXPORT_CHUNK_SIZE = 8`。

### 2. 五阶段流水线

```
阶段 0  读暂存：workflow.step_outputs["outline"] 为空 → ok=false（明确错误文案）
        parse_deck_outline 再解析一次（防御暂存被非规范写入）→ 收敛后的页列表
阶段 1  建文件：目标名 = sanitize("课件-{topic}.pptx")
        若磁盘已存在本运行自己的旧文件 → Python unlink 后 create（幂等重入，
        见下方「冒烟取证 3」：create 拒绝覆盖且拒绝被 resident 持有的文件，
        而「删文件后 create 会自动顶掉 resident 锁」是官方文档给出的可靠惯用法）
阶段 2  分页批量：页列表按 8 页/块切分，每块一条
        `batch <pptx> --json --stop-on-error --commands [...]`
        子项：{"command":"add","parent":"/","type":"slide",
               "props":{"layout":L,"title":T,"text":P}}
        （text = points 以 "\n" 连接——冒烟取证 2：\n 即新段落；
          cover 页的副标题同样走 text）
        讲稿同批内紧随：{"command":"add","parent":"/slide[N]",
               "type":"notes","props":{"text":notes}}
        （N = 批起始序号 + 批内位置，批内顺序执行，索引确定）
        任一批 ok=false → 整体中止：unlink 目标文件 → ok=false 返回
        （阶段 5 的落盘闸语义：自验失败不留半成品，见 §五-2）
阶段 3  图片批（可选，见 §七）：独立一条 batch，失败逐页跳过、不中止
阶段 4  双重自验：
        a) `view stats` → 正则 `Slides:\s*(\d+)` 必须等于计划页数
           （冒烟取证 1；教案同款「段落数>0」的 pptx 强化版：精确相等）
        b) `validate --json` → success=true
        任一不过 → unlink 目标文件 → ok=false
阶段 5  返回 {ok, pptx 路径, slides, deck_title, notes_count, images_embedded}
        文件保留在盘上 → generate 步 officecli_edit 回执登记产物 →
        §五-2 落盘闸磁盘判定通过 → 步骤 COMPLETED
```

### 3. 版式映射（写死，不信任模型）

| outline layout | officecli `layout` prop |
|---|---|
| cover / closing | `Title Slide` |
| section | `Section Header` |
| content | `Title and Content` |

布局名缺失/不匹配时的降级（冒烟已确认默认模板三布局可用，此为实现期兜底）：批失败且错误指向 layout → 该批去掉 `layout` prop 重试一次；`title`/`text` prop 自带占位形状（冒烟取证 4：layout 只是元数据，内容不依赖布局实例化），降级不丢内容。

### 4. 冒烟取证（2026-08-29，本仓库工作区真实命令）

1. `view deck.pptx stats` 输出含 `Slides: 4`、`Slides without title: 0` 等行 —— 自验正则可靠；
2. `text` 属性内 `\n` = 新段落（`help batch` 原文：`\n starts a new paragraph, \v is a line break`）—— 要点逐条成段；
3. `create` 输出 `kept open in background`，且 `help batch` 明确：resident 持有文件时 `create` 报 `file_locked`，**「rm 后 create 自动顶掉 resident」** 是官方可靠惯用法 —— 阶段 1 幂等方案的依据。本次探针收尾时目录被 resident 占用无法删除，实地复现了该锁行为；
4. `--prop layout="Title Slide"` 仅为元数据，占位形状由 `title=`/`text=` prop 实体化（`help pptx slide` 原文）—— §六-3 降级不丢内容的依据；
5. `batch --json` 逐子项返回 `success/output`，缺省原子（`--best-effort` 才部分生效）—— 阶段 2 失败即整体干净的依据；
6. `add /slide[N] --type notes --prop text=...` 与 `validate --json` 均按预期工作。

---

## 七、素材组织（图片素材，有界且失败不阻断）

原则：**图片是增强项，任何图片失败不得阻断课件导出**（与 review「校验是增强项」同一哲学）。

1. **盘点（collect 步）**：文件工具列出工作区内图片（`.png/.jpg/.jpeg`），产出「素材清单：路径 + 一句话描述」；无图则明说。禁止联网下载、禁止引用产物区外不存在的路径。
2. **绑定（outline 步）**：slide 可选 `image` 字段，只允许引用清单中的工作区相对路径；Schema 层只收字符串，不做校验（解析阶段无文件系统语义）。
3. **嵌入（导出阶段 3，独立批）**：
   - 逐条确定性预检：路径经 `WorkspaceFileSystem.resolve_readable_file` 可解析（复用 officecli 工具链同一解析器，天然防逃逸）且文件存在、≤5MB、总数 ≤6 张——超限/不合法条目**静默丢弃**并计入返回值的 `images_skipped`；
   - 通过预检的条目构造子项 `{"command":"add","parent":"/slide[N]","type":"picture","props":{"src":<解析后绝对路径>,"alt":<该页title>}}`（alt 取该页标题，满足 `view stats` 的 `Pictures without alt text` 审计项）；
   - 整批失败或个别子项失败 → 只记日志不中止（文字课件完整即算导出成功）。
4. **审计**：`images_embedded/images_skipped` 进导出工具返回值 → generate 步 summary → 收口轮向用户说明「哪些图没嵌进去、为什么」。

---

## 八、失败策略、有界回退与复核链

### 1. 步骤失败策略总账

| 步骤 | on_failure | 语义 |
|---|---|---|
| collect | abort | 无素材则全流程无意义，直接熔断（与教案一致） |
| outline | retry(1) | 结构校验失败/迭代超限 → 重试一次（带 §五-4 提示），再失败熔断 |
| generate | retry(1) | 导出自验失败/落盘闸未过 → 重跑导出（幂等：删旧文件重建），再失败熔断 |
| review | continue | 评审失败不阻断已生成的产物，落 SKIPPED 照常收口 |

### 2. revise 回退（有界一次）

```python
PPT_SLIDES_REVISE_ROUNDS = 1
# review（index 3）verdict=revise → 回退 outline（index 1）
```

- 回退目标选 **outline 而非 generate**：review 判的结构性问题（缺页/错位/要点缺失）根因在大纲；generate 自身失败由 on_failure retry 覆盖，两条修复路径职责不重叠。
- 复用既有机制：`[1..3]` 重置 PENDING、`workflow.attempts+1`、发 `WORKFLOW_STEP_RETRY`；重跑时 outline 指令模板不变，但 review 的 `revision_points` 已在共享 messages 里，指令模板显式声明「若历史中存在评审修订点，必须逐条落实」。
- 二轮 review 再判 revise 时 `attempts >= max_revise_rounds` → 回退失效，照常收口（带瑕疵交付，收口说明中注明）——与教案同口径，绝不无限循环。

### 3. 复核链（可复核的四层）

| 层 | 执行者 | 判据 | 失败后果 |
|---|---|---|---|
| L1 机械自验 | 导出工具 | 页数精确相等 + `validate` 通过 | 删文件、步骤 FAILED、重试 |
| L2 落盘闸 | 调度簿记 | 期望文件在磁盘存在且非空 | 步骤 FAILED、重试 |
| L3 模型评审 | evaluator（review 步） | ① 页数 ≥10（`view stats`）② 有封面/小结 ③ 每页标题非空、要点不超 6 条（`stats` 的 `Slides without title` / 大纲核对）④ 内容与素材稿一致、引用可追溯 ⑤ 讲稿覆盖主内容页 | 判 revise → 有界回退一次 |
| L4 收口回执 | Supervisor | 产物路径 + 页数 + 结构概览 + 下载入口（`generated_files` 链） | — |

### 4. 恢复语义（可恢复）

- **断点续跑**：每步是 checkpoint 边界，刷新/重启后从 `current_step_index` 恢复（`test_workflow_resume` 既有模式扩一条 ppt 用例）；
- **审批暂停**：PPT 流预期零审批（写操作全在产物区）；模型若偏航写产物区外 → 照旧 `paused_approval` 暂停等人批，批准后断点续跑（机制原样，无需新代码）；
- **导出重入**：阶段 1「删旧文件 → create」+ batch 原子性 → 任何中断点重跑都从空文件重建，不存在半写入状态；
- **排队输入**：复用教案工作流的 `queued_messages` 语义，无新增。

---

## 九、触发与路由

- 意图：沿用 `LESSON_PREP`，不新增枚举（做课件属于备课语义）。
- `prompts.py` Supervisor 卡追加路由约定：请求含「PPT / 课件 / 幻灯片 / slides」→ `start_workflow("ppt_slides", topic, grade_level, params={"page_count": 用户明确页数或省略, "style_hint": 风格词或省略})`；「教案 / 教学设计」→ 仍走 `lesson_plan`；两者都要 → 提示用户分次发起（工作流不支持同轮双启动，`graph_builder.py:1025` 既有防御）。
- `API_WORKFLOW_MODE=auto` 开关对两个工作流统一生效，一键回退能力不变。

---

## 十、事件与契约

- **零新增事件类型**：`WORKFLOW_STARTED / STEP_STARTED / STEP_COMPLETED / STEP_RETRY / COMPLETED / FAILED` 族原样复用，步骤摘要承载进度信息（outline：「12 页大纲」；generate：「课件已生成：12 页 / 图片 2 张」——取自步骤 summary 有界截断，不记正文，脱敏原则不变）。
- 契约不变：`ChatResponse.workflow: WorkflowDto` 对 `workflow_id="ppt_slides"` 天然生效；前端工作流进度块按步骤渲染，无需改造。

---

## 十一、改动点清单（file-level）

| 层 | 文件 | 改动 |
|---|---|---|
| 工作流 | `core/workflows/ppt_slides.py`（新） | 定义、四步指令模板、大纲 Schema 常量、`parse_deck_outline`、revise_policy |
| 工作流 | `core/workflows/definition.py` | `WorkflowStepDefinition.output_validator` / `requires_artifact`；`WorkflowDefinition.extra_params` |
| 工作流 | `core/workflows/__init__.py` | `register_workflow(ppt_slides_workflow())` |
| 编排 | `core/graph_builder.py` | `_workflow_worker_updates` 接结构门禁与落盘闸；`_workflow_dispatch` 重试提示；`start_workflow` 增 `params` 字段并按定义校验；`_create_export_workflow_pptx_tool` 新工具并注册进 teaching_assistant 工具集 |
| 提示词 | `core/nodes/prompts.py` | Supervisor 的 ppt_slides 触发约定（含页数/风格参数抽取规则） |
| 测试 | `backend/tests/` | 见 §十二 |
| 文档 | `DOCS_AUTHORITY` 登记 + README 工作流章节补 ppt_slides | — |

API / 前端 / 契约层：**无改动**（复用教案工作流既有面）。

---

## 十二、测试计划

沿用 `test_workflow_*` 五件套体例，新增/扩展：

1. **定义层**（扩 `test_workflow_state.py`）：ppt_slides 步骤唯一性、预算区间、`extra_params` 声明与 `required_params()` 联动（`{page_count}` 模板键的声明-校验闭环）。
2. **大纲解析**（新 `test_ppt_outline_parse.py`）：合法解析；坏 JSON/非对象/缺 slides → None；`<10` 页 → None（触发失败）；`>16` 页截断；字段越界截断（标题/要点/讲稿）；非法 layout 归一；回写暂存的规范化序列化往返。
3. **编排**（扩 `test_workflow_orchestration.py`）：四步依序分派与角色；outline 结构校验失败 → 重试一次（含重试提示注入断言）→ 再失败熔断；generate 落盘闸未过 → 重试；review revise → 回退 outline（`[1..3]` 重置、attempts 计数、WORKFLOW_STEP_RETRY）；二轮 revise 不再回退；`attempt_budget=16` 边界。
4. **产物区**（扩 `test_workflow_artifact_zone.py`）：pptx 写区内放行；图片预检对逃逸路径/不存在文件/超尺寸判弃；弃图不阻断导出。
5. **导出**（新 `test_workflow_export_pptx.py`）：暂存为空 → ok=false；分块边界（8/9/16 页的批次数断言）；自验页数不符 → 删文件 + ok=false；已存在旧文件 → 先删后建；`--commands` 体积 ≤32768 的组合断言（最坏字段长度组合）。
6. **恢复**（扩 `test_workflow_resume.py`）：generate 步中断后从 `current_step_index` 续跑；导出重入幂等。
7. **端到端冒烟**：真实模型一条——「帮我做一份《光合作用的原理》12 页教学 PPT（初一）」：零审批、≥10 页、`validate` 通过、下载回执可见；既有后端测试零回归。

---

## 十三、风险与缓解

| 风险 | 缓解 |
|---|---|
| batch JSON 超 32K / 子项超 64 | ≤8 页/批 + 字段硬上限（§六-1 推导留 4 倍余量）；导出工具构造前对每条子项 `len` 断言，超限即截断字段而非截断命令 |
| resident 锁导致 create 失败（探针已实地复现） | 「unlink → create」官方惯用法（§六-2 阶段 1）；create 仍报 `file_locked` 时 ok=false 明确归因，走步骤重试 |
| 模型大纲 JSON 格式违约 | output_validator 硬失败 + 有界重试 + 重试提示注入；收敛式截断减少「差一点」失败 |
| 图片幻觉路径/不可用 | 预检（存在/尺寸/数量）+ 静默弃图 + 审计计数，导出永不因图中断 |
| 布局名与部署机模板不符 | 降级批（去 layout 重试一次），内容经 title/text prop 不依赖布局（冒烟取证 4） |
| 单次模型输出装不下全部大纲 | 页数硬上限 16 + 字段上限 ≈ 2 万字符 < 暂存 60K 上限；超限由迭代预算暴露为失败而非静默截断 |
| review 与导出自验重复判页数 | 分层是有意冗余：L1 机械（精确相等）、L3 语义（结构/内容）；两层口径不同不算重复 |

---

## 十四、开放问题（实现前定夺）

1. `page_count` 容差：现案 ±2 页不判失败（防回退空转）；若验收要求「用户说 10 页就必须 ≥10」，下限已是硬约束 [10,16]，只需确认上限侧是否也要求贴近用户值。
2. 图片阈值：单张 ≤5MB、总数 ≤6 —— 是否随部署环境调整，建议做成模块常量不开放为参数。
3. 讲稿（notes）是否强制：现案为可选；若教研要求每页必带讲稿，review 清单加一条即可，不改状态机。
4. 是否在收口轮附大纲结构摘要（页标题列表）：建议附（来自暂存，零成本），提升可复核性。

---

## 十五、里程碑

- **P1** 框架增量：`output_validator` / `requires_artifact` / `extra_params` + 重试提示（教案回归测试全绿为前提）
- **P2** `ppt_slides.py` 定义 + `parse_deck_outline` + 注册（编排测试可跑）
- **P3** `export_workflow_pptx` 五阶段流水线 + 图片批（导出单测全绿）
- **P4** Supervisor 提示词路由 + 真实模型冒烟一条
- **P5** 文档登记 + 既有测试全量回归


---

## 十六、评审补充（2026-08-29，教案工作流实现者交叉评审）

总体结论：**方案成立，可进入 P1**。三处框架扩展（output_validator / requires_artifact / extra_params）设计克制、对教案零回归的论证充分；五阶段流水线的每一步都有冒烟取证支撑；「落盘闸用磁盘存在性而非回执登记」是本稿最有价值的一笔——直接堵死教案侧出现过的「谎报完成」。

两点实现期必须处理的补充：

1. **回执链缺口同样存在于教案侧**：`export_workflow_docx` / `export_workflow_pptx` 内部调用 officecli_edit 产生的 `generated_files` 不会进入模型的 `tool_results`（工具在 core 侧直接 invoke），因此 §八 L4 的「下载回执」不能依赖既有回执链——导出工具必须在成功路径上**显式写 `workflow.artifacts` 登记**（教案 docx 侧当前就有此缺口，记录于项目记忆）。建议 P2 把「导出成功 → artifacts 登记」作为两个导出工具的统一收尾，前端下载入口（M4）以此为数据源。
2. **前端进度块是两工作流共同前置**：本稿 §十称「复用既有工作流进度块」，但该进度块属 M4 待办（queued_messages + 进度块 + 下载入口）尚未实现——`ppt_slides` 的 P4 冒烟可以先行，**用户可见交付**（下载入口 + 步骤进度）应与 M4 合并验收，里程碑里宜显式标注该依赖。

另确认一处框架一致性：`requires_artifact` 的磁盘判定与 `WorkflowState.artifacts` 登记是两条独立事实（前者管步骤成败、后者管交付清单），实现时不要互相替代。

---

## 十七、模板主题化路线（2026-08-30 实施，任务清单 `docs/ppt-template-theme-plan.md`）

**决策记录（为何不用 ppt-master 资产原文）**：hugohe3/ppt-master（MIT）的设计方法论（design spec 先行、色板/字体/页型语言逐版式落地）被采纳为设计参考，但其资产针对 python-pptx 逐页手绘路线，与本项目「确定性导出 + officecli」管线不兼容；且逐页手绘属非目标（§一）。故资产全部自制——构建脚本 `backend/scripts/build_ppt_templates.py` 用 officecli 命令序列把设计烤进 0 页纯母版，幂等重建、自验 fail-fast（README 见 `backend/assets/ppt-templates/`）。

**冒烟取证五条（2026-08-30，真实命令）**：

1. `set /theme --prop accentN/dk*/lt*/headingFont[.ea]/bodyFont[.ea]` 设主题色板与中西文字体，绑定页占位符经 effective.* 继承；
2. `set /slidelayout[N] --prop background=C1-C2-角度`（渐变）与纯色，传播到所有绑定页；
3. `add /slidelayout[N] --type shape` 装饰形状（accent 条/色带/细线）传播到所有绑定页；
4. `add / --type slide --prop layout=...` 新页绑定版式即继承背景与装饰；
5. 默认模板版式清单（错误信息取证）：`Blank / Title Slide / Title and Content / Two Content / Title Only`。

**实施期新增取证（占位符继承链，构建脚本的关键机制）**：页面 `title`/`text` prop 自动实例化的占位符是 `type=title` / `type=body idx=1`，与默认版式的 `ctrTitle` / `subTitle` **不匹配**，文字颜色不随版式继承（直接 `set /slidelayout[N]/shape[1] --prop color` 落在段落 defRPr 上，只作用于版式自身空段落）。构建脚本用 `raw-set` 做占位符手术：`ctrTitle→title`、移除 `subTitle`（同为 idx=1，不移除会遮蔽 body donor 的匹配）、注入带白色 `<a:lstStyle><a:lvl1pPr>`（含 `buClr`，bullet 独立取色链）的 body donor。注意 `lvl1pPr` 在 lstStyle 内只能出现一次（buClr 与 defRPr 必须合并，拆两个违反 Schema，raw-set 自验拦截）。

**降级语义**：`style_hint` 关键词未命中/空值 → 默认主题（edu）；模板资产缺失/复制失败 → 回退空白 `create` 流程，回执 `template: "none(degraded)"`。两条降级都**永不失败**；版式映射在两种路径下共用同一份（资产与默认模板都有同五版式）。回执新增 `template` 字段（审计可见，收口轮可报）。

**视觉验收**：两套主题（edu 教育青 / academic 学术藏蓝）四类页型（封面渐变+accent 条 / 章节深底+侧色带 / 内容白底+顶部细条+页脚线 / 小结同封面）经 officecli 渲染截图核对：渐变方向、中文渲染、文字安全区无遮挡、深浅背景对比度全部达标；构建后自验锁定 0 页 / validate / 五版式名 / 背景与白字手术落点。
