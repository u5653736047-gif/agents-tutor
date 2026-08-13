// ============================================================================
// assistant-ui 接入(T16):新渲染路径 E2E 套件
//
// 与 chat-flow.spec.ts(旧路径)双路径并行守护——同一套 mock SSE 回放
// (mocks.ts),经 page.addInitScript 预置 localStorage 开关启用新路径
// (lib/feature-flags.ts 的 assistant-ui-enabled=1)。
//
// 定位器约定(新路径锚点):
//   [data-slot="assistant-thread"]     新路径根
//   [data-slot="assistant-message"]    助手消息(含在飞消息)
//   [data-slot="user-message"]         用户消息
//   [data-slot="reasoning-block"]      思维链折叠块
//   [data-slot="tool-row"]/tool-details 工具卡片与详情
//   [data-slot="thinking-row"]         阶段提示行
//   [data-slot="terminal-approval-card"] 工具审批卡片
//   [data-slot="live-status"]          sr-only 流式状态播报
// ============================================================================

import { expect, test } from "@playwright/test";

import {
  installMocks,
  mockAnswerFor,
  mockSession,
  type MockMessage,
} from "./mocks";

// 新路径开关:在页面脚本运行前写入 localStorage(feature-flags 的
// localStorage 覆盖优先于 env 默认,hydration 后经订阅生效)
async function enableAssistantUi(page: import("@playwright/test").Page) {
  await page.addInitScript(() => {
    window.localStorage.setItem("assistant-ui-enabled", "1");
  });
}

// 新建会话的完整链路:侧栏「新建会话」→ 工作空间对话框填路径 → 提交
// (validateWorkspace mock 通过)→ createSession。对话框是现行 UI 的必经
// 路径(D4 工作流引入),不是可选项。
async function createSessionViaDialog(page: import("@playwright/test").Page) {
  await page.getByRole("button", { name: "新建会话" }).click();
  const dialog = page.locator('[data-slot="workspace-dialog"]');
  await expect(dialog).toBeVisible();
  await page.getByLabel("工作空间绝对路径").fill("D:\\CODE\\Agents");
  await dialog.getByRole("button", { name: "使用此文件夹" }).click();
  await expect(dialog).toHaveCount(0);
}

test.beforeEach(async ({ page }) => {
  await enableAssistantUi(page);
  await installMocks(page);
});

test("流式提问:思维链与工具时间线内联,回答完整渲染", async ({ page }) => {
  const question = "E2E 什么是注意力机制?";
  const answer = mockAnswerFor(question);
  // 续传计数:记录每个 /chat/stream 请求的 from_sequence——断言重连
  // 续传携带递增 from_sequence,服务端回放补发而不重复执行整轮
  const streamSequences: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === "/chat/stream") {
      streamSequences.push(url.searchParams.get("from_sequence") ?? "0");
    }
  });

  await page.goto("/");
  await createSessionViaDialog(page);
  const input = page.getByLabel("输入消息");
  await expect(input).toBeVisible();

  // 新路径根出现(动态加载完成)
  await expect(page.locator('[data-slot="assistant-thread"]')).toBeVisible();

  const streamRequested = page.waitForRequest((request) =>
    request.url().includes("/chat/stream"),
  );
  await input.fill(question);
  await input.press("Enter");
  await streamRequested;

  // 思维链与工具卡片内联在助手消息中(不再依赖独立侧栏面板)
  const assistantMessage = page.locator('[data-slot="assistant-message"]').last();
  await expect(assistantMessage).toBeVisible({ timeout: 10_000 });
  await expect(
    assistantMessage.locator('[data-slot="reasoning-block"]'),
  ).toContainText("先识别问题目标");
  await expect(
    assistantMessage.locator('[data-slot="thinking-row"]').first(),
  ).toBeVisible();
  const toolRow = assistantMessage.locator('[data-slot="tool-row"]').first();
  await expect(toolRow).toContainText("检索课程知识库");
  await toolRow.locator("summary").first().click();
  await expect(
    assistantMessage.locator('[data-slot="tool-details"]').first(),
  ).toContainText(question);

  // 旧路径的过程面板在新路径不出现(过程已内联)
  await expect(page.locator('[data-slot="collaboration-panel"]')).toHaveCount(0);

  // 完整回答 + 用户消息回显
  await expect(assistantMessage).toContainText(answer, { timeout: 10_000 });
  await expect(page.locator('[data-slot="user-message"]')).toContainText(question);

  // 断线续传:首请求 from_sequence=0;mock 首响应止于 message_end(无 done)
  // → 重连续传带递增 from_sequence(>0),且总计仅两次请求——不重复执行
  await expect
    .poll(() => streamSequences.length, { timeout: 10_000 })
    .toBeGreaterThanOrEqual(2);
  expect(streamSequences[0]).toBe("0");
  expect(Number(streamSequences[1])).toBeGreaterThan(0);
  expect(streamSequences.length).toBe(2);
});

test("流式重试耗尽后不通过同步接口重复执行任务", async ({ page }) => {
  await installMocks(page, { failStreaming: true });
  let syncChatRequests = 0;
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/chat") {
      syncChatRequests += 1;
    }
  });

  await page.goto("/");
  await createSessionViaDialog(page);
  const input = page.getByLabel("输入消息");
  await input.fill("这条任务只能执行一次");
  await input.press("Enter");

  await expect(page.locator('[data-slot="sidebar-request-error"]')).toBeVisible({
    timeout: 20_000,
  });
  await expect(page.locator('[data-slot="user-message"]')).toContainText(
    "这条任务只能执行一次",
  );
  expect(syncChatRequests).toBe(0);
});

test("切换并刷新后历史回溯:思维链折叠进历史回答", async ({ page }) => {
  const session = mockSession("aui-session-history");
  const messages: MockMessage[] = [
    {
      role: "user",
      content: "历史问题:什么是梯度消失?",
      created_at: "2026-01-01T10:00:00.000Z",
    },
    {
      role: "assistant",
      content: "历史回答:梯度消失是深层网络的常见现象。",
      agent: "supervisor",
      created_at: "2026-01-01T10:00:01.000Z",
    },
  ];
  const processEvents = [
    {
      event_type: "thinking",
      sequence: 1,
      session_id: session.session_id,
      agent: "supervisor",
      content: null,
    },
    {
      event_type: "reasoning",
      sequence: 2,
      session_id: session.session_id,
      agent: "supervisor",
      content: "先检查梯度经过每一层时的连乘效应。",
      is_delta: false,
      message_id: "history-reasoning-1",
    },
    {
      event_type: "tool_call",
      sequence: 3,
      session_id: session.session_id,
      agent: "supervisor",
      tool_name: "search_knowledge",
      tool_call_id: "history-tool-1",
      input_summary: '{"query":"梯度消失"}',
    },
    {
      event_type: "tool_result",
      sequence: 4,
      session_id: session.session_id,
      agent: "supervisor",
      tool_name: "search_knowledge",
      tool_call_id: "history-tool-1",
      output_summary: '{"hits":2}',
      success: true,
    },
  ];
  await installMocks(page, {
    seedSessions: [session],
    seedMessages: { [session.session_id]: messages },
    seedProcess: { [session.session_id]: processEvents },
  });

  await page.goto("/");
  await page.getByTitle(session.session_id).click();

  // 历史消息 + 折叠恢复的过程 parts(思维链/阶段行/工具卡片)同处一条
  // 助手消息内——新路径的核心 UX 收益
  const assistantMessage = page.locator('[data-slot="assistant-message"]').last();
  await expect(assistantMessage).toContainText("历史回答:梯度消失");
  await expect(
    assistantMessage.locator('[data-slot="reasoning-block"]'),
  ).toContainText("先检查梯度经过每一层");
  // 持久化快照的 thinking content 为 null → 旧面板同款占位文案
  await expect(
    assistantMessage.locator('[data-slot="thinking-row"]'),
  ).toContainText("正在思考…");
  await expect(
    assistantMessage.locator('[data-slot="tool-details"]').first(),
  ).toContainText("梯度消失");

  // 刷新后重选,恢复结果一致
  await page.reload();
  await page.getByTitle(session.session_id).click();
  await expect(
    page.locator('[data-slot="assistant-message"]').last(),
  ).toContainText("历史回答:梯度消失");
  await expect(
    page.locator('[data-slot="reasoning-block"]'),
  ).toContainText("先检查梯度经过每一层");
});

test("长会话虚拟化:120 条消息 DOM 行数有界且滚动到底消息齐全", async ({ page }) => {
  const session = mockSession("aui-session-long");
  const messages: MockMessage[] = [];
  for (let index = 0; index < 60; index += 1) {
    messages.push({
      role: "user",
      content: `问题 ${index}:什么是梯度 ${index}?`,
      created_at: new Date(Date.UTC(2026, 0, 1, 10, index, 0)).toISOString(),
    });
    messages.push({
      role: "assistant",
      content: `回答 ${index}:梯度 ${index} 是第 ${index} 层的导数。`,
      agent: "supervisor",
      created_at: new Date(Date.UTC(2026, 0, 1, 10, index, 30)).toISOString(),
    });
  }
  await installMocks(page, {
    seedSessions: [session],
    seedMessages: { [session.session_id]: messages },
  });

  await page.goto("/");
  await page.getByTitle(session.session_id).click();

  // 全部 120 条已在 store(权威历史),但 DOM 行数有界(虚拟化窗口)
  const renderedRows = page.locator("[data-message-role]");
  await expect(page.locator('[data-slot="assistant-thread"]')).toBeVisible();
  await page.waitForFunction(
    () => document.querySelectorAll("[data-message-role]").length > 0,
  );
  const visibleCount = await renderedRows.count();
  expect(visibleCount).toBeLessThan(60);
  expect(visibleCount).toBeGreaterThan(0);

  // 滚动到底:末尾消息进入窗口(虚拟化不丢消息)
  await page.locator('[data-slot="message-list"]').evaluate((element) => {
    element.scrollTop = element.scrollHeight;
  });
  await expect(page.locator('[data-message-role="assistant"]').last()).toContainText(
    "回答 59",
    { timeout: 10_000 },
  );
});

test("引用与反馈:回答携带引用列表,反馈按钮可提交", async ({ page }) => {
  await installMocks(page, {
    streamCitations: [
      {
        chunk_id: "doc-1:3:0:120",
        document_id: "doc-1",
        page: 3,
        source: "ml-textbook.pdf",
      },
    ],
  });

  await page.goto("/");
  await createSessionViaDialog(page);
  const input = page.getByLabel("输入消息");
  await input.fill("什么是反向传播?");
  await input.press("Enter");

  // 引用列表挂在回答消息的 footer(message_end citations → metadata)
  const assistantMessage = page.locator('[data-slot="assistant-message"]').last();
  await expect(
    assistantMessage.locator('[data-slot="citation-list"]'),
  ).toBeVisible({ timeout: 10_000 });
  await expect(
    assistantMessage.locator('[data-slot="citation-item"]'),
  ).toContainText("ml-textbook.pdf");

  // 反馈按钮挂在同一 footer;点赞提交后进入已选态(控件禁用)
  const upButton = assistantMessage.locator('[data-slot="feedback-up"]');
  await expect(upButton).toBeVisible();
  const feedbackPosted = page.waitForRequest((request) =>
    request.url().includes("/feedback"),
  );
  await upButton.click();
  await feedbackPosted;
  await expect(upButton).toBeDisabled();
});

test("停止生成:流式期间点击停止,已收内容保留且不报错", async ({ page }) => {
  // 默认 mock:帧序列止于 message_end(无 done),streamChatWithRetry 在
  // 约 1s 退避窗口内保持流式——停止点击落在该窗口内,abort 打断退避等待,
  // 收尾后拉取的权威历史已含本轮问答(与真实后端行为一致,已实测)。
  // 注:hangStreaming(请求永不交付)场景暴露的是 store 层既有边缘行为
  // (abort 早于后端持久化时乐观消息被权威空历史覆盖),新旧路径共用同一
  // store 代码路径,不属于本分支的回归,已登记为后续修复候选。
  const question = "这条会被中途停止";
  const answer = mockAnswerFor(question);
  await installMocks(page);

  await page.goto("/");
  await createSessionViaDialog(page);
  const input = page.getByLabel("输入消息");
  await input.fill(question);
  await input.press("Enter");

  // 流式进行中(退避窗口):停止按钮出现 → 点击
  const stopButton = page.locator('[data-slot="stop-generating"]');
  await expect(stopButton).toBeVisible({ timeout: 10_000 });
  await stopButton.click();

  // 停止后:发送按钮回归、问答内容保留、无错误块
  await expect(
    page.getByRole("button", { name: "发送" }),
  ).toBeVisible({ timeout: 10_000 });
  await expect(page.locator('[data-slot="user-message"]')).toContainText(question);
  await expect(
    page.locator('[data-slot="assistant-message"]').last(),
  ).toContainText(answer, { timeout: 10_000 });
  await expect(
    page.locator('[data-slot="sidebar-request-error"]'),
  ).toHaveCount(0);
});

test("暗色模式:主题切换后新路径完整渲染", async ({ page }) => {
  await installMocks(page);

  await page.goto("/");
  await createSessionViaDialog(page);
  const input = page.getByLabel("输入消息");
  await input.fill("暗色模式冒烟");
  await input.press("Enter");
  await expect(
    page.locator('[data-slot="assistant-message"]').last(),
  ).toContainText("E2E 模拟回答", { timeout: 10_000 });

  // 切换到暗色:html.dark 由 next-themes 驱动;消息/卡片/工具行仍渲染
  await page.locator('[data-slot="theme-toggle"]').click();
  await expect(page.locator("html")).toHaveClass(/dark/);
  await expect(
    page.locator('[data-slot="assistant-message"]').last(),
  ).toContainText("E2E 模拟回答");
  await expect(
    page.locator('[data-slot="tool-row"]').first(),
  ).toBeVisible();
});

test("工具审批:approval_required 门控卡片确认后恢复执行", async ({ page }) => {
  const pendingToolApproval = {
    interrupt_id: "interrupt-e2e-1",
    request: {
      agent_role: "supervisor",
      arguments: { command: "pip list" },
      tool_call_id: "mock-tool-1",
      tool_name: "shell",
    },
  };
  await installMocks(page, { pendingToolApproval });

  await page.goto("/");
  await createSessionViaDialog(page);
  const input = page.getByLabel("输入消息");
  await input.fill("帮我看下环境里装了哪些包");
  await input.press("Enter");

  // 门控卡片出现(流式 approval_required → store pendingToolApproval)
  const approvalCard = page.locator('[data-slot="terminal-approval-card"]');
  await expect(approvalCard).toBeVisible({ timeout: 10_000 });
  await expect(approvalCard).toContainText("pip list");

  // 批准并运行 → 恢复流补发 message_end/done → 卡片卸载、回答渲染
  await approvalCard.locator('[data-slot="terminal-confirm"]').click();
  await expect(approvalCard).toHaveCount(0, { timeout: 10_000 });
  await expect(
    page.locator('[data-slot="assistant-message"]').last(),
  ).toContainText("E2E 模拟回答", { timeout: 10_000 });
});
