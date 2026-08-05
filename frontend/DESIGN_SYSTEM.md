# 设计系统(Design System)

> **用途声明**:本文件是前端开发的唯一样式依据,与 `UI_REFERENCE_BASELINE.md`
> 的中性 shadcn 方向一致。Tokens 的唯一实现在 `frontend/app/globals.css`
> (亮色 `:root` + 暗色 `.dark` + `@theme inline` 映射),本文档只描述与约束,
> 不另立值;组件内不出现硬编码色值,个别布局魔法值以第 3 节允许清单为界。
>
> **版本**:1.0(D5-T1)。**维护约定**:① 新增/修改 token 必须先改
> `globals.css`,再同步第 1 节总表;② 组件样式优先复用既有语义类,任何新的
> 魔法值必须登记进第 3 节允许清单,否则视为违规;③ 本文件与
> `UI_REFERENCE_BASELINE.md` 冲突时,以本文件(实际实现)为准并回写基准文档。

## 1. Tokens 总表

全部变量定义在 `frontend/app/globals.css`:`:root`(基准色/角色色/字体/圆角/
间距/动效)+ `.dark`(暗色语义覆盖)+ `@theme inline`(映射为 Tailwind 工具类)。
`@theme inline` 段由 D4-T6 起保留,测试(design-baseline.test.mjs)断言其存在,
不得删除。

### 1.1 基准色(单一值,亮/暗共用)

| Token | 值 | 用途 | 示例类名 |
| --- | --- | --- | --- |
| `--brand` | `oklch(0.546 0.245 262.88)` | 品牌主色(蓝) | `bg-brand`、`text-brand` |
| `--brand-foreground` | `oklch(0.985 0.002 247.84)` | 品牌色上的前景 | `text-brand-foreground` |
| `--neutral-50` | `oklch(0.985 0.002 247.84)` | 最浅中性色(页面背景基准) | `bg-neutral-50` |
| `--neutral-100` | `oklch(0.951 0.009 264.37)` | 浅中性(muted 底) | `bg-neutral-100` |
| `--neutral-200` | `oklch(0.89 0.012 264.37)` | 浅中性(边框基准) | `border-neutral-200` |
| `--neutral-300` | `oklch(0.81 0.016 264.37)` | 中性(代码块浅色文字) | `text-neutral-300` |
| `--neutral-400` | `oklch(0.67 0.02 264.37)` | 中性 | `text-neutral-400` |
| `--neutral-500` | `oklch(0.507 0.022 264.37)` | 中深中性(muted 前景基准) | `text-neutral-500` |
| `--neutral-600` | `oklch(0.4 0.022 264.37)` | 深中性 | `text-neutral-600` |
| `--neutral-700` | `oklch(0.31 0.021 264.37)` | 深中性(代码块按钮边框) | `border-neutral-700` |
| `--neutral-800` | `oklch(0.255 0.019 264.37)` | 深中性(代码块底、暗色 pre 提亮) | `bg-neutral-800` |
| `--neutral-900` | `oklch(0.205 0.017 264.37)` | 最深中性(代码块底、前景基准) | `bg-neutral-900` |

### 1.2 语义色(亮/暗双值)

| Token | 亮色值 | 暗色值(.dark) | 用途 | 示例类名 |
| --- | --- | --- | --- | --- |
| `--background` | `var(--neutral-50)` | `oklch(0.141 0.005 285.823)` | 页面背景 | `bg-background` |
| `--foreground` | `var(--neutral-900)` | `oklch(0.985 0 0)` | 正文前景 | `text-foreground` |
| `--card` | `oklch(1 0 0)` | `oklch(0.21 0.006 285.885)` | 卡片/面板/侧栏底 | `bg-card` |
| `--card-foreground` | `var(--neutral-900)` | `oklch(0.985 0 0)` | 卡片前景 | `text-card-foreground` |
| `--primary` | `var(--brand)` | `var(--brand)` | 主操作/用户消息气泡 | `bg-primary`、`text-primary` |
| `--primary-foreground` | `var(--brand-foreground)` | `oklch(0.985 0.002 247.84)` | 主操作上的前景 | `text-primary-foreground` |
| `--muted` | `var(--neutral-100)` | `oklch(0.269 0.01 285.91)` | 弱化底(选中会话/悬浮) | `bg-muted` |
| `--muted-foreground` | `var(--neutral-500)` | `oklch(0.708 0.015 285.47)` | 弱化前景(辅助文案) | `text-muted-foreground` |
| `--border` | `var(--neutral-200)` | `oklch(0.269 0.01 285.91)` | 边框/分隔线 | `border-border` |
| `--destructive` | `oklch(0.577 0.245 27.33)` | `oklch(0.704 0.191 22.216)` | 错误/危险 | `text-destructive`、`bg-destructive` |
| `--input`(D5-T1 补齐) | `var(--neutral-200)` | `oklch(0.269 0.01 285.91)` | 输入框边框 | `border-input` |
| `--ring`(D5-T1 补齐) | `var(--brand)` | `var(--brand)` | 焦点环 | `ring-ring`、`focus-visible:ring-ring` |

> D5-T1 说明:`border-input` / `ring-ring` 类此前已被 chat-input 引用但 CSS
> 缺 `--input`/`--ring`,类不生效;已按 shadcn 惯例补齐(亮色与 border 同值,
> 暗色与暗色 border 同值),焦点环统一品牌色。

### 1.3 角色色(四角色徽章,单一值)

| Token | 值 | 角色 | 示例类名 |
| --- | --- | --- | --- |
| `--role-supervisor` | `oklch(0.546 0.245 262.88)` | Supervisor(蓝,同品牌) | `text-role-supervisor` |
| `--role-teaching-assistant` | `oklch(0.52 0.12 193)` | 助教(青) | `text-role-teaching-assistant` |
| `--role-learning-assistant` | `oklch(0.57 0.14 153)` | 助学(绿) | `text-role-learning-assistant` |
| `--role-evaluator` | `oklch(0.66 0.14 75)` | 评价(黄) | `text-role-evaluator` |

### 1.4 字体 / 圆角 / 间距 / 动效

| 类别 | Token | 值 | 用途 | 示例类名 |
| --- | --- | --- | --- | --- |
| 字体 | `--app-font-sans` | `"PingFang SC", "Microsoft YaHei", "Noto Sans CJK SC", Arial, sans-serif` | 全站字体 | `font-sans`(body 默认) |
| 字号 | `--text-caption` | `0.75rem`(行高 1.25rem) | 辅助/徽章小字 | `text-caption` |
| 字号 | `--text-body` | `0.9375rem`(行高 1.625rem) | 正文/消息 | `text-body` |
| 字号 | `--text-title` | `1.5rem`(行高 2rem) | 区块标题 | `text-title` |
| 字号 | `--text-display` | `2rem`(行高 2.5rem) | 大标题 | `text-display` |
| 圆角 | `--app-radius-sm` | `0.375rem` | 小控件/图标按钮 | `rounded-sm` |
| 圆角 | `--app-radius-md` | `0.625rem` | 输入框/列表项/代码块 | `rounded-md` |
| 圆角 | `--app-radius-lg` | `0.875rem` | 卡片/消息气泡/按钮 | `rounded-lg` |
| 间距 | `--space-1/2/3/4/6/8` | `0.25/0.5/0.75/1/1.5/2rem` | 显式声明的关键间距档 | `p-4`、`gap-2` |
| 动效 | `--app-duration-fast` | `120ms` | 即时反馈(悬停/按下) | `transition-colors` + 时长 |
| 动效 | `--app-duration-normal` | `200ms` | 常规过渡 | 同上 |
| 动效 | `--app-duration-slow` | `320ms` | 大面积/抽屉过渡 | 同上 |
| 动效 | `--app-ease-out` | `cubic-bezier(0.22, 1, 0.36, 1)` | 出场缓动 | `transition-[...]` |
| 动效 | `--app-ease-in-out` | `cubic-bezier(0.45, 0, 0.55, 1)` | 对称缓动 | 同上 |

> 间距说明:组件间距全部落在 Tailwind 内置档位(`0.5`–`8`),`--space-*` 只
> 显式声明 6 个关键档;不新增 `--app-spacing-*`(Tailwind spacing 体系已够用,
> 见第 3 节允许清单)。

#### 检查清单

- [ ] 新 token 先写进 `globals.css`,再同步本表
- [ ] 语义色改值只动 `:root`/`.dark` 两处,不碰 `@theme inline` 映射名
- [ ] 组件类名一律来自本表示例列,不写 oklch/hex/rgb 字面量
- [ ] 动效时长只用 3 档之一;新增档位需先在本表登记

## 2. 四角色徽章规范

角色徽章由 `AgentBadge` 组件(`frontend/components/agent-badge.tsx`)统一渲染,
展示映射集中在 `frontend/lib/agent-roles.ts`(唯一数据源,类型来自生成的
API 契约 `AgentRole`)。

### 2.1 色值与徽章样式

| 角色 | 徽章色 `--role-*` | 徽章类(agent-roles.ts `badgeClassName`) | 中文标签 |
| --- | --- | --- | --- |
| supervisor | `oklch(0.546 0.245 262.88)`(蓝) | `border-role-supervisor/30 bg-role-supervisor/10 text-role-supervisor` | Supervisor |
| teaching_assistant | `oklch(0.52 0.12 193)`(青) | `border-role-teaching-assistant/30 bg-role-teaching-assistant/10 text-role-teaching-assistant` | 助教 |
| learning_assistant | `oklch(0.57 0.14 153)`(绿) | `border-role-learning-assistant/30 bg-role-learning-assistant/10 text-role-learning-assistant` | 助学 |
| evaluator | `oklch(0.66 0.14 75)`(黄) | `border-role-evaluator/30 bg-role-evaluator/10 text-role-evaluator` | 评价 |

徽章样式约定(AgentBadge 基类,所有角色一致):

```
inline-flex items-center rounded-full border px-2 py-0.5 text-caption font-medium
```

- 圆角胶囊 `rounded-full`,小号字 `text-caption`(0.75rem)+ `font-medium`;
- 配色公式统一为「30% 色相边框 + 10% 色相底 + 100% 色相文字」,由
  `--role-*` 单一值经透明度派生,亮/暗两模式共用;
- 会话外一律使用 `AgentBadge`,不得手写徽章类(计划步骤条、事件时间线、
  消息流均引用该组件)。

### 2.2 亮/暗对比度说明

- 四个角色色亮度在 0.52–0.66(oklch L),对亮色 card(≈1.0)与暗色 card
  (≈0.21)均有可用对比度;
- D4-T6 决策:角色色为中等饱和度,两模式均不覆盖(globals.css 有注释),
  本版维持「不覆盖」;
- 已知弱项:evaluator(黄,L=0.66)在亮色纯白 card 上的小字对比度约 3:1,
  低于正文 AA 4.5:1;因徽章为小号加粗字 + 10% 底色陪衬,暂列已知项,不
  单独调色;若后续放大字号或强调可读性,优先调 `--role-evaluator` 单点。

#### 检查清单

- [ ] 新角色只改 `globals.css`(`--role-*` + `@theme inline`)与 `agent-roles.ts`
- [ ] 徽章一律走 `AgentBadge`,标签/配色不进组件内联
- [ ] 角色色不在 `.dark` 单独覆盖(维持 D4-T6 决策)

## 3. 组件样式约定

组件一律用语义类(Tokens 总表示例列),通用档位如下;允许的魔法值只限
「3.2 允许清单」,清单外出现即违规。

### 3.1 各类组件类名基准

| 组件 | 基准类名 | 说明 |
| --- | --- | --- |
| 按钮(ui/button.tsx) | `rounded-lg text-sm font-medium transition-colors`;变体:`bg-primary text-primary-foreground hover:bg-primary/90`(default)、`border border-border bg-transparent text-foreground hover:bg-muted`(outline);尺寸:`h-9 px-4 py-2`(default)、`h-8 px-3`(sm) | 仅 default/outline 两变体;ghost 未启用,新增需登记 |
| 卡片(面板类 section) | `rounded-lg border border-border bg-card` + `overflow-hidden` | collaboration-panel、handoff-card 同式 |
| 输入框 textarea/input | `rounded-md border border-border bg-background text-foreground placeholder:text-muted-foreground`;textarea 用 `rounded-lg border-input` + `focus-visible:ring-2 focus-visible:ring-ring`;搜索框用 `focus:ring-2 focus:ring-primary/40` | 输入态边框/焦点环语义化(`--input`/`--ring`) |
| 消息气泡 | 用户:`max-w-[80%] rounded-lg bg-primary px-4 py-3 text-body text-primary-foreground`;助手:`max-w-[80%] rounded-lg border border-border bg-card px-4 py-3 text-body text-foreground` | conversation-panel.tsx |
| 错误块 | `flex items-start gap-3 rounded-lg border border-destructive/30 bg-destructive/5 px-4 py-3` | 运行错误/网络错误统一 |
| 代码块 | `pre`: `overflow-x-auto rounded-md bg-neutral-900 p-3 pt-8 text-neutral-50`;复制按钮:`rounded border border-neutral-700 bg-neutral-800 px-2 py-0.5 text-caption text-neutral-300 hover:bg-neutral-700` | 中性色阶固定档(两模式统一深底浅字,github-dark 依赖);`.dark pre` 提亮为 `--neutral-800` |
| 侧栏条目 | 选中 `bg-muted`、未选中 `hover:bg-muted/60`,`rounded-md px-3 py-2` | session-sidebar |
| 徽章类(状态/标签) | 胶囊 `rounded-full border px-2 py-0.5 text-caption font-medium`,配色走 `border-{色}/30 bg-{色}/10 text-{色}` 公式 | 计划状态/角色徽章 |
| 面板分隔 | `border-b/border-t border-border` + 段内 `px-4 py-2/3` | 面板头部/区块 |

圆角档位:组件只用 `rounded`(0.25rem,Tailwind 默认小圆角,角标/复制按钮)、
`rounded-sm/md/lg`、`rounded-full`;间距档位只用 Tailwind 内置
`0.5/1/1.5/2/2.5/3/3.5/4/5/6/7/8`(含 `p-*`/`px-*`/`py-*`/`m-*`/`gap-*`/
`space-y-*` 与 `mt-*` 微调)。

### 3.2 魔法值允许清单(D5-T1 审计结果)

审计范围:`frontend/components/**/*.tsx`(2026-08 快照)。**未发现 hex/oklch/
rgb 字面量、未发现 `bg-white`/`text-black` 硬编码**;以下为文档明示允许的
布局/语义魔法值(均为 Tailwind 内置档位或业界惯例,亮/暗可读):

| 位置 | 值 | 用途 | 分类 |
| --- | --- | --- | --- |
| app-shell.tsx:61 | `bg-black/40` | 移动端抽屉遮罩(深色半透明,两模式通用) | 遮罩惯例,允许 |
| app-shell.tsx:44 | `md:grid-cols-[18rem_minmax(0,1fr)]` | 桌面两栏布局(侧栏 18rem + 自适应主区) | 布局,允许 |
| app-shell.tsx:65 / session-sidebar.tsx:119 | `w-72` | 抽屉/侧栏宽(18rem) | 布局,允许 |
| conversation-panel.tsx:333 | `max-w-3xl px-8 py-6` | 消息流内容列宽度/内边距 | 内容列,允许 |
| conversation-panel.tsx:412 | `px-8 py-4` | 输入区容器内边距 | 内容列,允许 |
| conversation-panel.tsx:69/70/140 | `max-w-[80%]` | 消息气泡宽度上限 | 布局,允许 |
| citation-list.tsx:72 | `grid-cols-[auto_1fr]` | 引用元信息两列网格 | 布局,允许 |
| 图标/控件尺寸 | `size-3.5/4/5/7/9`、`h-8/9`、`min-h-24`、`max-h-48` | Tailwind 内置档位 | 尺寸档,允许 |
| 色值:`text-emerald-600`(app-shell.tsx:94 在线状态、collaboration-panel.tsx:117 成功勾)、`text-emerald-700`(collaboration-panel.tsx:38 已完成徽章,暗色须配 `dark:text-emerald-400`)、`bg-amber-200/70`(session-sidebar.tsx:221 搜索高亮,暗色须配 `dark:bg-amber-400/30`) | 状态色(内置色阶,非浅色残留) | 语义,允许(暗色变体要求见第 4 节) |

「无硬编码魔法值残留」验收口径:**语义类优先**;个别布局魔法值以上表为
界,新值必须先登记再使用;`bg-white`/`text-black` 及任何 oklch/hex/rgb
字面量一律禁止(组件内),由 design-system.test.ts 基线测试守护。

#### 检查清单

- [ ] 新组件优先抄「3.1 基准类名」,禁止新造色值
- [ ] 新魔法值先登记 3.2 表,否则验收不通过
- [ ] 间距/圆角只用本节约定的档位
- [ ] 按钮变体只增不删,ghost 启用需登记

## 4. 暗色模式规则

### 4.1 语义映射与切换机制

- 亮色/暗色仅通过 `<html class="dark">` 切换;`@custom-variant dark` 使
  `dark:` 变体作用于 `.dark` 后代;
- 切换机制:`next-themes` `ThemeProvider attribute="class" defaultTheme="system"`
  (app/layout.tsx),SSR 首屏跟随 `prefers-color-scheme`,内联脚本 hydration
  前设类防闪烁;app-shell 顶栏按钮手动切换,图标由 `dark:` 变体驱动;
- `.dark` 段只覆盖语义映射(背景/前景/card/muted/border/destructive/
  input/ring),基准色与角色色不覆盖;`@theme inline` 映射名两模式共用。

### 4.2 组件暗色注意点

| 项 | 规则 |
| --- | --- |
| 代码块 | `pre` 用 `bg-neutral-900`(亮色);暗色下由 `.dark pre { background-color: var(--neutral-800) }` 提亮一档,与页面背景区分;复制按钮用 neutral-700/800/300 固定档,两模式一致 |
| 角色徽章 | 维持 D4-T6「不覆盖」决策;见第 2.2 节已知项 |
| 输入焦点环 | `ring-ring` 暗色下仍为品牌色(亮度 0.546,on 暗底 ≥ 3:1,作焦点指示可辨识) |
| 对比度 | 正文/小字语义色须 ≥ WCAG AA:正文 4.5:1、`text-caption`(12px)加粗 3:1;`muted-foreground` 两模式均满足 |
| D5-T1 暗色修复 | ① 搜索高亮 mark:亮色 `bg-amber-200/70`,暗色必须配 `dark:bg-amber-400/30`(否则近白继承文字落在浅黄底上不可读);② 计划「已完成」徽章:`text-emerald-700` 暗色下过暗,必须配 `dark:text-emerald-400`(修复 diff 见 D5-T1 交付报告) |
| 动效 | D5-T2 动效使用 `--app-duration-*`/`--app-ease-*`;`prefers-reduced-motion` 的媒体查询在组件层处理(本文档不强制) |

#### 检查清单

- [ ] 新暗色值只进 `.dark` 段,不写死组件内
- [ ] 组件内浅色背景(amber-200 类)必须带 `dark:` 深色变体
- [ ] 自测两模式截图对比,焦点环/禁用态可辨识
- [ ] 不动 `@theme inline` 与 `.hljs` 覆盖

## 5. 移动端规则

### 5.1 断点语义

| 断点 | 布局 | 说明 |
| --- | --- | --- |
| `< md`(默认) | 单栏 `grid-cols-1`,主区独占整行;侧栏隐藏(`hidden md:block` 的桌面分支 + 抽屉分支) | app-shell.tsx:44 |
| `md` 起 | 两栏 `md:grid-cols-[18rem_minmax(0,1fr)]`:静态侧栏(18rem)+ 自适应主区 | 顶栏内边距 `md:px-8` |

### 5.2 抽屉交互

- 汉堡按钮仅移动端可见(`md:hidden`,size-9 图标按钮)打开抽屉;
- 抽屉:`fixed inset-y-0 left-0 z-40 w-72`,内含同一 `SessionSidebar`;
- 遮罩:`fixed inset-0 z-30 bg-black/40`,点击收起;Esc 键收起;选中会话
  自动收起(`onSessionSelected` 回调);
- SSR 初始态不渲染抽屉/遮罩(`sidebarOpen=false`),开合全在客户端;
- 内容列在移动端沿用 `px-8` 上下 `py-6`,消息气泡 `max-w-[80%]` 自适应。

### 5.3 触控目标

- 图标按钮下限 `size-9`(36px,顶栏汉堡/主题切换/归档按钮);新增大按钮
  建议 ≥ 44px(WCAG 2.5.5);
- 会话条目整行可点(`flex-1` 文本按钮),行高由 `py-2` + `text-caption` 撑起;
- 工具行/折叠按钮 `px-2 py-1` 为小型触控,移动端优先用整行点击区域。

#### 检查清单

- [ ] 新断点一律用 `md:` 前缀,不新增自定义断点
- [ ] 移动端可点元素 ≥ 36px,正文可点元素尽量 ≥ 44px
- [ ] 抽屉/遮罩 z 序固定(遮罩 z-30、抽屉 z-40),新浮层不得低于此档
- [ ] 移动端实测:消息流滚动、输入区聚焦、抽屉开关三路径无横向溢出
