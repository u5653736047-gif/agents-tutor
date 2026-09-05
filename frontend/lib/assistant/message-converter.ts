// assistant-ui 接入(T3):会话状态切片 → ThreadMessageLike[] 纯函数转换器。
//
// 为什么集中在单个纯函数:全部「SSE 事件契约 → assistant-ui content parts」
// 的业务正确性收敛于此,不接 UI、不依赖 React/store,可用 tsx --test 完备
// 参数化单测(正常 + 脏数据),与 stores/chat-store.ts 的 dispatchStreamEvent
// 语义一一对应(store 零改动红线,本模块是它的只读适配层)。
//
// 映射表(单一事实来源;与 backend/src/api/schemas.py StreamEventType 对齐):
//   历史 messages[]        → 逐条 text part(user 附件 / assistant agent
//                            角色写入 metadata.custom,保留鉴权 Blob 与徽章链路)
//   reasoning              → { type: "reasoning" }(store 已按 message_id 合并,
//                            一个 message_id 一条事件,直接成 part)
//   tool_call              → { type: "tool-call", argsText: input_summary }
//   tool_result            → 按 tool_call_id 回填 result / isError(Map O(1));
//                            孤儿 result(缺 call)降级为独立 part,不丢信息
//   tool_output            → 按 tool_call_id 把增量 content 追加进 result
//   agent_switch           → { type: "data", name: "agent-switch" }
//                            (连续重复角色去重,接力分隔在 T5 渲染)
//   非 supervisor message_delta → { type: "data", name: "subagent-output" }
//                            (Worker 阶段输出卡——与最终回答文本刻意分开:
//                            message_end 的权威全文与之同源,若映射为 text
//                            part 会在收尾时双重渲染;旧 UI 也只在侧栏展示)
//   supervisor 流 / message_end → streamingMessage.content 作为在飞消息的
//                            末尾 text part;references 写入该消息
//                            metadata.custom.citations
//   thinking               → { type: "data", name: "thinking" }(T8:阶段提示行)
//   taskPlan/taskResults   → { type: "data", name: "plan-steps" }(T8:计划步骤
//                            条,置于过程 parts 首位——计划先于执行)
//   approval_required / error / done → 不映射(审批卡片属 T9 的消息外挂载;
//                            error/done 由 store 错误态呈现)
//
// 在飞消息合成规则(避免与历史重复的关键):
//   - isStreaming 或 streamingMessage 非空 → 追加一条在飞 assistant 消息,
//     status: { type: "running" } 仅在仍在流式时标记;
//   - 会话重载后(不流式、无 streamingMessage 但 events 非空、末条历史为
//     assistant)→ 过程 parts 折叠进末条助手消息,思维链在刷新后可恢复
//     (history 的 content 是纯文本,不含过程信息,无重复风险);
//   - 其余情况 events 不产生额外消息(与旧 UI「过程面板随轮次清空」一致)。
//
// 性能设计:
//   - 历史消息按对象引用 WeakMap 缓存(store 的消息对象不可变,乐观追加/
//     权威替换都复用或整体更换引用),转换 O(1) 摊销;
//   - tool_result/tool_output 经 Map<toolCallId, partIndex> 原地回填,
//     全量转换 O(n);事件数组按 run 重置,规模有界;
//   - 引用附加经 (message, references) 二级缓存,references 每轮只写一次,
//     缓存命中率接近 100%。
//
// 宽容读取(对齐后端 message_agent_role / message_references 哲学):缺
// message_id / tool_call_id、空 content、未知 event_type 一律降级跳过,
// 绝不抛异常击穿渲染。

import type { ThreadMessageLike } from "@assistant-ui/react";

import type { components } from "../../contracts/api.generated";

type Message = components["schemas"]["Message"];
type RunEvent = components["schemas"]["RunEvent"];
type StreamEvent = components["schemas"]["StreamEvent"];
type Citation = components["schemas"]["Citation"];
type AgentRole = components["schemas"]["AgentRole"];
type TaskPlan = components["schemas"]["TaskPlan"];
type TaskResult = components["schemas"]["TaskResult"];
type ConversationEvent = RunEvent | StreamEvent;

/** 转换器的输入:chat-store 会话状态的最小只读切片。 */
export type ConversationSlice = {
  messages: Message[];
  events: ConversationEvent[];
  streamingMessage: Message | null;
  streamingAgent: AgentRole | null;
  isStreaming: boolean;
  references: Citation[] | null;
  // T8:当前轮的任务计划与步骤结果(store 的 taskPlan/taskResults,
  // 过程快照恢复时随 getSessionProcess 一并回填)
  taskPlan: TaskPlan | null;
  taskResults: TaskResult[] | null;
};

/** metadata.custom 的键——集中定义,渲染层(T4+)按此读取。 */
export const CUSTOM_METADATA_KEYS = {
  agent: "agent",
  attachments: "attachments",
  citations: "citations",
  // 反馈提交的 message_id 契约值:后端按消息的 created_at 原文匹配,
  // 必须原样透传(Date 序列化会丢微秒位,见 T7 转换处注释)
  messageId: "messageId",
} as const;

/**
 * 转换器输出的消息类型:ThreadMessageLike + convertConfig。
 * convertConfig 不属于 ThreadMessageLike 本体,是 useExternalMessageConverter
 * 认读的扩展字段——joinStrategy "none" 禁止 runtime 把多智能体连续助手
 * 消息按默认 concat-content 策略合并成一条(agent 边界即消息边界)。
 * T4 的 runtime 适配层以恒等 convertMessage 原样透传本类型。
 */
export type ConvertedMessage = ThreadMessageLike & {
  readonly convertConfig?: { readonly joinStrategy?: "concat-content" | "none" };
};

type AssistantPart = Exclude<
  ThreadMessageLike["content"],
  string
>[number];

/**
 * providerMetadata 的应用命名空间(T5):reasoning part 无自定义字段,
 * 角色等应用级元数据挂在该命名空间键下({ "agents-tutor": { agent } })。
 */
export const PROVIDER_METADATA_NS = "agents-tutor";

// —— 历史消息转换(WeakMap 引用缓存) ——

const historyCache = new WeakMap<Message, ConvertedMessage>();

function messageIdFor(message: Message, index: number): string {
  // created_at 是权威历史消息的稳定标识;乐观/流式消息缺省时退回位置 id
  // (与 conversation-panel 的 key 逻辑同源:created_at ?? role-index)
  return message.created_at ?? `m-${message.role}-${index}`;
}

function convertHistoryMessage(
  message: Message,
  index: number,
): ConvertedMessage {
  const cached = historyCache.get(message);
  if (cached) {
    return cached;
  }
  const custom: Record<string, unknown> = {};
  if (message.role === "assistant" && message.agent) {
    custom[CUSTOM_METADATA_KEYS.agent] = message.agent;
  }
  // T5-3:附件透传不再限用户消息——助手消息的 attachments 是 officecli
  // 生成文件的下载回执(后端 Message.attachments 契约),渲染层按角色
  // 选择配色(tone),转换层只做角色无关的透传。
  if (message.attachments && message.attachments.length > 0) {
    custom[CUSTOM_METADATA_KEYS.attachments] = message.attachments;
  }
  // T7:反馈按钮的 messageId——created_at 原样透传(权威历史消息才有;
  // 乐观/流式消息缺省,反馈按钮按旧路径语义不渲染)
  if (message.created_at) {
    custom[CUSTOM_METADATA_KEYS.messageId] = message.created_at;
  }
  const converted: ConvertedMessage = {
    id: messageIdFor(message, index),
    role: message.role,
    content: [{ type: "text", text: message.content }],
    convertConfig: { joinStrategy: "none" },
    metadata: { custom },
  };
  historyCache.set(message, converted);
  return converted;
}

// —— 引用附加((message, references) 二级缓存) ——

const citationCache = new WeakMap<
  Message,
  { refs: readonly Citation[]; out: ConvertedMessage }
>();

function withCitations(
  base: ConvertedMessage,
  cacheKey: Message,
  references: readonly Citation[],
): ConvertedMessage {
  const cached = citationCache.get(cacheKey);
  if (cached && cached.refs === references) {
    return cached.out;
  }
  const previous = base.metadata?.custom ?? {};
  const out: ConvertedMessage = {
    ...base,
    metadata: {
      ...base.metadata,
      custom: { ...previous, [CUSTOM_METADATA_KEYS.citations]: references },
    },
  };
  citationCache.set(cacheKey, { refs: references, out });
  return out;
}

// —— 过程事件 → parts(Map 索引,O(n)) ——

type ProcessParts = {
  parts: AssistantPart[];
  /** 最后一条 agent_switch 的角色(连续重复去重) */
  lastSwitchAgent: AgentRole | null;
};

function appendToolResult(
  parts: AssistantPart[],
  indexByToolCallId: Map<string, number>,
  event: ConversationEvent,
): void {
  const toolCallId = event.tool_call_id ?? null;
  const result = event.output_summary ?? "";
  const isError = event.success === false;
  const index = toolCallId === null ? undefined : indexByToolCallId.get(toolCallId);
  const existing = index === undefined ? undefined : parts[index];
  if (index !== undefined && existing && existing.type === "tool-call") {
    // 原地回填(新建 part 对象替换,保持不可变语义)
    parts[index] = { ...existing, result, isError };
    return;
  }
  // 孤儿 result(缺 call 或 id 不匹配):降级为独立 part,不丢审计信息
  const orphan: AssistantPart = {
    type: "tool-call",
    toolCallId: toolCallId ?? `orphan-${event.sequence}`,
    toolName: event.tool_name ?? "unknown",
    argsText: "",
    result,
    isError,
  };
  if (toolCallId !== null) {
    indexByToolCallId.set(toolCallId, parts.length);
  }
  parts.push(orphan);
}

function appendToolOutput(
  parts: AssistantPart[],
  indexByToolCallId: Map<string, number>,
  event: ConversationEvent,
): void {
  const toolCallId = event.tool_call_id ?? null;
  const chunk = event.content ?? "";
  if (!chunk) {
    return;
  }
  const index = toolCallId === null ? undefined : indexByToolCallId.get(toolCallId);
  const existing = index === undefined ? undefined : parts[index];
  if (index !== undefined && existing && existing.type === "tool-call") {
    const previous = typeof existing.result === "string" ? existing.result : "";
    parts[index] = { ...existing, result: `${previous}${chunk}` };
  }
  // 找不到对应 call 的增量输出直接丢弃(审批前输出不可展示,语义与
  // 旧 CollaborationPanel 一致:tool_output 只依附已有工具行)
}

function eventsToProcessParts(
  events: ConversationEvent[],
  taskPlan: TaskPlan | null,
  taskResults: TaskResult[] | null,
): AssistantPart[] {
  const state: ProcessParts = { parts: [], lastSwitchAgent: null };
  const indexByToolCallId = new Map<string, number>();

  // T8:计划步骤条置于过程 parts 首位(计划先于执行);taskPlan 缺失时
  // 不产生 part(直接回答轮次无计划,与旧面板空态语义一致)
  if (taskPlan) {
    state.parts.push({
      type: "data",
      name: "plan-steps",
      data: { plan: taskPlan, results: taskResults },
    });
  }

  for (const event of events) {
    switch (event.event_type) {
      case "thinking": {
        // T8:阶段提示行。live 事件由后端流式层填充文案(stream.py 的
        // _AGENT_PROGRESS_SUMMARIES 兑底);持久化快照(RunEvent)的
        // content 为 null(阶段文案是流式期生成物)——与旧面板同一
        // 兑底「正在思考…」(collaboration-panel EventTimeline 语义)。
        const raw = event.content ?? "";
        const content = raw.trim() ? raw : "正在思考…";
        state.parts.push({
          type: "data",
          name: "thinking",
          data: { agent: event.agent ?? null, content },
        });
        break;
      }
      case "reasoning": {
        const text = event.content ?? "";
        if (!text.trim()) {
          break;
        }
        // T5:角色随 part 透传(providerMetadata 是命名的扩展位,
        // reasoning part 契约无自定义字段)——多智能体交错时思维链块
        // 头部可展示产出角色徽章
        state.parts.push({
          type: "reasoning",
          text,
          ...(event.agent
            ? { providerMetadata: { [PROVIDER_METADATA_NS]: { agent: event.agent } } }
            : {}),
        });
        break;
      }
      case "tool_call": {
        const toolCallId = event.tool_call_id ?? `call-${event.sequence}`;
        state.parts.push({
          type: "tool-call",
          toolCallId,
          toolName: event.tool_name ?? "unknown",
          argsText: event.input_summary ?? "",
        });
        indexByToolCallId.set(toolCallId, state.parts.length - 1);
        break;
      }
      case "tool_result":
        appendToolResult(state.parts, indexByToolCallId, event);
        break;
      case "tool_output":
        appendToolOutput(state.parts, indexByToolCallId, event);
        break;
      case "agent_switch": {
        const agent = event.agent ?? null;
        if (agent === null || agent === state.lastSwitchAgent) {
          break;
        }
        state.lastSwitchAgent = agent;
        state.parts.push({
          type: "data",
          name: "agent-switch",
          data: { agent },
        });
        break;
      }
      case "message_delta": {
        // supervisor 的增量走 streamingMessage 主路径(store 语义),这里只
        // 收 Worker 的阶段输出卡
        if (event.agent === "supervisor") {
          break;
        }
        const text = event.content ?? "";
        if (!text.trim()) {
          break;
        }
        state.parts.push({
          type: "data",
          name: "subagent-output",
          data: { agent: event.agent ?? null, content: text },
        });
        break;
      }
      default:
        // approval_required / message_end / error / done / 未知类型不映射
        // (见文件头映射表注释),宽容跳过
        break;
    }
  }
  return state.parts;
}

// —— 在飞消息合成 ——

function buildLiveMessage(
  slice: ConversationSlice,
  processParts: AssistantPart[],
): ConvertedMessage | null {
  const { isStreaming, streamingMessage, streamingAgent, references } = slice;
  if (!isStreaming && streamingMessage === null) {
    return null;
  }
  const parts: AssistantPart[] = [...processParts];
  const finalText = streamingMessage?.content ?? "";
  if (finalText.trim()) {
    parts.push({ type: "text", text: finalText });
  }
  const custom: Record<string, unknown> = {};
  const agent = streamingMessage?.agent ?? streamingAgent;
  if (agent) {
    custom[CUSTOM_METADATA_KEYS.agent] = agent;
  }
  if (references && references.length > 0) {
    custom[CUSTOM_METADATA_KEYS.citations] = references;
  }
  // T5-3:message_end 权威消息的生成文件附件——流式完成到权威历史替换
  // 之间的窗口同样渲染下载入口(历史替换后由 convertHistoryMessage 透传)
  const liveAttachments = streamingMessage?.attachments;
  if (liveAttachments && liveAttachments.length > 0) {
    custom[CUSTOM_METADATA_KEYS.attachments] = liveAttachments;
  }
  // T7:与历史消息同一语义——message_end 的权威消息携带 created_at 时透传
  if (streamingMessage?.created_at) {
    custom[CUSTOM_METADATA_KEYS.messageId] = streamingMessage.created_at;
  }
  return {
    id: streamingMessage?.created_at ?? "live-message",
    role: "assistant",
    content: parts,
    status: isStreaming ? { type: "running" } : undefined,
    convertConfig: { joinStrategy: "none" },
    metadata: { custom },
  };
}

// —— 主入口 ——

/**
 * 把 chat-store 会话切片转换为 assistant-ui 的消息数组。
 * 纯函数:相同输入(按引用)产生结构等价输出;历史消息经 WeakMap 缓存
 * 保持引用相等,下游 React.memo 可跳过重渲染。
 */
export function convertConversationToThreadMessages(
  slice: ConversationSlice,
): ConvertedMessage[] {
  const output: ConvertedMessage[] = slice.messages.map((message, index) =>
    convertHistoryMessage(message, index),
  );

  const processParts = eventsToProcessParts(
    slice.events,
    slice.taskPlan,
    slice.taskResults,
  );
  const liveMessage = buildLiveMessage(slice, processParts);
  if (liveMessage) {
    output.push(liveMessage);
    return output;
  }

  // 会话重载后的过程恢复:不流式、无在飞消息,但本轮事件仍在(末条历史
  // 为 assistant 时才折叠——过程属于那条回答)
  const lastIndex = slice.messages.length - 1;
  const lastMessage = lastIndex >= 0 ? slice.messages[lastIndex] : undefined;
  if (
    processParts.length > 0 &&
    lastMessage?.role === "assistant" &&
    output.length > 0
  ) {
    const lastConverted = output[output.length - 1];
    if (!lastConverted) {
      return output;
    }
    const lastContent = Array.isArray(lastConverted.content)
      ? lastConverted.content
      : [];
    let restored: ConvertedMessage = {
      ...lastConverted,
      content: [...processParts, ...lastContent],
    };
    output[output.length - 1] = restored;
    // 引用随轮次保留在 store.references:折叠恢复时一并把引用挂回末条
    // 助手消息,刷新后 CitationList 仍可渲染
    if (slice.references && slice.references.length > 0) {
      restored = withCitations(restored, lastMessage, slice.references);
      output[output.length - 1] = restored;
    }
    return output;
  }

  // 常规收尾:引用挂到最后一条助手消息(message_end 后、历史替换前的
  // 窗口,以及历史替换完成后的稳定态,两种时序都覆盖)
  if (slice.references && slice.references.length > 0 && lastMessage) {
    const lastConverted = output[output.length - 1];
    if (lastConverted?.role === "assistant") {
      output[output.length - 1] = withCitations(
        lastConverted,
        lastMessage,
        slice.references,
      );
    }
  }
  return output;
}
