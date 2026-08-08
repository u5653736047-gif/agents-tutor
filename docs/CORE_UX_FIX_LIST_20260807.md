# 多智能体协作系统——核心 UX 问题修复清单

> **日期**：2026-08-07
> **范围**：从两次深度分析（①全维度 UX 审计 69 条发现 + 对抗核查；②视觉设计深度诊断，实机截图 + 像素级量化 + 20 条落地性核查方案）中，精选 **5 个最核心问题**。
> **用途**：交付给实现同事逐项落地。每项含现象、根因、拟修改方案、影响面与注意事项、验收口径。
> **证据截图**：见 `docs/ui-review-shots/`（空态 / 对话态 / 协作面板 / 移动端空态）。

---

## 0. 优先级总览

| # | 问题 | 级别 | 用户影响 | 涉及面 | 工作量 | 依赖 |
|---|------|------|---------|--------|--------|------|
| 1 | 流式发送用户消息不乐观回显 | 高 | 每次发送都中招：看不到自己说了什么 | `chat-store.ts` | 小 | 无 |
| 2 | 切换会话历史加载无反馈 | 高 | 点会话必现：消息区空白无骨架 | `chat-store.ts` + `conversation-panel.tsx` | 小 | 无 |
| 3 | Markdown 回答正文视觉扁平 | 高 | 每次读回答受影响：无法扫读 | `assistant-markdown.tsx` + `globals.css` | 中 | #4 的字号档 |
| 4 | 全站亮色白海无层次 + 色彩单调 | 高 | 观感平淡、无品牌感 | `globals.css` + 若干组件 | 中 | 无 |
| 5 | 会话列表仅展示不透明 session_id | 高 | 多会话后无法定位会话 | 后端 + 前端契约 + 侧栏 | 大 | 后端配合 |

**建议实施顺序**：#1、#2 是纯前端小改动，可立即落地；#3、#4 是观感提升主战场；#5 需前后端排期。

> 通用注意：改 `globals.css` token 必须先改文件、再同步 `frontend/DESIGN_SYSTEM.md`（维护约定①）；改类名/文案/默认态可能破坏 3 个测试文件，见文末「实施注意事项」。

---

## 问题 1：流式发送用户消息不乐观回显

**级别**：高（每次发送都中招）

### 现象

用户输入问题、点发送后，输入框立即清空，但消息流里**不出现用户自己的消息**。多 Agent 一轮回答可能耗时数十秒到数分钟（thinking → tool_call → handoff 审批 → agent_switch → 回答），期间消息列表只有流式气泡/骨架，用户看不到自己刚才说了什么，会困惑「我发的内容去哪了」。

对比：同步通道（`sendMessage`）是乐观回显的，两条通道体验不一致。

**证据截图**：`docs/ui-review-shots/10-chat-light.png`（对话态，消息流只有回答，无用户消息回显痕迹）。

### 根因

`frontend/stores/chat-store.ts`：

- `streamSendMessage`（约 :525-536）进入时 `set()` 只置 `isStreaming / streamingMessage / events / runError / lastSentMessage`，**从不把用户消息追加进 `messages`**。
- `dispatch`（约 :539-587）只处理服务端事件（thinking / tool_call / tool_result / agent_switch / message_end / error / done），`message_end` 也只写 assistant 消息。
- 用户消息直到 run 结束 `loadCurrentSessionMessages()`（约 :625）拉权威历史才进入列表。
- 对比 `sendMessage`（约 :392-408）在请求前构造 `{role:'user', content:message, created_at:undefined, agent:null}` 乐观追加进 `messages`——这才是正确模式。

### 拟修改方案

在 `streamSendMessage` 中，守卫（无会话）通过后、调流式通道前，追加乐观用户消息，与 `sendMessage` 同构：

```ts
// 守卫通过后，流式请求发起前
const optimistic: Message = {
  agent: null,
  content: message,
  created_at: undefined,
  role: "user",
};
set((state) => ({
  isStreaming: true,
  requestError: null,
  runError: null,
  streamingAgent: null,
  streamingMessage: null,
  lastSentMessage: message,
  events: [],
  messages: [...state.messages, optimistic],   // 新增：乐观回显用户消息
}));
```

- run 正常结束/失败/降级后，`loadCurrentSessionMessages()` 会以权威历史**整体替换** `messages`，乐观消息自然被权威数据覆盖，无需去重。
- 若担心失败后乐观消息残留：可在 `streamSendMessage` 的 catch 分支按对象引用从末尾移除该乐观消息（复用 `sendMessage` 失败回滚的按引用查找模式，chat-store.ts 约 :435-448）。
- 发送期间输入框已锁定（`isSending`），无并发追加问题。

### 影响面与注意事项

- 仅改 `chat-store.ts` 单文件。
- `streamSendMessage` 相关单测若断言初始 `messages` 长度/内容，需同步更新。
- 附件消息路径（`streamSendMessage(message, attachments)`）同样生效，乐观消息不带附件字段即可（权威历史会补全）。

### 验收口径

1. 发送后输入框清空，消息流**立即**出现用户气泡。
2. 回答期间用户气泡持续可见。
3. 一轮结束后消息列表与权威历史一致，无重复用户消息。

---

## 问题 2：切换会话历史加载无反馈

**级别**：高（点击会话必现）

### 现象

点选会话后，消息区先被清空（`emptyConversationState`），随后 GET 历史消息期间页面**一片空白**——无骨架、无 loading 指示，无法区分「正在加载」与「会话是空的」。后端慢或长会话时空白时间明显，伴随滚动位置异常。

附带现象：侧栏首帧先闪「暂无会话」再切骨架。

### 根因

`frontend/stores/chat-store.ts` + `frontend/components/conversation-panel.tsx`：

- `loadCurrentSessionMessages`（chat-store.ts 约 :241）会 `set({ isLoadingMessages: true })` 并在结束/失败后复位，但**全前端没有任何组件订阅该字段**（conversation-panel.tsx 的订阅列表约 :465-488 不含 `isLoadingMessages`）。字段是「死状态」。
- `selectSession`（约 :271-279）经 `emptyConversationState` 清空 `messages` 后，`session-sidebar.tsx:234-236` 随即调 `loadCurrentSessionMessages()`，等待期 `ConversationContent` 无任何骨架（`isSending` 的 message-skeleton / `isStreaming` 的 streaming-skeleton 都不激活）。
- 首帧闪空态：`chat-store.ts:178` 初始 `isLoadingSessions: false`，挂载 effect 才置 true，首帧 `sessions` 空且未加载 → 渲染「暂无会话」（session-sidebar.tsx:182-190）。

### 拟修改方案

1. **消费 `isLoadingMessages`**：`ConversationPanel` 订阅该字段，当 `isLoadingMessages && messages.length === 0` 时，在消息区渲染 2-3 条消息骨架，复用 `isSending` 的 message-skeleton 样式（conversation-panel.tsx 约 :441-458）。
2. **修首帧闪空态**：`chat-store.ts:178` 把 `isLoadingSessions` 初始值改为 `true`（与 knowledge 页 `listLoading` 初始 true 的先例一致），首帧即渲染侧栏骨架。

### 影响面与注意事项

- `conversation-panel.tsx` + `chat-store.ts`，小改动。
- 骨架复用既有样式，不新增 token/魔法值。
- 空会话（确实无消息）最终显示既有空态，与「加载中骨架」不冲突（isLoadingMessages 结束后才渲染空态）。

### 验收口径

1. 点选会话，消息区立即出现骨架，加载完成后替换为消息。
2. 空会话在加载结束后显示空态（而非一直骨架）。
3. 侧栏首帧不再闪现「暂无会话」。

---

## 问题 3：Markdown 回答正文视觉扁平（一面墙）

**级别**：高（每次读回答都受影响）+ 美观核心

### 现象

回答正文是用户最常读的内容，但目前：

- 标题与正文**同字号**（无层级）；
- 无序列表**无项目符号**、无缩进；
- 引用块**无边框无装饰**；
- 链接**与正文同色、无下划线**，不可辨识为链接；
- 内联代码 **12px 缩水 20% 且无底色**，嵌在 15px 正文里几乎看不见。

长回答无法扫读，视觉上是一整面墙。

### 根因

`frontend/components/assistant-markdown.tsx`：

- `components` prop 只映射 `code / pre / table / thead / th / td`（约 :74-121）。
- Tailwind v4 preflight 已把 `h1-h6` 字号重置为 `inherit`（= 正文 15px）、`ul/ol` 的 `list-style` 清零、`blockquote` 无装饰、`a` 继承前景色。
- `globals.css` 无任何全局 markdown 样式。

### 拟修改方案

**步骤 1：排版地基——新增 18px 字号档（问题 4 的字号档依赖它）**

`globals.css` `@theme inline` 段（`--text-title` 之前）新增：

```css
--text-heading: 1.125rem;            /* 18px */
--text-heading--line-height: 1.75rem; /* 28px */
```

同步 `frontend/DESIGN_SYSTEM.md` §1.4 字号表登记该 token。最终字阶：caption 12 / body 15 / **heading 18** / title 24 / display 32。

**步骤 2：补齐 Markdown 结构映射**

在 `assistant-markdown.tsx` 的 `components` 中补齐：

```tsx
h1:      <h1 className="mt-6 mb-3 text-title font-semibold">
h2:      <h2 className="mt-6 mb-2 text-heading font-semibold">
h3:      <h3 className="mt-5 mb-1.5 text-body font-semibold">
h4:      <h4 className="mt-4 mb-1 text-body font-semibold text-muted-foreground">
p:       <p className="my-2 first:mt-0 last:mb-0">
ul:      <ul className="my-3 list-disc space-y-1.5 pl-5">
ol:      <ol className="my-3 list-decimal space-y-1.5 pl-5">
blockquote: <blockquote className="my-3 border-l-2 border-primary/40 pl-3 text-muted-foreground">
a:       <a className="text-primary underline underline-offset-2 hover:text-primary/80">
strong:  <strong className="font-semibold">
hr:      <hr className="my-4 border-t border-border">
```

**步骤 3：内联代码修复**

把 `<ReactMarkdown>` 包进 `<div className="markdown-body">`（code 组件本身不动，继续 `font-mono text-caption`），并在 `globals.css`（`.hljs` 覆盖规则附近）新增：

```css
.markdown-body :not(pre) > code {
  font-size: 0.8125rem;          /* 13px，正文 87% */
  background-color: var(--muted);
  border-radius: var(--app-radius-sm);
  padding: 0.125rem 0.375rem;
  color: var(--foreground);
}
```

`:not(pre) > code` 只命中行内 code；块代码（12px、`neutral-900` 底）保持现状。

### 影响面与注意事项

- `assistant-markdown.tsx` + `globals.css` + `DESIGN_SYSTEM.md`。
- 现有 `assistant-markdown.test.ts` 是子串断言（`font-mono` / `bg-neutral-900` / `data-slot=*`），加包裹 div 不破坏。
- `em → italic` 可省（preflight 本就斜体）；`hr` 的 `border-t` 部分冗余于全局边框色，可省略 `border-border`。
- 所有类名均为既有语义 token / Tailwind 内置档位，无硬编码色值，符合 DESIGN_SYSTEM。

### 验收口径

1. 回答中标题分级可见、列表有圆点、引用有左边条、链接品牌色下划线、内联代码有灰底且字号≥13px。
2. 长回答可扫读（标题作锚点、列表块状化）。
3. 截图对比前后（含暗色模式）。

---

## 问题 4：全站亮色白海无层次 + 色彩单调

**级别**：高（视觉核心，观感提升杠杆最大）

### 现象（实测数据）

- 浅色页面 **96-98% 像素近白**（空态 96.6%、knowledge 97.9%、stats 96.4%、对话态 87%）。
- 背景 `rgb(249,250,251)` 与卡片/侧栏纯白 `rgb(255,255,255)` **亮度差仅 6/255**——卡片几乎看不见，全部读作「白纸上的更白纸」；侧栏与主区同色，仅靠 1px 边框分隔，无「面板感」。
- 浅色 mean saturation **3-4**（近单色灰白）。
- 品牌蓝全页占比 **<0.7%**，唯一的选中态（当前会话）还用灰色 `bg-muted`。
- 成功/警告色靠 Tailwind 内置 `emerald/amber` 硬编码，与角色 4 色是**两套独立色板**。
- 深色 mean saturation 40-49（角色徽章成「荧光贴片」），与浅色 4 严重不一致——两模式视觉语言分裂。

**证据截图**：`docs/ui-review-shots/01-empty-light.png`（白海空态）、`10-chat-light.png`、`08-mobile-empty.png`。

### 根因

`frontend/app/globals.css`：

- 中性档 `--neutral-50/100/200` chroma 极低、背景与卡片同亮（`--background: var(--neutral-50)`，`--card: oklch(1 0 0)`）。
- `@theme inline` 只映射 `primary / muted / border / destructive`，无 `success / warning / info`。
- 角色色单一值两模式共用；组件硬编码 emerald/amber。

### 拟修改方案

**步骤 1（杠杆最高）：画布压深一档——白海变灰画布**

`:root` 三个中性档各下沉（单一数据源，语义映射保持 `var()` 不变）：

```css
--neutral-50:  oklch(0.985 0.002 247.84) → oklch(0.955 0.004 247.86);  /* 页面背景 */
--neutral-100: oklch(0.951 0.009 264.37) → oklch(0.925 0.009 264.37);  /* muted 底 */
--neutral-200: oklch(0.89 0.012 264.37)  → oklch(0.87 0.012 264.37);   /* 边框 */
```

卡片/侧栏保持纯白 `oklch(1 0 0)` → 背景与卡片亮度差从 ~6/255 提升到 ~15/255（3 倍），全站卡片、两栏分层立现，零组件改动。

注意：`--neutral-50` 同时是亮色代码块字色 `text-neutral-50`（assistant-markdown.tsx:95），压深会微变代码字色，在 `neutral-900` 底上对比仍极高，可接受。

**步骤 2：色温统一——中性色相对齐品牌蓝**

`:root` 中性阶 hue 264.37 → **262.88**（品牌蓝），chroma 微升（sat 3-4 → 6-8）；`.dark` 背景/边框 285.823 → 262.88（**含 `--input`、`--muted-foreground`**，否则破坏「暗色 input 与 border 同值」不变量）。页面底色自带品牌冷蓝温，不再「无色灰」。

**步骤 3：品牌记忆点——选中态与交互热区品牌化**

- `session-sidebar.tsx:224-228` 选中会话 `bg-muted` → `bg-primary/10 ring-1 ring-primary/25`。
- `app-shell.tsx:225/234` 顶栏「知识库/进度」链接 `hover:text-foreground` → `hover:text-primary`。
- `app-shell.tsx:291` 空态示例问题按钮 hover `bg-muted` → `hover:border-primary/40 hover:bg-primary/5`（**替换**而非叠加）。

品牌蓝占比 0.7% → 2-3%，落在用户最高频注视位置。

**步骤 4：语义色补齐 + 角色色统一**

`:root` 新增：

```css
--success: oklch(0.53 0.16 152);   /* 绿 */
--warning: oklch(0.55 0.17 75);    /* 琥珀 */
--info:    var(--brand);           /* 蓝 */
```

`.dark` 增 `--success: oklch(0.72 0.15 152)`、`--warning: oklch(0.76 0.15 75)`、`--info: oklch(0.72 0.16 262.88)`。`@theme inline` 映射 `--color-success/--color-warning/--color-info`。

角色色并入语义族：`--role-evaluator` L 0.66 → 0.55（黄字 on 白对比 3.18 → **4.95:1**，修复已知弱项）。

组件迁移：`text-emerald-600` → `text-success`（app-shell.tsx:242、collaboration-panel.tsx:117）；collaboration-panel.tsx:38 徽章 → `border-success/30 bg-success/10 text-success`；session-sidebar.tsx:246 高亮 mark → `bg-warning/20`（**保留 `text-inherit`**，否则暗色回退黑字）。

**步骤 5（可选，纯 CSS）：暗色角色色降彩提亮**

`.dark` 段新增角色色覆盖：`--role-*` L 0.76-0.80 + 降彩 0.10-0.17，收敛深色 sat 40 → ~30，两模式观感方向一致。需同步 DESIGN_SYSTEM 中 5 处「角色色不覆盖」决策。

### 影响面与注意事项

- 核心是 `globals.css` + `session-sidebar.tsx` + `app-shell.tsx` + collaboration-panel 等组件类名。
- **测试破坏**：改链接 hover 类会挂 `frontend/tests/app-shell.test.ts:191/212` 正则，需同步改为 `hover:text-primary`。
- **文档同步面大**：DESIGN_SYSTEM.md §1.1（中性值）、§1.2（语义色）、§1.3/§2.x（角色色）、§3.2（emerald/amber 魔法值移除）都要同步。
- 新 token（success/warning/info/阴影）需按维护约定先写 globals.css 再登记文档。
- token 只改值不改名，`design-baseline.test.mjs` 只断言 token 名，不破坏。

### 验收口径

1. 卡片/侧栏与页面背景分层肉眼可辨（截图对比）。
2. 页面底色带品牌冷蓝温，不再「无色灰」。
3. 选中会话品牌蓝高亮，链接/按钮 hover 变蓝。
4. 全仓 `emerald/amber` 硬编码清零，成功/警告走语义 token。
5. 浅/深两模式色彩观感方向一致。

---

## 问题 5：会话列表仅展示不透明 session_id（无法区分会话）

**级别**：高（多会话后必现，信息架构核心）

### 现象

侧栏每行只渲染 truncate 的 session_id（后端 uuid4 随机生成，用户不可记忆），搜索框 placeholder 为「搜索会话 ID…」、搜索只匹配 uuid。用户无法凭记忆识别「上次讲反向传播的那个会话」，只能逐个点开试错；会话越多定位成本线性上升，搜索功能几乎不可用。

### 根因

**契约级限制**，前端无法单方修复：

- Session 契约（`frontend/contracts/api.generated.ts` 约 :799-814）只有 `session_id / created_at / archived / user_id` 四字段。
- 后端 SessionStore 表没有 `title` / `last_activity` 列。
- 会话按 `created_at` 分组（今天/近 7 天/更早），但单行内没有任何可读信息。

### 拟修改方案（前后端配合）

1. **后端**：Session 模型与列表/创建接口增加两字段：
   - `title`：取首条用户消息摘要（截断约 30 字）。会话创建时为空，发首条消息后回填；前端兜底「新会话」。
   - `last_activity_at`：最近一条消息的时间，用于行内相对时间展示。
   - 列表接口 `GET /sessions` 与详情接口返回这两个字段。
2. **契约**：重新生成 `api.generated.ts`，前端 `Session` 类型同步。
3. **前端侧栏**：会话行改两行排版——
   - 第一行：`title`（`text-body font-medium text-foreground truncate`），搜索命中时沿用高亮 mark。
   - 第二行：相对时间（`text-caption text-muted-foreground truncate`）。
   - 新增纯函数 `relativeTime(createdAt, now = new Date())` 放 `lib/session-groups.ts`（与 `groupSessions` 同模式、`now` 可注入便于单测），输出「刚刚 / 5 分钟前 / 2 小时前 / 3 天前 / M月D日」。
4. **搜索**：`filterSessions` 同时匹配 `title` 与 `session_id`（不区分大小写）；placeholder 改为「搜索会话…」。
5. 空会话（无首条消息）title 兜底「新会话」。

### 影响面与注意事项

- 后端：sessions 存储层、接口、消息写入时回填 title/last_activity。需后端同事配合。
- 前端：契约重新生成 + `session-sidebar.tsx` + `lib/session-groups.ts` + 搜索逻辑。
- 时区口径：分组按 UTC 自然日、相对时间按本地流逝时长，午夜附近可能「组=近 7 天、行内=30 分钟前」，可接受但需预期。
- `relativeTime` 需防御缺失/非法 `created_at`（参照 groupSessions 的 NaN 归 older）。
- 现有 `session-sidebar.test.ts` 仅断言 data-slot 与 session_id 子串，两行化改动不破坏（但需为新排版补断言）。

### 验收口径

1. 会话行显示「标题 + 相对时间」两行，主信息可读。
2. 搜索能命中标题关键字。
3. 空会话显示「新会话」兜底。
4. 列表接口返回 title/last_activity_at，历史会话 title 已回填。

---

## 附：实施注意事项（跨问题通用）

### 1. 测试破坏点（改动前先跑 `npm test` 建立基线）

| 测试文件 | 被破坏的改动 | 处理 |
|----------|-------------|------|
| `frontend/tests/app-shell.test.ts:191/212` | 顶栏链接 hover 类改 `hover:text-primary`（问题 4） | 同步改正则 |
| `frontend/tests/app-shell.test.ts:157` | 空态标题文案（若做空态重构） | 保留「请选择或新建会话」副标题 |
| `frontend/tests/design-system.test.ts:118` | 抽屉遮罩 `bg-black/40` 改 `bg-black/30`（若做移动端） | 同步改断言 |

### 2. 设计系统约束

- 新 token 先改 `globals.css`，再同步 `frontend/DESIGN_SYSTEM.md`（维护约定①）。
- 新布局魔法值（`max-w-4xl/5xl`、grid 模板等）须先登记 §3.2 允许清单。
- 组件内不出现 oklch/hex/rgb 字面量与 `bg-white/text-black`。

### 3. 两模式一致性

- 涉及 `.dark` 的改动（问题 4 步骤 2/4/5）需同时验证亮/暗色截图。
- 角色色为单值 token（D4-T6 决策），改 evaluator L 会同时影响两模式，暗色对比略有回归（仍在 3:1 以上），需在文档明示取舍。

### 4. 验证流程

每项落地后：`npm run build` 确认无未识别类 → 起服务截图对比 → 跑 `npm test` / `npm run lint` / typecheck 全门禁（事故文档 8.5：dev 模式有 Next 16 回归 bug，验收走 prod 模式 `next build && next start`）。
