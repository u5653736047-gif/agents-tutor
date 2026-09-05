# PPT 视觉升级调研：接入 open-kimi-ppt 替代 officecli 导出 — 调研报告

> 状态：调研完成（2026-08-30），**未实施**。起因：officecli 生成的 PPT 观感朴素（无版式设计、无配色装饰），期望引入 open-kimi-ppt（Kimi Slides 逆向实现，HTML 页面 + 30 套设计系统主题）获得接近成品的效果。
> 调研方式：源码阅读 + 本机实测（依赖清单、示例项目真实导出、故障分层隔离、绕行方案验证）。

---

## 一、结论摘要

1. **可行性：成立，但有一个必须解决的驱动层障碍**。open-kimi-ppt 的导出 CLI（`export_pptx.py`）接口干净、确定性调用；其依赖的浏览器自动化组件 **agent-browser 在本机（服务进程上下文）浏览器启动挂起**——已用 Playwright 直连 Chromium 验证浏览器本身完全可用，因此绕行方案（薄驱动替代 agent-browser）可行。
2. **推荐方案：C「双引擎」**——保留 officecli 引擎（快、零网络、现有测试），新增 kimi 引擎作为 `style_hint` 驱动的精修路径；两者共用既有四步状态机与产物区授权。不建议直接替换。
3. **工作量估计**：约 2~3 个工作日（P1 驱动层 0.5~1 天、P2 内容生成契约与主题注入 1 天、P3 冒烟调优 0.5 天），另有模型产出质量的不确定性需要冒烟迭代。

## 二、open-kimi-ppt 链路拆解（实测）

### 2.1 交付物与生成模式

- **PPTD 项目** = `.pptd`（YAML DSL 清单，抽象 OOXML）+ `pages/`（每页自包含 HTML）+ `media/`；**PPTX 成品** = 浏览器端 writer 导出（嵌字体、淡入淡出翻页切换）。
- 与 officecli 的本质区别：officecli 是「结构化元素 API」（模型拼命令）；kimi 是「设计系统 + HTML 页面」（~30 套官方同款主题在 `reference/design_system/`，模型按主题规范写页面）——观感差距的来源即在此。
- 质量保障机制：导出前**视觉质检环**（`export_images.py` 导出整份页面图片 → 多模态模型逐项核查变形/遮挡/出界/对比度 → 修复复检）——我们的 API_VISION 视觉链路可以直接复用这一环。

### 2.2 确定性导出 CLI（可直接接工作流）

```bash
python scripts/export_pptx.py <PPTD目录或.pptd> -o <输出.pptx> \
    [--transition fade|none] [--no-embed-fonts] [--force]
```
- 输入/输出均为显式路径，stdout 输出 JSON summary，失败 exit 1——完全符合工作流「确定性导出工具」的形态；
- 内部链路：起临时 localhost SDK host（`export_host.html`）→ **agent-browser** 打开页面 → 浏览器端 writer 生成 → 下载到 `--download-path` → 回移到输出路径。

### 2.3 本机依赖实测（2026-08-30）

| 依赖 | 要求 | 实测 |
|---|---|---|
| Node.js | ≥18 | ✓ v24.12.0 |
| Python + PyYAML | 3.x | ✓ 3.11.9 + PyYAML 已装 |
| Chrome/Chromium/Edge | agent-browser 需要 | ⚠ **见 §三** |
| agent-browser npm | ≥0.33.2 | ✓ 0.33.2（恰为下限） |
| kimi.com / statics.moonshot.cn | 导出时在线取前端资源 | ✓ kimi.com 200（0.5s）；CDN 根 403 属正常（拒绝列目录） |
| 示例项目导出 | 18 页真实导出 | ✗ **失败**（agent-browser 层，见 §三） |

## 三、故障分层隔离（导出探针失败的分析）

现象：示例项目导出在 `agent-browser open http://127.0.0.1:57906/export_host.html` 处 90 秒超时。逐层隔离结果：

1. **网络层 ✓**：kimi.com 可达（国内服务）；
2. **浏览器可执行层 ✗→✓**：agent-browser 报 "Chrome not found"——它只认系统 Chrome / Puppeteer 缓存 / Playwright 缓存（**不认 Edge**，本机仅有 Edge）。`npx playwright install chromium` 安装后其缓存探测仍不识别（版本目录差异），但 `--executable-path` 直接指向 `ms-playwright/chromium-1234/chrome-win64/chrome.exe` 是它显式支持的入口；
3. **启动握手层 ✗（当前阻塞点）**：即使显式 executable-path，`agent-browser open` 在本环境无限挂起（守护进程握手，90 秒无 chrome 进程产生）——**问题定位在 agent-browser 组件本身**；
4. **绕行验证 ✓（决定性）**：Playwright（node 1.62.1，npx 可用；浏览器已装进 ms-playwright 缓存）直接 `chromium.launch()` + `goto` 完全正常——浏览器与渲染链路无问题，仅 agent-browser 这一层不可用。

**绕行方案**：不修 agent-browser，写 **~60 行 Playwright 薄驱动**替代其在导出中的角色（起 host → 打开 export_host.html → 等待下载事件 → 回移文件）。可选：给上游提补丁（`--executable-path` + 超时控制），但自研驱动不阻塞在任何外部组件上。

## 四、接入方案对比

| | A：维持 officecli | B：全面替换为 kimi | **C：双引擎（推荐）** |
|---|---|---|---|
| 观感 | 朴素（现状） | 设计系统级 | 按 `style_hint` 选择 |
| 确定性/稳定性 | 高（已验证） | 中（HTML 生成质量依赖模型 + 导出需联网取 CDN 资源） | 高（默认引擎不变，kimi 为增强路径） |
| 工作量 | 0 | 大（替换 + 回归全部重做） | 中（§五） |
| 评委环境风险 | 无 | **断网即失败**（export 需 kimi CDN） | 默认引擎兜底，可降级 |
| 模型职责 | 拼 JSON 大纲 | 写 PPTD YAML + 逐页 HTML（重创意） | 同 B，但失败可降级 officecli |

推荐 **C** 的理由：竞赛演示要「效果上限」，交付保障要「稳定性下限」——双引擎让两者不冲突。`style_hint` 含「精美/设计感」等关键词或显式选择时走 kimi 引擎；默认 officecli（或后续把默认切到 kimi，视冒烟质量）。

## 五、推荐方案的落地要点（C 路线）

**步骤状态机调整**（ppt_slides 四步 → 五步）：
```
collect → outline（JSON 大纲，复用）→ compose（新增：按选中 theme 生成
PPTD YAML + 逐页 HTML，写产物区 kimi 项目目录，暂存清单）→
export（确定性：薄驱动调 export_pptx.py + 自验）→ review → 收口
```

**关键实现点**：
1. **薄驱动**：`kimi_export_driver.py`——subprocess 起 `export_pptx.py` 前置不可行（agent-browser 在其内部），改为：复用其 host 准备逻辑（或直接调其内部函数）+ Playwright 启动 chromium 打开 host 页 + 监听下载。建议给上游 export_pptx.py 提取一个 `--driver playwright` 分支的最小补丁（本地维护），比完全重写稳；
2. **主题注入**：`theme.md` 30 套主题 → 精选 2~3 套教学适用（如学术简洁风）作为内置默认；compose 指令注入所选主题的 design_system 规范（按需读 `reference/design_system/<theme>/`，避免 90KB pptd.md 全文进上下文——写成 reference 文件供 Worker 按需 Read）；
3. **requires_artifact 复用**：generate 步的落盘闸直接适用（pptx 不在盘上即失败）；
4. **视觉质检（增强项，二期）**：`export_images.py` 出页面图 → API_VISION 链路核查 → 不合格回退 compose；首期先做人查；
5. **网络风险缓解**：`statics.moonshot.cn` 资源可预下载到本地 host 目录实现离线导出（二期调研项）；演示环境确认在线即可用。

**工作量**：P1 薄驱动 + 导出封装（0.5~1 天）；P2 compose 步骤（主题机制 + HTML 契约 + 指令）（1 天）；P3 冒烟与质量迭代（0.5 天起）。另有两个前置项：M4 前端进度块（两工作流共同）、教案 docx 产物登记补全（已记忆）。

## 六、风险清单

| 风险 | 等级 | 缓解 |
|---|---|---|
| agent-browser 本机不可用 | 已发生（已定位） | Playwright 薄驱动替代（已验证 Playwright 可用） |
| kimi CDN 断网 | 中（评委环境） | 演示环境在线；二期资源本地化 |
| HTML 页面生成质量不稳定 | 中（模型创意工作） | 主题规范注入 + review 步 + 视觉质检环（二期） |
| agent-browser 0.33.2 = 版本下限 | 低 | 升级 npx 包即可 |
| 双引擎维护成本 | 低 | officecli 引擎冻结为兜底，不再演进 |
