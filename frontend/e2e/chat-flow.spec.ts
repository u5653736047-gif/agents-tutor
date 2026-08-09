// ============================================================================
// D6-T8 主流程 E2E:对齐 README 手动验收路径
// (建会话 → 提问 → 流式回答 → 审批 → 刷新回溯 → 归档)
//
// 定位器约定:优先 data-slot / data-message-role / aria-label(组件已具备,
// 见各组件源码),不依赖中文文案文本:
//   [data-slot="streaming-message"]   流式气泡(conversation-panel)
//   [data-message-role="assistant|user"] 消息行(MessageRow)
//   [data-slot="handoff-card"]        审批卡片(handoff-card)
//   [data-slot="handoff-confirm"]     审批「确认」按钮
//   [data-slot="archive-toggle"]      归档视图切换
//   getByRole("button", { name: "新建会话" }) 侧栏新建按钮
//   getByLabel("输入消息")            输入框(aria-label)
//   getByRole("button", { name: "归档会话 <id>" }) 会话行归档按钮(aria-label)
//
// 等待策略:mock SSE 瞬时完成,一律用 expect(...).toBeVisible() 自动轮询,
// 不手动 sleep(唯一例外:用例 1 依赖 mock 首响应截断制造的 ~1s 重连窗口,
// 这是 Playwright 断言窗口而非 sleep)。
//
// @real 用例默认跳过:真实 DeepSeek 冒烟需 REAL_E2E=1 + 真实后端
// (127.0.0.1:8000)+ 根目录 .env 凭证,由主线程手动执行。
// ============================================================================

import { expect, test } from "@playwright/test";

import {
  installMocks,
  mockAnswerFor,
  mockSession,
} from "./mocks";

// 默认 mock 模式安装;@real 用例(REAL_E2E=1)不装 mock,走真实后端
test.beforeEach(async ({ page }) => {
  if (process.env.REAL_E2E !== "1") {
    await installMocks(page);
  }
});

test("创建会话并提问:流式气泡出现,message_end 后完整回答渲染", async ({ page }) => {
  const question = "E2E 什么是注意力机制?";
  const answer = mockAnswerFor(question);

  await page.goto("/");
  // 空态:侧栏无会话
  await expect(page.getByText("暂无会话")).toBeVisible();

  // 新建会话(侧栏按钮,aria-label="新建会话")
  await page.getByRole("button", { name: "新建会话" }).click();
  const input = page.getByLabel("输入消息");
  await expect(input).toBeVisible();

  // 提问:先挂 waitForRequest 再触发,证明请求确实走了流式通道
  const streamRequested = page.waitForRequest((request) =>
    request.url().includes("/chat/stream"),
  );
  await input.fill(question);
  await input.press("Enter");
  await streamRequested;

  // 流式气泡出现(mock 首响应止于 message_end,重连续传 done 前有约
  // 1s 窗口,气泡持续可见,见 mocks.ts 注释)
  await expect(page.locator('[data-slot="streaming-message"]')).toBeVisible({
    timeout: 10_000,
  });
  // 当前轮的思考摘要、工具调用与子代理轨迹必须在回答旁实时可见。
  const processPanel = page.locator('[data-slot="collaboration-panel"]');
  await expect(processPanel).toBeVisible();
  await expect(processPanel).toContainText("执行过程");
  await expect(processPanel).toContainText("检索课程知识库");
  const reasoningBlock = processPanel.locator('[data-slot="reasoning-block"]');
  await expect(reasoningBlock).toBeVisible();
  await reasoningBlock.locator("summary").click();
  await expect(reasoningBlock).toContainText("先识别问题目标");
  const toolRows = processPanel.locator('[data-slot="tool-row"]');
  await toolRows.first().click();
  await expect(processPanel.locator('[data-slot="tool-details"]').first()).toContainText(
    question,
  );
  await toolRows.last().click();
  await expect(processPanel.locator('[data-slot="tool-details"]').last()).toContainText(
    '"hits":2',
  );

  // message_end 后完整回答渲染(气泡并入消息列表,权威历史覆盖)
  await expect(page.locator('[data-message-role="assistant"]').last()).toContainText(
    answer,
    { timeout: 10_000 },
  );
  // 用户消息回显
  await expect(page.locator('[data-message-role="user"]')).toContainText(question);
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
  await page.getByRole("button", { name: "新建会话" }).click();
  const input = page.getByLabel("输入消息");
  await expect(input).toBeVisible();
  await input.fill("这条任务只能执行一次");
  await input.press("Enter");

  // 初次 + 3 次流式重试约 7s；耗尽后显示服务错误并保留用户消息，
  // 绝不能自动 POST /chat 原样重发。mock 返回结构化 internal_error，
  // 因此错误由侧栏统一呈现，而不是网络错误专用块。
  await expect(page.locator('[data-slot="sidebar-request-error"]')).toBeVisible({
    timeout: 20_000,
  });
  await expect(page.locator('[data-message-role="user"]')).toContainText(
    "这条任务只能执行一次",
  );
  expect(syncChatRequests).toBe(0);
});

test("切换并刷新后历史回溯:消息与协作过程仍可渲染", async ({ page }) => {
  const session = mockSession("mock-session-history");
  const otherSession = mockSession("mock-session-other");
  const messages = [
    {
      role: "user" as const,
      content: "历史问题:E2E 什么是梯度消失?",
      created_at: "2026-01-01T10:00:00.000Z",
    },
    {
      role: "assistant" as const,
      content: "历史回答:梯度消失是深层网络反向传播时梯度趋近于零的现象。",
      agent: "supervisor" as const,
      created_at: "2026-01-01T10:00:01.000Z",
    },
  ];
  const processEvents = [
    {
      event_type: "reasoning",
      sequence: 1,
      session_id: session.session_id,
      agent: "supervisor",
      content: "先检查梯度经过每一层时的连乘效应。",
      is_delta: false,
      message_id: "history-reasoning-1",
    },
    {
      event_type: "tool_call",
      sequence: 2,
      session_id: session.session_id,
      agent: "supervisor",
      tool_name: "search_knowledge",
      tool_call_id: "history-tool-1",
      input_summary: '{"query":"梯度消失"}',
    },
    {
      event_type: "tool_result",
      sequence: 3,
      session_id: session.session_id,
      agent: "supervisor",
      tool_name: "search_knowledge",
      tool_call_id: "history-tool-1",
      output_summary: '{"hits":2}',
      success: true,
    },
  ];
  await installMocks(page, {
    seedSessions: [session, otherSession],
    seedMessages: { [session.session_id]: messages },
    seedProcess: { [session.session_id]: processEvents },
  });

  await page.goto("/");
  // 会话出现在侧栏,点选后历史消息渲染
  const sessionButton = page.getByTitle(session.session_id);
  await expect(sessionButton).toBeVisible();
  await sessionButton.click();
  await expect(page.locator('[data-message-role="user"]')).toContainText(
    "历史问题:E2E 什么是梯度消失?",
  );
  await expect(page.locator('[data-message-role="assistant"]')).toContainText(
    "历史回答:梯度消失",
  );
  await expect(page.locator('[data-slot="reasoning-block"]')).toContainText(
    "先检查梯度经过每一层",
  );
  await expect(page.locator('[data-slot="tool-details"]').first()).toContainText(
    "梯度消失",
  );

  // 切到另一会话再切回，必须从后端快照恢复协作过程。
  await page.getByTitle(otherSession.session_id).click();
  await expect(page.locator('[data-slot="reasoning-block"]')).toHaveCount(0);
  await page.getByTitle(session.session_id).click();
  await expect(page.locator('[data-slot="reasoning-block"]')).toContainText(
    "先检查梯度经过每一层",
  );

  // 刷新页面(内存状态清空)→ 重新从 mock 拉取 → 消息与协作过程仍在。
  await page.reload();
  const sessionButtonAfterReload = page.getByTitle(session.session_id);
  await expect(sessionButtonAfterReload).toBeVisible();
  await sessionButtonAfterReload.click();
  await expect(page.locator('[data-message-role="user"]')).toContainText(
    "历史问题:E2E 什么是梯度消失?",
  );
  await expect(page.locator('[data-message-role="assistant"]')).toContainText(
    "历史回答:梯度消失",
  );
  await expect(page.locator('[data-slot="reasoning-block"]')).toContainText(
    "先检查梯度经过每一层",
  );
  await expect(page.locator('[data-slot="tool-details"]').last()).toContainText(
    '"hits":2',
  );
});

test("归档会话:从默认列表消失,归档视图可见", async ({ page }) => {
  const s1 = mockSession("mock-session-archive-a");
  const s2 = mockSession("mock-session-archive-b");
  await installMocks(page, { seedSessions: [s1, s2] });

  await page.goto("/");
  await expect(page.getByTitle(s1.session_id)).toBeVisible();
  await expect(page.getByTitle(s2.session_id)).toBeVisible();

  // 归档 s1(归档按钮 aria-label="归档会话 <id>")
  await page.getByRole("button", { name: `归档会话 ${s1.session_id}` }).click();
  await expect(page.getByTitle(s1.session_id)).toHaveCount(0);
  await expect(page.getByTitle(s2.session_id)).toBeVisible();

  // 归档视图:切换后 mock 列表(include_archived=true)带回归档会话
  await page.locator('[data-slot="archive-toggle"]').click();
  await expect(page.getByTitle(s1.session_id)).toBeVisible();
});

// ── 真实 DeepSeek 冒烟(默认跳过,主线程手动执行) ─────────────────
// 执行方式:REAL_E2E=1 npx playwright test(需真实后端 127.0.0.1:8000
// 与根目录 .env 的 DEEPSEEK_* 凭证;playwright.config.ts 在 REAL_E2E=1
// 时不覆盖 NEXT_PUBLIC_API_BASE_URL,dev server 走真实后端)。
test.describe("真实 DeepSeek 冒烟(手动)", () => {
  test.skip(
    process.env.REAL_E2E !== "1",
    "真实凭证冒烟:设置 REAL_E2E=1 并启动真实后端后手动执行",
  );

  test("@real 真实提问冒烟", async ({ page }) => {
    test.setTimeout(150_000);
    await page.goto("/");
    await page.getByRole("button", { name: "新建会话" }).click();
    const input = page.getByLabel("输入消息");
    await expect(input).toBeVisible();
    await input.fill("用一句话介绍反向传播");
    const processRefreshed = page.waitForResponse(
      (response) =>
        response.request().method() === "GET" &&
        /\/sessions\/[^/]+\/process$/.test(new URL(response.url()).pathname),
      { timeout: 120_000 },
    );
    await input.press("Enter");
    // 运行事件应先让执行过程面板出现，证明真实后端而非仅 mock 能把
    // 思考摘要、工具/子代理生命周期推到页面。
    await expect(page.locator('[data-slot="collaboration-panel"]')).toBeVisible({
      timeout: 30_000,
    });
    // 真实模型回答耗时不定,放宽超时;助手回答出现即冒烟通过
    await expect(
      page.locator('[data-message-role="assistant"]').last(),
    ).toBeVisible({ timeout: 120_000 });
    expect((await processRefreshed).status()).toBe(200);
    await expect(page.locator('[data-slot="sidebar-request-error"]')).toHaveCount(0);
  });
});
