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
  onEvent: StreamEventCallback;
  signal?: AbortSignal;
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
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
    baseUrl,
    fetchImpl = fetch,
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
      response = await fetchImpl(`${baseUrl}/chat/stream`, {
        body: JSON.stringify({ message, session_id: sessionId }),
        headers: {
          "Content-Type": "application/json",
          "X-User-Id": userId,
        },
        method: "POST",
        signal: controller.signal,
      });
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
