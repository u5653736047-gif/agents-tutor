# UI 参考基准调研（W0-T7）

> 调研日期：2026-08-02
> 用途：为阶段三骨架期确定布局范式、组件划分与配色方向。本文不引入或复制任一候选项目的代码；视觉细节、动效、深色模式和移动端适配仍留给细节清单。

## 许可证判定规则

- 仅 MIT、Apache-2.0、BSD 等宽松许可证的代码，才可在保留原版权、许可证、免责声明和适用 NOTICE 的前提下采用。
- GPL、AGPL 或带额外商业/分发限制的项目，只能参考设计，不复制代码。
- 根目录许可证与包元数据冲突时，以仓库根许可证文本作保守判定。

## 候选项目评估

| 候选 | 技术栈与结构 | UI / 工程亮点 | License 与采用结论 |
| --- | --- | --- | --- |
| [Vercel Chatbot](https://github.com/vercel/chatbot)（原 `vercel/ai-chatbot`） | Next.js App Router、React Server Components / Server Actions、Vercel AI SDK、shadcn/ui、Tailwind、Radix；以 `app/`、`components/`、`hooks/`、`lib/`、`tests/` 分层。 | 成熟的聊天产品模板；页面、可复用组件、业务工具和测试职责清晰。仓库含 CI、Husky、Biome、Playwright、TypeScript 和 Drizzle 配置，是可靠的工程质量信号。README / 许可证没有给出可直接照抄的色值，因此只借鉴中性 shadcn 方向。 | [Apache-2.0](https://github.com/vercel/chatbot/blob/main/LICENSE)，`Copyright 2024 Vercel, Inc.`。**可直接采用代码**，但必须一并保留版权、Apache-2.0 许可证、免责声明及适用的 NOTICE；本阶段决定不复制代码。 |
| [Chatbot UI](https://github.com/mckaywrigley/chatbot-ui) | Next.js 14、React 18、TypeScript、Tailwind、Radix UI、Supabase；按 `app/`、`components/`、`context/`、`db/`、`lib/`、`supabase/`、`types/` 分层。 | 典型聊天侧栏 + 主对话区的划分，前端组件层次直观；仓库配置了 Jest / Testing Library、ESLint、Prettier、Husky。Supabase / 数据层与本项目后端 API 桥接目标不匹配，不能照搬。 | [MIT](https://github.com/mckaywrigley/chatbot-ui/blob/main/license)，`Copyright (c) 2024 Mckay Wrigley`。**可直接采用代码**，但每个副本或实质部分必须保留该版权及 MIT 许可；本阶段决定不复制代码。 |
| [LobeHub](https://github.com/lobehub/lobehub)（原 `lobehub/lobe-chat` 已重定向） | TypeScript / React，Next.js 与 Vite SPA，pnpm / Bun，Vitest / Playwright；monorepo 覆盖 UI、图标、语音 hooks 与应用。 | 多 Agent / 工具型聊天产品的信息层级与组件生态很有参考价值，可作为复杂对话界面的设计上限参考。根 `LICENSE` 与 `package.json` 的 `license` 字段不一致，按根许可证保守处理。 | [LobeHub Community License](https://github.com/lobehub/lobehub/blob/main/LICENSE)：虽以 Apache-2.0 为基础，但衍生作品分发有额外商业许可条件。**仅参考设计，不复制代码**。 |

## 选定的骨架期基准

1. **主基准：Vercel Chatbot。** 它与既定的 Next.js App Router、Tailwind、shadcn/ui 技术选型直接对齐，适合作为页面/组件/业务工具分层的参考。
2. **辅基准：Chatbot UI。** 它适合作为桌面聊天双栏布局与会话组件拆分的参考；其 Supabase 数据层不纳入本项目。

LobeHub 保留为“仅设计参考”：可观察复杂 Agent 会话的层级处理，但不得复制其实现。

## 对后续 W1 的约束性落点

- **布局范式：** 桌面端左侧会话栏、右侧对话工作区；本任务不实现移动端抽屉或视觉打磨。
- **组件划分：** 保持本清单约定的 `app/`、`components/`、`lib/`、`stores/`，在 `components/` 内按会话侧栏、消息流、输入区和角色徽章拆分；API 封装只放 `lib/`，客户端状态只放 `stores/`。
- **配色方向：** 使用 shadcn 兼容的中性色阶和一个品牌主色；四个 Agent 角色的固定徽章色将在 W1-T1 统一定义。本文不从候选项目提取或固化色值。
- **代码采用决定：** W0/W1 均以本仓库原生实现为准，不基于任何候选项目代码改造。因此不需要引入其依赖、数据模型或许可证文件；若未来逐段采用许可代码，必须在对应提交中保留源项目要求的版权、许可证与 NOTICE。

## 证据链接

- Vercel Chatbot：[README](https://github.com/vercel/chatbot#readme) · [LICENSE](https://github.com/vercel/chatbot/blob/main/LICENSE)
- Chatbot UI：[README](https://github.com/mckaywrigley/chatbot-ui#readme) · [MIT LICENSE](https://github.com/mckaywrigley/chatbot-ui/blob/main/license)
- LobeHub：[README](https://github.com/lobehub/lobehub#readme) · [根 LICENSE](https://github.com/lobehub/lobehub/blob/main/LICENSE)
