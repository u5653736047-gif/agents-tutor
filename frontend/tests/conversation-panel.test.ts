import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import test from "node:test";

const panelPath = new URL("../components/conversation-panel.tsx", import.meta.url);

async function loadConversationPanel() {
  assert.ok(existsSync(panelPath), "missing conversation panel");
  return import("../components/conversation-panel");
}

test("the conversation panel distinguishes messages and shows Agent, error, and sending state", async () => {
  const { ConversationContent } = await loadConversationPanel();

  assert.equal(typeof ConversationContent, "function", "missing conversation content renderer");
  const markup = renderToStaticMarkup(
    createElement(ConversationContent, {
      isSending: true,
      messages: [
        { agent: null, content: "用户的问题", role: "user" },
        { agent: "supervisor", content: "助手的回答", role: "assistant" },
      ],
      runError: {
        agent: "supervisor",
        error_code: "model_call_failed",
        message: "模型暂时不可用。",
      },
    }),
  );

  assert.match(markup, /data-message-role="user"/);
  assert.match(markup, /data-message-role="assistant"/);
  assert.match(markup, /用户的问题/);
  assert.match(markup, /助手的回答/);
  assert.match(markup, /Supervisor/);
  // D5-T3:同步路径发送态为骨架气泡(不再渲染「正在生成回答」文案)
  assert.match(markup, /data-slot="message-skeleton"/);
  assert.doesNotMatch(markup, /正在生成回答/);
  assert.match(markup, /模型暂时不可用。/);
});

test("the conversation panel keeps an end anchor for automatic scrolling", () => {
  assert.ok(existsSync(panelPath), "missing conversation panel");
  const panel = readFileSync(panelPath, "utf8");

  assert.match(panel, /scrollIntoView/);
  assert.match(panel, /data-slot="conversation-end"/);
});

// D2-T5:错误降级 UX——runError 分类渲染与重试按钮 ———————————————————
test("the conversation panel renders a categorized run error with a retry button when onRetry is provided", async () => {
  const { ConversationContent } = await loadConversationPanel();

  const markup = renderToStaticMarkup(
    createElement(ConversationContent, {
      isSending: false,
      isStreaming: false,
      messages: [],
      onRetry: () => undefined,
      runError: {
        agent: "supervisor",
        error_code: "session_busy",
        message: "A request is already running.",
      },
      streamingAgent: null,
      streamingMessage: null,
    }),
  );

  // 分类标题 + 说明 + 原始 message 保留 + 重试按钮
  assert.match(markup, /会话正忙/);
  assert.match(markup, /该会话正在处理其他请求/);
  assert.match(markup, /A request is already running/);
  assert.match(markup, /data-slot="run-error-retry"/);
  // 按钮文案优先用预设 action(「稍后再试」)
  assert.match(markup, /稍后再试/);
});

test("the conversation panel omits the retry button when onRetry is absent", async () => {
  const { ConversationContent } = await loadConversationPanel();

  const markup = renderToStaticMarkup(
    createElement(ConversationContent, {
      isSending: false,
      isStreaming: false,
      messages: [],
      runError: {
        agent: "supervisor",
        error_code: "model_call_failed",
        message: "模型暂时不可用。",
      },
      streamingAgent: null,
      streamingMessage: null,
    }),
  );

  // 分类文案(标题 + 说明)渲染;无 onRetry 时无重试按钮
  assert.match(markup, /模型服务暂不可用/);
  assert.match(markup, /模型调用失败/);
  assert.doesNotMatch(markup, /data-slot="run-error-retry"/);
});

test("the conversation panel shows a network error block with retry for null request code", async () => {
  const { ConversationContent } = await loadConversationPanel();
  const { ApiClientError } = await import("../lib/api-client");

  const markup = renderToStaticMarkup(
    createElement(ConversationContent, {
      isSending: false,
      isStreaming: false,
      messages: [],
      onRetry: () => undefined,
      requestError: new ApiClientError("网络失败。", { code: null, status: null }),
      runError: null,
      streamingAgent: null,
      streamingMessage: null,
    }),
  );

  // 网络失败(code===null)在消息流内显示错误块 + 重试按钮
  assert.match(markup, /网络请求失败/);
  assert.match(markup, /请检查网络连接后重试/);
  assert.match(markup, /data-slot="request-error-network"/);
  assert.match(markup, /data-slot="request-error-retry"/);
  // 非网络错误码不渲染该块(如 session_busy 只走侧栏映射)
  const busyMarkup = renderToStaticMarkup(
    createElement(ConversationContent, {
      isSending: false,
      isStreaming: false,
      messages: [],
      requestError: new ApiClientError("忙。", { code: "session_busy", status: null }),
      runError: null,
      streamingAgent: null,
      streamingMessage: null,
    }),
  );
  assert.doesNotMatch(busyMarkup, /data-slot="request-error-network"/);
});

// D4-T8:虚拟化与性能(长会话渲染) —————————————————————————————
test("the conversation panel fully renders short conversations without virtualization", async () => {
  const { ConversationContent } = await loadConversationPanel();

  const markup = renderToStaticMarkup(
    createElement(ConversationContent, {
      isSending: false,
      isStreaming: false,
      messages: [
        { agent: null, content: "问题一", role: "user" },
        { agent: "supervisor", content: "回答一", role: "assistant" },
        { agent: null, content: "问题二", role: "user" },
      ],
      runError: null,
      streamingAgent: null,
      streamingMessage: null,
    }),
  );

  // 短列表(<50 条)未启用虚拟化:3 条消息全部渲染,且不输出虚拟化 data-index
  assert.equal(markup.match(/data-message-role=/g)?.length, 3);
  assert.match(markup, /问题一/);
  assert.match(markup, /回答一/);
  assert.match(markup, /问题二/);
  assert.doesNotMatch(markup, /data-index/);
});

test("ConversationContent skips message rows when virtualItems is provided", async () => {
  // D4-T8 review should-fix:虚拟化启用分支的行为级测试——ConversationContent
  // 收到非 null virtualItems 时,消息行渲染由 ConversationPanel 的虚拟窗口
  // 负责(此处跳过),流式气泡/错误块/sending 等尾部块仍渲染。
  const { ConversationContent } = await loadConversationPanel();

  const markup = renderToStaticMarkup(
    createElement(ConversationContent, {
      isSending: true,
      isStreaming: false,
      messages: [
        { agent: null, content: "不应渲染的全量消息", role: "user" },
        { agent: "supervisor", content: "全量路径才有的回答", role: "assistant" },
      ],
      runError: null,
      streamingAgent: null,
      streamingMessage: null,
      virtualItems: [{ index: 0 }],
    }),
  );

  // 消息行被跳过(虚拟窗口负责),但尾部块仍在(发送态骨架气泡)
  assert.doesNotMatch(markup, /不应渲染的全量消息/);
  assert.doesNotMatch(markup, /全量路径才有的回答/);
  assert.doesNotMatch(markup, /data-message-role=/);
  assert.match(markup, /data-slot="message-skeleton"/);
});

test("the conversation panel virtualizes long message lists behind a threshold", () => {
  const panel = readFileSync(panelPath, "utf8");

  // 阈值开关:超过 50 条才启用虚拟化(短会话保持既有全量渲染)
  assert.match(panel, /useVirtualizer/);
  assert.match(panel, /enabled: messages\.length > 50/);
  // 动态行高测量 + 滚动容器贴底跟随判定(防回归)
  assert.match(panel, /measureElement/);
  assert.match(panel, /onScroll=\{handleScroll\}/);
  assert.match(panel, /isNearBottom\(/);
  assert.match(panel, /data-slot="message-list"/);
});

test("MessageRow renders a single message row for the virtualized window", async () => {
  const { MessageRow } = await loadConversationPanel();

  const markup = renderToStaticMarkup(
    createElement(MessageRow, {
      message: {
        agent: "supervisor",
        content: "窗口内的回答",
        role: "assistant",
      },
      index: 42,
    }),
  );

  assert.match(markup, /data-message-role="assistant"/);
  assert.match(markup, /窗口内的回答/);
  assert.match(markup, /Supervisor/);
});

test("MessageRow renders data-index on virtualized rows for measureElement", async () => {
  const { MessageRow } = await loadConversationPanel();

  const markup = renderToStaticMarkup(
    createElement(MessageRow, {
      message: { agent: null, content: "窗口中的问题", role: "user" },
      index: 42,
      dataIndex: 42,
      measureRef: () => undefined,
    }),
  );

  // measureElement 依赖 data-index 定位行索引;行内容与全量路径一致
  assert.match(markup, /data-index="42"/);
  assert.match(markup, /data-message-role="user"/);
  assert.match(markup, /窗口中的问题/);
});

// D5-T2:动效与过渡(消息进入动画) ————————————————————————
test("only the newest message carries the enter animation in full rendering", async () => {
  const { ConversationContent } = await loadConversationPanel();

  const markup = renderToStaticMarkup(
    createElement(ConversationContent, {
      isSending: false,
      isStreaming: false,
      messages: [
        { agent: null, content: "历史问题", role: "user" },
        { agent: "supervisor", content: "新回答", role: "assistant" },
      ],
      runError: null,
      streamingAgent: null,
      streamingMessage: null,
    }),
  );

  // 两条消息仅最后一条(新消息)带 animate-in;历史消息不带,
  // 避免虚拟化/滚动挂载时逐行重播动画闪动
  assert.equal((markup.match(/animate-in/g) ?? []).length, 1);
  // 按 SSR 输出顺序定位:animate-in 出现在第一条消息内容之后、
  // 最后一条消息内容之前(即最后一条 article 的 class 上)
  assert.ok(markup.indexOf("历史问题") < markup.indexOf("animate-in"));
  assert.ok(markup.indexOf("animate-in") < markup.indexOf("新回答"));
});

test("the streaming bubble carries a fade-in animation", async () => {
  const { ConversationContent } = await loadConversationPanel();

  const markup = renderToStaticMarkup(
    createElement(ConversationContent, {
      isSending: false,
      isStreaming: true,
      messages: [],
      runError: null,
      streamingAgent: "supervisor",
      streamingMessage: null,
    }),
  );

  // 流式气泡挂载即新消息:class 含 animate-in fade-in-0(仅淡入,
  // 内容逐字渲染时不做位移动画)
  assert.match(
    markup,
    /<article class="[^"]*animate-in[^"]*"[^>]*data-slot="streaming-message"/,
  );
  assert.match(markup, /animate-in fade-in-0/);
});

test("the enter animation is limited to the newest message in source", () => {
  const panel = readFileSync(panelPath, "utf8");

  // 全量路径仅最后一条传 animate(index === messages.length - 1);
  // 虚拟化窗口路径不传(prop 默认 false),滚动挂载不闪动(review 防回归)
  assert.match(panel, /animate=\{index === messages\.length - 1\}/);
  // 虚拟化分支不带动画类(无 animate= 传参),仅全量分支出现
  assert.equal((panel.match(/animate=\{/g) ?? []).length, 1);
});

// D5-T3:骨架屏与渐进式内容加载 ————————————————————————
test("the streaming bubble shows a skeleton before the first stream event", async () => {
  const { ConversationContent } = await loadConversationPanel();

  // isStreaming=true 且 streamingMessage=null(首事件前):气泡存在,
  // 内容区为骨架行,不渲染「正在生成…」LoaderCircle 文案
  const markup = renderToStaticMarkup(
    createElement(ConversationContent, {
      isSending: false,
      isStreaming: true,
      messages: [],
      runError: null,
      streamingAgent: "supervisor",
      streamingMessage: null,
    }),
  );

  assert.match(markup, /data-slot="streaming-message"/);
  assert.match(markup, /data-slot="streaming-skeleton"/);
  assert.doesNotMatch(markup, /data-slot="message-skeleton"/);
  // D5-T3:首事件前不渲染 LoaderCircle 指示文案「正在生成…」;
  // D5-T5:sr-only 播报行(「助手正在生成回答…」)此时存在
  assert.doesNotMatch(markup, /正在生成…/);
  assert.match(markup, /data-slot="live-status"/);
  assert.match(markup, /助手正在生成回答…/);
});

test("the streaming bubble switches to real content once the first event arrives", async () => {
  const { ConversationContent } = await loadConversationPanel();

  // 首事件后 streamingMessage 有内容:真实气泡渲染内容,骨架行消失
  // (渐进式衔接,无额外切换逻辑)
  const markup = renderToStaticMarkup(
    createElement(ConversationContent, {
      isSending: false,
      isStreaming: true,
      messages: [],
      runError: null,
      streamingAgent: "supervisor",
      streamingMessage: {
        agent: "supervisor",
        content: "首段流式内容",
        role: "assistant",
      },
    }),
  );

  assert.match(markup, /data-slot="streaming-message"/);
  assert.match(markup, /首段流式内容/);
  assert.doesNotMatch(markup, /data-slot="streaming-skeleton"/);
  assert.doesNotMatch(markup, /data-slot="message-skeleton"/);
});

test("sending and streaming placeholders go through the Skeleton component in source", () => {
  const panel = readFileSync(panelPath, "utf8");

  // review 防回归:同步发送态与流式首事件前占位必须走 Skeleton 骨架
  // (不得回归为纯文案占位),两个 data-slot 可区分测试
  assert.match(panel, /data-slot="message-skeleton"/);
  assert.match(panel, /data-slot="streaming-skeleton"/);
  assert.match(panel, /<Skeleton /);
  // D5-T5:唯一允许的「正在生成回答」文案是 aria-live 的 sr-only 播报行
  // (live-status)——骨架占位不得回归为纯文案
  assert.equal((panel.match(/正在生成回答/g) ?? []).length, 1);
  assert.match(panel, /data-slot="live-status"/);
  assert.match(panel, /助手正在生成回答…/);
});

// D5-T5:可访问性——aria-live 消息流区与 sr-only 状态播报 ————————————
test("the message list is an aria-live region and announces streaming state", async () => {
  // SSR 消息流区:aria-live="polite" 落在 data-slot="message-list" 上
  // (ConversationPanel 直接 SSR:chat-store/api-client 无浏览器 API 访问,
  // useVirtualizer 服务端返回空窗口,既有短会话路径全量渲染)
  const { ConversationPanel } = await loadConversationPanel();
  const markup = renderToStaticMarkup(createElement(ConversationPanel));

  assert.match(markup, /aria-live="polite"[^>]*data-slot="message-list"/);
  // 空闲态无播报行
  assert.doesNotMatch(markup, /data-slot="live-status"/);
});

test("streaming and sending states render a sr-only live status line", async () => {
  const { ConversationContent } = await loadConversationPanel();

  const streamingMarkup = renderToStaticMarkup(
    createElement(ConversationContent, {
      isSending: false,
      isStreaming: true,
      messages: [],
      runError: null,
      streamingAgent: "supervisor",
      streamingMessage: null,
    }),
  );
  // 流式:sr-only 状态行 + 播报文本(读屏可感知生成进行中)
  assert.match(streamingMarkup, /class="sr-only"[^>]*data-slot="live-status"/);
  assert.match(streamingMarkup, /助手正在生成回答…/);

  const sendingMarkup = renderToStaticMarkup(
    createElement(ConversationContent, {
      isSending: true,
      isStreaming: false,
      messages: [],
      runError: null,
      streamingAgent: null,
      streamingMessage: null,
    }),
  );
  // 发送态:播报「正在发送…」
  assert.match(sendingMarkup, /data-slot="live-status"/);
  assert.match(sendingMarkup, /正在发送…/);
});

test("skeleton placeholders are hidden from assistive tech in source", () => {
  const panel = readFileSync(panelPath, "utf8");

  // D5-T5:骨架是视觉占位,aria-hidden 避免读屏朗读骨架噪音
  // (进行中状态由 sr-only live-status 播报)。断言与 JSX 属性顺序一致:
  // aria-hidden → className → data-slot 在同一元素内
  assert.match(
    panel,
    /aria-hidden\s*\n\s*className="mt-2 space-y-2"\s*\n\s*data-slot="streaming-skeleton"/,
  );
  assert.match(
    panel,
    /aria-hidden\s*\n\s*className="flex justify-start"\s*\n\s*data-slot="message-skeleton"/,
  );
});

// D6-T2:回答反馈交互挂载 —————————————————————————————————————
test("assistant rows render feedback buttons when wired, user rows never do", async () => {
  const { ConversationContent } = await loadConversationPanel();

  const markup = renderToStaticMarkup(
    createElement(ConversationContent, {
      feedbackSessionId: "session-1",
      isSending: false,
      isStreaming: false,
      messages: [
        { agent: null, content: "用户的问题", role: "user" },
        { agent: "supervisor", content: "助手的回答", role: "assistant" },
      ],
      onFeedback: async () => undefined,
      runError: null,
      streamingAgent: null,
      streamingMessage: null,
    }),
  );

  // 仅 assistant 行渲染反馈按钮(全量路径),user 行没有
  assert.equal((markup.match(/data-slot="feedback-up"/g) ?? []).length, 1);
  assert.equal((markup.match(/data-slot="feedback-down"/g) ?? []).length, 1);
  // 反馈按钮位于 assistant 消息气泡之后(气泡下方)
  assert.ok(markup.indexOf("助手的回答") < markup.indexOf('data-slot="feedback-up"'));
});

test("conversation content renders no feedback buttons when not wired", async () => {
  const { ConversationContent } = await loadConversationPanel();

  const markup = renderToStaticMarkup(
    createElement(ConversationContent, {
      isSending: false,
      isStreaming: false,
      messages: [{ agent: "supervisor", content: "回答", role: "assistant" }],
      runError: null,
      streamingAgent: null,
      streamingMessage: null,
    }),
  );

  // 未接线(无 feedbackSessionId)零渲染——既有调用与测试行为不变
  assert.doesNotMatch(markup, /data-slot="feedback-up"/);
});

test("MessageRow renders feedback buttons only for assistant rows", async () => {
  const { MessageRow } = await loadConversationPanel();

  const assistantMarkup = renderToStaticMarkup(
    createElement(MessageRow, {
      feedbackSessionId: "session-1",
      index: 0,
      message: { agent: "supervisor", content: "回答", role: "assistant" },
      onFeedback: async () => undefined,
    }),
  );
  assert.match(assistantMarkup, /data-slot="feedback-up"/);
  assert.match(assistantMarkup, /data-slot="feedback-down"/);

  const userMarkup = renderToStaticMarkup(
    createElement(MessageRow, {
      feedbackSessionId: "session-1",
      index: 1,
      message: { agent: null, content: "问题", role: "user" },
      onFeedback: async () => undefined,
    }),
  );
  assert.doesNotMatch(userMarkup, /data-slot="feedback-up"/);
});

test("feedback wiring covers both full and virtualized message paths in source", () => {
  const panel = readFileSync(panelPath, "utf8");

  // 全量路径(ConversationContent 内透传)与虚拟化窗口路径(ConversationPanel
  // 直接渲染 MessageRow)都要把反馈参数传给消息行
  assert.match(panel, /feedbackSessionId=\{feedbackSessionId\}/);
  assert.match(panel, /feedbackSessionId=\{currentSessionId \?\? undefined\}/);
  assert.ok((panel.match(/feedbackSessionId=/g) ?? []).length >= 3);
  // MessageRow 内仅 assistant 行渲染 FeedbackButtons(且会话 + 回调
  // 同时提供才渲染)
  assert.match(panel, /!isUser && feedbackSessionId && onFeedback/);
  // 反馈走 store 独立 action(submitFeedback 订阅),与主流程解耦
  assert.match(panel, /useChatStore\(\(state\) => state\.submitFeedback\)/);
});
