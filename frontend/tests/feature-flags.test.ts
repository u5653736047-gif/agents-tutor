import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import test from "node:test";

const featureFlagsPath = new URL("../lib/feature-flags.ts", import.meta.url);

async function loadFeatureFlags() {
  assert.ok(existsSync(featureFlagsPath), "missing feature-flags lib");
  return import("../lib/feature-flags");
}

// —— 纯逻辑测试:内存 Storage stub(与 onboarding.test.ts 同一风格) ——

function memoryStorage(initial: Record<string, string> = {}) {
  const values = new Map(Object.entries(initial));
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => {
      values.set(key, value);
    },
  };
}

// assistant-ui 接入:assistantUiFlagFromStorage 读取三层语义——"1"=开、
// "0"=关(显式覆盖,即使 env 默认开也要能关)、缺省/脏值=null(回落 env)。
test("assistantUiFlagFromStorage reads explicit overrides and tolerates dirt", async () => {
  const { ASSISTANT_UI_FLAG_KEY, assistantUiFlagFromStorage } =
    await loadFeatureFlags();

  // 缺省(无覆盖)= null,由调用方回落 env 默认
  assert.equal(assistantUiFlagFromStorage(memoryStorage()), null);
  // 显式开 / 显式关
  assert.equal(
    assistantUiFlagFromStorage(memoryStorage({ [ASSISTANT_UI_FLAG_KEY]: "1" })),
    true,
  );
  assert.equal(
    assistantUiFlagFromStorage(memoryStorage({ [ASSISTANT_UI_FLAG_KEY]: "0" })),
    false,
  );
  // 脏值(历史脏数据/未来值)按无覆盖处理
  assert.equal(
    assistantUiFlagFromStorage(
      memoryStorage({ [ASSISTANT_UI_FLAG_KEY]: "yes" }),
    ),
    null,
  );
});

test("assistantUiFlagFromStorage falls back to null when storage access throws", async () => {
  const { assistantUiFlagFromStorage } = await loadFeatureFlags();
  const throwing: Pick<Storage, "getItem"> = {
    getItem: () => {
      throw new Error("storage denied");
    },
  };
  assert.equal(assistantUiFlagFromStorage(throwing), null);
});

test("writeAssistantUiFlag writes the override and tolerates write failures", async () => {
  const { ASSISTANT_UI_FLAG_KEY, writeAssistantUiFlag } =
    await loadFeatureFlags();

  const storage = memoryStorage();
  writeAssistantUiFlag(storage, true);
  assert.equal(storage.getItem(ASSISTANT_UI_FLAG_KEY), "1");
  writeAssistantUiFlag(storage, false);
  assert.equal(storage.getItem(ASSISTANT_UI_FLAG_KEY), "0");

  const throwing: Pick<Storage, "setItem"> = {
    setItem: () => {
      throw new Error("quota exceeded");
    },
  };
  assert.doesNotThrow(() => writeAssistantUiFlag(throwing, true));
});

// SSR 语义:node 环境(无 window)下 isAssistantUiEnabled 恒为 env 默认
// (测试环境未设 NEXT_PUBLIC_ASSISTANT_UI,默认关),与 getServerSnapshot
// 一致——SSR 首帧与客户端首帧相同,无 hydration mismatch。
test("isAssistantUiEnabled falls back to the env default without window", async () => {
  const { ASSISTANT_UI_ENV_DEFAULT, isAssistantUiEnabled } =
    await loadFeatureFlags();

  assert.equal(ASSISTANT_UI_ENV_DEFAULT, false);
  assert.equal(isAssistantUiEnabled(), ASSISTANT_UI_ENV_DEFAULT);
});

// 无 window 时 setter/subscriber 安全 no-op(node 单测环境直接调用不抛错)。
test("window-bound helpers no-op safely without window", async () => {
  const { setAssistantUiEnabled, subscribeAssistantUiFlag } =
    await loadFeatureFlags();

  assert.doesNotThrow(() => setAssistantUiEnabled(true));
  const unsubscribe = subscribeAssistantUiFlag(() => {});
  assert.equal(typeof unsubscribe, "function");
  assert.doesNotThrow(() => unsubscribe());
});
