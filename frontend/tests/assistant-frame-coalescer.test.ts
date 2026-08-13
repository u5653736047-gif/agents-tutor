// assistant-ui 接入(T12):帧级合并器的纯逻辑测试(注入时钟/定时器)。
// 验收口径(计划 T12):60 events/s 合成流下桥层通知频率 ≤30/s。
import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import test from "node:test";

const hookPath = new URL(
  "../components/assistant-ui/use-throttled-conversion.ts",
  import.meta.url,
);

async function loadCoalescer() {
  assert.ok(existsSync(hookPath), "missing throttled conversion hook");
  const mod = await import("../components/assistant-ui/use-throttled-conversion");
  return mod.createFrameCoalescer;
}

// 可控时钟 + 记录型定时器:不依赖真实时间,测试确定性
function makeHarness(frameMs = 33) {
  let currentTime = 0;
  const timers = new Map<number, { callback: () => void; at: number }>();
  let nextTimerId = 0;
  let flushCount = 0;

  return {
    timers,
    frameMs,
    now: () => currentTime,
    schedule: (callback: () => void, ms: number) => {
      nextTimerId += 1;
      timers.set(nextTimerId, { callback, at: currentTime + ms });
      return nextTimerId;
    },
    cancel: (handle: unknown) => {
      timers.delete(handle as number);
    },
    advance(ms: number) {
      currentTime += ms;
      // 触发所有到期的定时器(按到期时间排序)
      const due = [...timers.entries()]
        .filter(([, timer]) => timer.at <= currentTime)
        .sort((a, b) => a[1].at - b[1].at);
      for (const [id, timer] of due) {
        timers.delete(id);
        timer.callback();
      }
    },
    flush: () => {
      flushCount += 1;
    },
    get flushCount() {
      return flushCount;
    },
  };
}

test("leading edge flushes immediately after an idle period", async () => {
  const createFrameCoalescer = await loadCoalescer();
  const harness = makeHarness();
  const coalescer = createFrameCoalescer(
    harness.flush,
    harness.frameMs,
    harness.now,
    harness.schedule,
    harness.cancel,
  );

  coalescer.notify();
  assert.equal(harness.flushCount, 1);
  // 无尾沿补发
  harness.advance(100);
  assert.equal(harness.flushCount, 1);
  coalescer.dispose();
});

test("in-frame bursts coalesce into one trailing flush with the latest state", async () => {
  const createFrameCoalescer = await loadCoalescer();
  const harness = makeHarness();
  const coalescer = createFrameCoalescer(
    harness.flush,
    harness.frameMs,
    harness.now,
    harness.schedule,
    harness.cancel,
  );

  coalescer.notify(); // t=0 领先沿
  assert.equal(harness.flushCount, 1);
  // 帧内 9 次连击:只补一次尾沿
  for (let index = 0; index < 9; index += 1) {
    harness.advance(2);
    coalescer.notify();
  }
  assert.equal(harness.flushCount, 1);
  harness.advance(33); // 越过帧边界,尾沿到点
  assert.equal(harness.flushCount, 2);
  coalescer.dispose();
});

test("60 events per second stay within the 30 per second budget", async () => {
  const createFrameCoalescer = await loadCoalescer();
  const harness = makeHarness();
  const coalescer = createFrameCoalescer(
    harness.flush,
    harness.frameMs,
    harness.now,
    harness.schedule,
    harness.cancel,
  );

  // 60 events/s = 每 16.6ms 一次,持续 1 秒
  for (let step = 0; step < 60; step += 1) {
    coalescer.notify();
    harness.advance(16.6);
  }
  // 收尾:让最后一个尾沿到点
  harness.advance(50);

  assert.ok(
    harness.flushCount <= 31,
    `expected <=31 flushes (30/s + 1 trailing), got ${harness.flushCount}`,
  );
  assert.ok(harness.flushCount >= 28, "flushes keep up with the stream");
  coalescer.dispose();
});

test("spaced notifications above the frame interval each flush immediately", async () => {
  const createFrameCoalescer = await loadCoalescer();
  const harness = makeHarness();
  const coalescer = createFrameCoalescer(
    harness.flush,
    harness.frameMs,
    harness.now,
    harness.schedule,
    harness.cancel,
  );

  for (let index = 0; index < 5; index += 1) {
    coalescer.notify();
    harness.advance(40);
  }
  assert.equal(harness.flushCount, 5);
  coalescer.dispose();
});

test("dispose cancels a pending trailing flush", async () => {
  const createFrameCoalescer = await loadCoalescer();
  const harness = makeHarness();
  const coalescer = createFrameCoalescer(
    harness.flush,
    harness.frameMs,
    harness.now,
    harness.schedule,
    harness.cancel,
  );

  coalescer.notify();
  coalescer.notify(); // 安排尾沿
  coalescer.dispose();
  harness.advance(100);
  // 尾沿被取消:卸载后不再冲刷(防内存泄漏与过期写入)
  assert.equal(harness.flushCount, 1);
});
