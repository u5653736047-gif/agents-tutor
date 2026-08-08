import type { components } from "@/contracts/api.generated";
import {
  ApiClientError,
  DEFAULT_REQUEST_TIMEOUT_MS,
  type ApiErrorCode,
} from "./api-client";

export type StreamEvent = components["schemas"]["StreamEvent"];

export type StreamEventCallback = (event: StreamEvent) => void;

export interface StreamChatOptions {
  baseUrl: string;
  userId: string;
  sessionId: string;
  message: string;
  // D7-T2:附件引用列表(契约 ChatRequest.attachments)——流式通道与同步
  // 通道同契约,有附件时一并提交;缺省 undefined 不落 body 字段。
  // 类型直接引用生成契约,store→stream 间字段零漂移(review nit)。
  attachments?: components["schemas"]["Attachment"][];
  onEvent: StreamEventCallback;
  signal?: AbortSignal;
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
  // D1-T3:断线重连续传起点——上次成功收到的最新事件 sequence。
  // 服务端据此回放剩余事件 + done,不重复执行整轮;默认 0 表示新消息。
  fromSequence?: number;
}

// D1-T3 重连参数:指数退避 + 重试上限。重试期间携带递增的
// fromSequence(见 streamChatWithRetry),配合服务端回放语义
// (轮次已结束时重连不重复执行,只补发剩余事件)。
export interface StreamRetryOptions {
  maxRetries: number;
  baseDelayMs?: number; // 首次重试等待,默认 1000ms,之后按 2 的幂翻倍
  maxDelayMs?: number; // 退避上限,默认 30000ms
  onRetry?: (attempt: number, error: unknown) => void; // 可选,供 UI 提示
}

// 为什么手写 SSE 解析而不引入依赖:
// 1) SSE 只是「data: <json> 行 + 空行分帧」的纯文本协议,读 response.body
//    的 ReadableStream 按行重组即可,几十行代码能讲清楚,不增加包体积;
// 2) 浏览器 EventSource 只支持 GET 且不能带 X-User-Id 这类自定义请求头,
//    而后端流式接口是 POST + 自定义头,必须走 fetch;
// 3) 因此解码、分帧、坏帧防御都在这里直接控制,行为可测试、可解释。
//
// 坏帧防御:协议约定 data: 行必为合法 JSON,但网络代理或服务端异常可能
// 混入脏字节;单帧解析失败只跳过该帧,绝不中断整个流(已渲染的内容不丢)。

function parseDataLine(line: string): string | null {
  if (!line.startsWith("data:")) {
    // `: keepalive` 注释帧、event:/id:/retry: 等元数据行一律忽略
    return null;
  }
  // 去掉 "data:" 前缀;SSE 规范允许前缀后跟一个空格,一并去掉
  return line.slice("data:".length).replace(/^ /, "");
}

async function readErrorDetail(response: Response): Promise<{
  code: ApiErrorCode | null;
  message: string;
}> {
  // 与 api-client 的 request<T> 错误路径同口径:detail.error_code / detail.message
  try {
    const payload = (await response.json()) as {
      detail?: { error_code?: unknown; message?: unknown };
    };
    const detail = payload?.detail;
    if (
      detail &&
      typeof detail.error_code === "string" &&
      typeof detail.message === "string"
    ) {
      return { code: detail.error_code as ApiErrorCode, message: detail.message };
    }
  } catch {
    // 读不到 JSON 错误体时退回默认文案
  }
  return { code: null, message: "请求失败，请稍后重试。" };
}

export async function streamChat(options: StreamChatOptions): Promise<void> {
  const {
    attachments,
    baseUrl,
    fetchImpl = fetch,
    fromSequence = 0,
    message,
    onEvent,
    sessionId,
    signal,
    timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS,
    userId,
  } = options;

  // 内部超时计时器与调用方 signal 合并:任一触发都中止 fetch 与读取循环
  // (reader.read 在 signal abort 时拒绝)。超时按失败抛错;调用方主动取消
  // (AbortController.abort)则静默返回——D1-T3 的重连依赖后者。
  // 注意:计时器与监听必须覆盖「fetch + 读取」全程,不能在响应头返回后就
  // 清理——否则后端挂起时前端无限挂起(review 修正)。
  const controller = new AbortController();
  let timedOut = false;
  const timeout = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  const abortFromCaller = () => controller.abort();
  signal?.addEventListener("abort", abortFromCaller, { once: true });

  try {
    // ── fetch 阶段:响应头返回前,网络错误 / 超时 / 调用方取消在此分流 ──
    let response: Response;
    try {
      response = await fetchImpl(
        `${baseUrl}/chat/stream?from_sequence=${fromSequence}`,
        {
          body: JSON.stringify({
            message,
            session_id: sessionId,
            // D7-T2:流式通道同契约透传附件(见 StreamChatOptions.attachments);
            // 条件展开:缺省不落字段,与既有请求体逐字节一致(既有测试
            // 零回归)。
            ...(attachments && attachments.length > 0 ? { attachments } : {}),
          }),
          headers: {
            "Content-Type": "application/json",
            "X-User-Id": userId,
          },
          method: "POST",
          signal: controller.signal,
        },
      );
    } catch {
      if (signal?.aborted) {
        // 调用方取消:正常路径,不抛
        return;
      }
      if (timedOut) {
        throw new ApiClientError("请求超时，请稍后重试。", {
          code: null,
          status: null,
        });
      }
      throw new ApiClientError("网络请求失败，请检查服务是否可用。", {
        code: null,
        status: null,
      });
    }

    if (!response.ok) {
      const { code, message } = await readErrorDetail(response);
      throw new ApiClientError(message, { code, status: response.status });
    }

    if (!response.body) {
      throw new ApiClientError("服务响应格式无效。", {
        code: null,
        status: response.status,
      });
    }

    // ── 读取阶段:reader.read 在 signal abort 时拒绝,超时/取消在此仍生效 ──
    const decoder = new TextDecoder();
    const reader = response.body.getReader();
    let buffer = "";
    let dataLines: string[] = [];

    // 一个完整的 SSE 帧由若干 data: 行与结尾空行(\n\n)组成;空行出现时把
    // 累积的 data 行拼起来解析成 StreamEvent 派发。
    const flush = () => {
      if (dataLines.length === 0) {
        return;
      }
      const payload = dataLines.join("\n");
      dataLines = [];
      try {
        onEvent(JSON.parse(payload) as StreamEvent);
      } catch {
        // 坏帧跳过:不中断整个流
      }
    };

    const handleLine = (line: string) => {
      if (line === "") {
        flush();
        return;
      }
      const data = parseDataLine(line);
      if (data !== null) {
        dataLines.push(data);
      }
    };

    for (;;) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      // stream: true 让 TextDecoder 保留跨 chunk 的半截多字节字符
      buffer += decoder.decode(value, { stream: true });
      let newlineIndex = buffer.indexOf("\n");
      while (newlineIndex !== -1) {
        handleLine(buffer.slice(0, newlineIndex));
        buffer = buffer.slice(newlineIndex + 1);
        newlineIndex = buffer.indexOf("\n");
      }
    }
    // 流结束:flush TextDecoder 残留的半截多字节字符,再处理剩余行
    // (最后一段可能没有 \n\n 结尾)
    buffer += decoder.decode();
    if (buffer.length > 0) {
      handleLine(buffer);
    }
    flush();
  } catch (error) {
    // 业务错误(非 2xx / 格式无效)原样透传,不被取消/超时分支改写。
    if (error instanceof ApiClientError) {
      throw error;
    }
    if (signal?.aborted) {
      return;
    }
    if (timedOut) {
      throw new ApiClientError("请求超时，请稍后重试。", {
        code: null,
        status: null,
      });
    }
    throw new ApiClientError("读取流式响应失败，请稍后重试。", {
      code: null,
      status: null,
    });
  } finally {
    // 计时器与监听在此统一清理:覆盖 fetch + 读取全程(review 修正)。
    clearTimeout(timeout);
    signal?.removeEventListener("abort", abortFromCaller);
  }
}

// D1-T3:可中断的退避等待。signal abort 时立即返回(不抛错)——调用方
// 取消后,重试循环继续,下一次 streamChat 会因 signal.aborted 静默返回,
// 与「取消不重试」的语义衔接(取消优先于任何待执行的重试)。
function delay(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal?.aborted) {
      resolve();
      return;
    }
    // onAbort 先定义、timeout 后定义:abort 事件最早在同步代码之后
    // 触发,闭包引用 timeout 时它已初始化(无 TDZ 风险)。
    const onAbort = () => {
      clearTimeout(timeout);
      resolve();
    };
    const timeout = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

// D1-T3:断线重连与消息补发——指数退避重试 + fromSequence 续传。
//
// 语义:
// - 每次尝试都携带「已收到的最新 sequence」(maxSeen,初始为
//   options.fromSequence ?? 0),断线重连时服务端回放剩余事件 + done,
//   不会重复执行整轮;
// - session_busy(会话忙)/ 超时 / 网络错误都重试:原流未结束时后端
//   返回 session_busy,退避等待锁释放后重连,自然进入回放补发;
// - 调用方取消(signal.aborted)不重试,静默返回;
// - 重试耗尽(maxRetries 次重试后仍失败)原样抛出最后一次错误。
export async function streamChatWithRetry(
  options: StreamChatOptions & StreamRetryOptions,
): Promise<void> {
  let maxSeen = options.fromSequence ?? 0;
  let sawDone = false;
  const wrappedOnEvent: StreamEventCallback = (event) => {
    maxSeen = Math.max(maxSeen, event.sequence);
    if (event.event_type === "done") {
      sawDone = true;
    }
    options.onEvent(event);
  };

  for (let attempt = 0; ; attempt += 1) {
    try {
      await streamChat({ ...options, fromSequence: maxSeen, onEvent: wrappedOnEvent });
      // 流关闭但从未收到 done(服务端截断/代理掐断):视为失败,重试
      // 续传——否则消息可能静默丢失(无 message_end 且不重试、不降级,
      // review 修正)。调用方取消(abort)时 streamChat 静默返回,不算失败。
      if (!sawDone && !options.signal?.aborted) {
        throw new ApiClientError("连接中断:未收到完成事件。", {
          code: null,
          status: null,
        });
      }
      return; // 正常收到 done / 流正常结束
    } catch (error) {
      if (options.signal?.aborted) {
        return; // 调用方取消:不重试
      }
      if (sawDone) {
        // 已收到 done 后连接才断:本轮结果已完整交付,重试只会让
        // 服务端重复执行一轮(review nit 修正)。
        return;
      }
      if (attempt >= options.maxRetries) {
        throw error; // 重试耗尽:原样抛出,由调用方决定降级策略
      }
      options.onRetry?.(attempt + 1, error);
      await delay(
        Math.min(
          (options.baseDelayMs ?? 1000) * 2 ** attempt,
          options.maxDelayMs ?? 30000,
        ),
        options.signal,
      );
    }
  }
}
