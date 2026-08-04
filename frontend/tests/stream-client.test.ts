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
