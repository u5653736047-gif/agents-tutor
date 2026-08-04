import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import test from "node:test";

const streamClientPath = new URL("../lib/stream-client.ts", import.meta.url);
const apiClientPath = new URL("../lib/api-client.ts", import.meta.url);

async function loadStreamClient() {
  assert.ok(existsSync(streamClientPath), "missing SSE stream client");
  return import("../lib/stream-client");
}

async function loadApiClient() {
  assert.ok(existsSync(apiClientPath), "missing generated-type API client");
  return import("../lib/api-client");
}

// 按给定字节片段逐块投递的 SSE 响应,模拟网络分片
function sseResponse(chunks: string[]): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(encoder.encode(chunk));
      }
      controller.close();
    },
  });
  return new Response(body, {
    headers: { "Content-Type": "text/event-stream" },
    status: 200,
  });
}

const frame = (payload: string) => `data: ${payload}\n\n`;

function streamOptions(fetchImpl: typeof fetch) {
  return {
    baseUrl: "https://api.example",
    fetchImpl,
    message: "你好",
    onEvent: () => {},
    sessionId: "session-1",
    userId: "demo-user",
  };
}

test("a single SSE frame delivers one StreamEvent", async () => {
  const { streamChat } = await loadStreamClient();
  const received: Array<{ event_type: string; sequence: number }> = [];
  const payload = JSON.stringify({
    agent: "supervisor",
    event_type: "thinking",
    sequence: 1,
    session_id: "session-1",
  });

  await streamChat({
    ...streamOptions(async () => sseResponse([frame(payload)])),
    onEvent: (event) => received.push(event),
  });

  assert.equal(received.length, 1);
  assert.equal(received[0]?.event_type, "thinking");
  assert.equal(received[0]?.sequence, 1);
});

test("multiple frames are delivered and keepalive comment frames are skipped", async () => {
  const { streamChat } = await loadStreamClient();
  const received: Array<{ event_type: string }> = [];
  const body =
    frame(
      JSON.stringify({
        agent: "supervisor",
        event_type: "thinking",
        sequence: 1,
        session_id: "session-1",
      }),
    ) +
    ": keepalive\n\n" +
    frame(
      JSON.stringify({
        event_type: "done",
        sequence: 2,
        session_id: "session-1",
      }),
    );

  await streamChat({
    ...streamOptions(async () => sseResponse([body])),
    onEvent: (event) => received.push(event),
  });

  assert.deepEqual(
    received.map((event) => event.event_type),
    ["thinking", "done"],
  );
});

test("frames split across chunks are still parsed", async () => {
  const { streamChat } = await loadStreamClient();
  const received: Array<{ event_type: string }> = [];
  const body =
    frame(
      JSON.stringify({
        agent: "supervisor",
        event_type: "thinking",
        sequence: 1,
        session_id: "session-1",
      }),
    ) +
    frame(
      JSON.stringify({
        event_type: "done",
        sequence: 2,
        session_id: "session-1",
      }),
    );
  // 把第一个 \n\n 帧边界拆到两个 chunk 中间,模拟网络分片
  const cut = body.indexOf("\n\n") + 1;

  await streamChat({
    ...streamOptions(async () => sseResponse([body.slice(0, cut), body.slice(cut)])),
    onEvent: (event) => received.push(event),
  });

  assert.deepEqual(
    received.map((event) => event.event_type),
    ["thinking", "done"],
  );
});

test("a non-2xx response rejects with the API error shape", async () => {
  const { streamChat } = await loadStreamClient();
  const { ApiClientError } = await loadApiClient();

  await assert.rejects(
    streamChat({
      ...streamOptions(
        async () =>
          new Response(
            JSON.stringify({
              detail: { error_code: "session_not_found", message: "会话不存在。" },
            }),
            { headers: { "Content-Type": "application/json" }, status: 404 },
          ),
      ),
      onEvent: () => {
        assert.fail("no event should be delivered for an error response");
      },
    }),
    (error: unknown) => {
      assert.ok(error instanceof ApiClientError);
      assert.equal(error.status, 404);
      assert.equal(error.code, "session_not_found");
      assert.equal(error.message, "会话不存在。");
      return true;
    },
  );
});

test("a response without a body rejects with the API error shape", async () => {
  const { streamChat } = await loadStreamClient();
  const { ApiClientError } = await loadApiClient();

  await assert.rejects(
    streamChat({
      ...streamOptions(async () => new Response(null, { status: 200 })),
      onEvent: () => {
        assert.fail("no event should be delivered without a body");
      },
    }),
    (error: unknown) => {
      assert.ok(error instanceof ApiClientError);
      assert.equal(error.status, 200);
      return true;
    },
  );
});

test("a malformed data frame is skipped without killing the stream", async () => {
  const { streamChat } = await loadStreamClient();
  const received: Array<{ event_type: string }> = [];
  const body =
    "data: not-json\n\n" +
    frame(
      JSON.stringify({
        event_type: "done",
        sequence: 1,
        session_id: "session-1",
      }),
    );

  await streamChat({
    ...streamOptions(async () => sseResponse([body])),
    onEvent: (event) => received.push(event),
  });

  assert.deepEqual(
    received.map((event) => event.event_type),
    ["done"],
  );
});

// ── 读取阶段超时 / 取消(review 修正的回归测试)────────────────────
//
// 说明:手动构造的 Response 不会因 signal abort 自动中断 body,因此
// fetchImpl 需要监听 init.signal 并主动 error 流,模拟真实 fetch 的
// abort 传播——否则「后端挂起」无法在读取阶段被中断。

function stalledResponseWithAbortPropagation(): typeof fetch {
  return (_url: string | URL | Request, init?: RequestInit) =>
    new Promise<Response>((resolve) => {
      const body = new ReadableStream<Uint8Array>({
        start(controller) {
          init?.signal?.addEventListener("abort", () => {
            controller.error(new DOMException("aborted", "AbortError"));
          });
        },
      });
      resolve(
        new Response(body, {
          headers: { "Content-Type": "text/event-stream" },
          status: 200,
        }),
      );
    });
}

test("a stalled stream rejects with a timeout error while reading", async () => {
  const { streamChat } = await loadStreamClient();
  const { ApiClientError } = await loadApiClient();

  await assert.rejects(
    streamChat({
      ...streamOptions(stalledResponseWithAbortPropagation()),
      onEvent: () => {
        assert.fail("no event should be delivered for a stalled stream");
      },
      timeoutMs: 50,
    }),
    (error: unknown) => {
      assert.ok(error instanceof ApiClientError);
      assert.match(error.message, /超时/);
      return true;
    },
  );
});

test("a caller abort during the read phase resolves silently", async () => {
  const { streamChat } = await loadStreamClient();
  const caller = new AbortController();

  // 挂起流 + 短超时:若取消不生效,50ms 后会被超时错误拒绝。
  const done = streamChat({
    ...streamOptions(stalledResponseWithAbortPropagation()),
    onEvent: () => {
      assert.fail("no event should be delivered after an abort");
    },
    signal: caller.signal,
    timeoutMs: 50,
  });
  setTimeout(() => caller.abort(), 10);

  await done; // resolve(不抛)即通过
});

test("a caller abort wins over an imminent timeout", async () => {
  const { streamChat } = await loadStreamClient();
  const caller = new AbortController();

  // 取消先于超时到期:静默返回而不是抛超时错误。
  const done = streamChat({
    ...streamOptions(stalledResponseWithAbortPropagation()),
    onEvent: () => {},
    signal: caller.signal,
    timeoutMs: 30,
  });
  setTimeout(() => caller.abort(), 5);

  await done;
});

test("streamChatWithRetry retries when the stream closes without a done frame", async () => {
  const { streamChatWithRetry } = await loadStreamClient();
  const received: Array<{ event_type: string }> = [];
  let fetchCalls = 0;
  const fetchImpl: typeof fetch = async () => {
    fetchCalls += 1;
    if (fetchCalls === 1) {
      // 第一次:推送一帧(无 done)后流正常关闭——服务端截断,
      // 消息可能未送达,必须重试续传(review 修正)。
      return sseResponse([
        frame(
          JSON.stringify({
            agent: "supervisor",
            event_type: "thinking",
            sequence: 1,
            session_id: "session-1",
          }),
        ),
      ]);
    }
    // 第二次:补发剩余事件并以 done 收尾。
    return sseResponse([
      frame(
        JSON.stringify({
          content: "完整回答",
          event_type: "message_end",
          sequence: 2,
          session_id: "session-1",
        }),
      ),
      frame(
        JSON.stringify({
          event_type: "done",
          sequence: 3,
          session_id: "session-1",
        }),
      ),
    ]);
  };

  await streamChatWithRetry({
    ...streamOptions(fetchImpl),
    baseDelayMs: 5,
    maxRetries: 2,
    onEvent: (event) => received.push(event),
  });

  assert.equal(fetchCalls, 2);
  assert.deepEqual(
    received.map((event) => event.event_type),
    ["thinking", "message_end", "done"],
  );
});

// ── D1-T3 断线重连与消息补发:streamChatWithRetry ───────────────────

test("streamChatWithRetry retries with an increasing fromSequence after a mid-stream failure", async () => {
  const { streamChatWithRetry } = await loadStreamClient();
  const received: Array<{ event_type: string; sequence: number }> = [];
  const urls: string[] = [];
  let fetchCalls = 0;
  const fetchImpl: typeof fetch = async (input) => {
    fetchCalls += 1;
    urls.push(String(input));
    if (fetchCalls === 1) {
      // 第一次:推送一帧后流中断(controller.error 模拟网络中断),
      // 客户端已收到 seq=1,重试应携带 from_sequence=1 续传。
      // 注意 error 必须异步触发:同步 enqueue + error 会让第一个
      // read() 直接抛错,seq=1 交付不到(重试的 from_sequence 会退
      // 回 0);setTimeout 保证「帧先交付、中断随后」。
      const encoder = new TextEncoder();
      const body = new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(
            encoder.encode(
              frame(
                JSON.stringify({
                  agent: "supervisor",
                  event_type: "thinking",
                  sequence: 1,
                  session_id: "session-1",
                }),
              ),
            ),
          );
          setTimeout(() => controller.error(new Error("network interrupted")), 0);
        },
      });
      return new Response(body, {
        headers: { "Content-Type": "text/event-stream" },
        status: 200,
      });
    }
    // 第二次:正常返回剩余事件(sequence 更大)并收尾。
    return sseResponse([
      frame(
        JSON.stringify({
          agent: "supervisor",
          event_type: "tool_call",
          sequence: 2,
          session_id: "session-1",
          tool_name: "web_search",
        }),
      ),
      frame(
        JSON.stringify({
          event_type: "done",
          sequence: 3,
          session_id: "session-1",
        }),
      ),
    ]);
  };

  await streamChatWithRetry({
    ...streamOptions(fetchImpl),
    baseDelayMs: 5,
    maxRetries: 2,
    onEvent: (event) => received.push(event),
  });

  // 重试请求携带递增的 from_sequence(首次 0,续传 1)。
  assert.equal(fetchCalls, 2);
  assert.ok(urls[0]?.includes("from_sequence=0"), `unexpected first url: ${urls[0]}`);
  assert.ok(urls[1]?.includes("from_sequence=1"), `unexpected retry url: ${urls[1]}`);
  // 首次收到的 seq=1 与重试后的 seq=2/3 全部交付,不丢事件。
  assert.deepEqual(
    received.map((event) => event.sequence),
    [1, 2, 3],
  );
});

test("streamChatWithRetry gives up after maxRetries and rethrows", async () => {
  const { streamChatWithRetry } = await loadStreamClient();
  const { ApiClientError } = await loadApiClient();
  let fetchCalls = 0;
  const fetchImpl: typeof fetch = async () => {
    fetchCalls += 1;
    // 每次都在读取阶段中断(流 error),模拟持续的网络故障。
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.error(new Error("network down"));
      },
    });
    return new Response(body, {
      headers: { "Content-Type": "text/event-stream" },
      status: 200,
    });
  };

  await assert.rejects(
    streamChatWithRetry({
      ...streamOptions(fetchImpl),
      baseDelayMs: 5,
      maxRetries: 2,
      onEvent: () => {
        assert.fail("no event should be delivered for a failing stream");
      },
    }),
    (error: unknown) => {
      assert.ok(error instanceof ApiClientError);
      assert.match(error.message, /读取流式响应失败/);
      return true;
    },
  );
  // 初次尝试 + 2 次重试 = 3 次调用。
  assert.equal(fetchCalls, 3);
});

test("streamChatWithRetry does not retry after a caller abort", async () => {
  const { streamChatWithRetry } = await loadStreamClient();
  const caller = new AbortController();
  let fetchCalls = 0;
  const fetchImpl: typeof fetch = (_url, init) => {
    fetchCalls += 1;
    return new Promise<Response>((resolve) => {
      const body = new ReadableStream<Uint8Array>({
        start(controller) {
          init?.signal?.addEventListener("abort", () => {
            controller.error(new DOMException("aborted", "AbortError"));
          });
        },
      });
      resolve(
        new Response(body, {
          headers: { "Content-Type": "text/event-stream" },
          status: 200,
        }),
      );
    });
  };

  const done = streamChatWithRetry({
    ...streamOptions(fetchImpl),
    baseDelayMs: 5,
    maxRetries: 2,
    onEvent: () => {},
    signal: caller.signal,
  });
  setTimeout(() => caller.abort(), 10);

  await done; // 取消是正常路径:resolve 且不重试
  assert.equal(fetchCalls, 1);
});
