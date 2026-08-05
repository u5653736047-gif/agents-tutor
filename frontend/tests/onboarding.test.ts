import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import test from "node:test";

const onboardingPath = new URL("../lib/onboarding.ts", import.meta.url);
const appShellPath = new URL("../components/app-shell.tsx", import.meta.url);
const helpPath = new URL("../HELP.md", import.meta.url);

async function loadOnboarding() {
  assert.ok(existsSync(onboardingPath), "missing onboarding lib");
  return import("../lib/onboarding");
}

// —— 纯逻辑测试:内存 Storage stub ——

function memoryStorage(initial: Record<string, string> = {}) {
  const values = new Map(Object.entries(initial));
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => {
      values.set(key, value);
    },
  };
}

// D5-T4:seenKeyFromStorage / markSeenInStorage 是接受 Storage 接口注入的
// 纯函数,node 环境(无 window)可直接用内存 stub 覆盖读/写/异常路径。
test("seenKeyFromStorage reads the onboarding seen flag from storage", async () => {
  const { ONBOARDING_SEEN_KEY, seenKeyFromStorage } = await loadOnboarding();

  // 缺省(无标记)= 未看过
  assert.equal(seenKeyFromStorage(memoryStorage()), false);
  // 值为 "1" = 已看过
  assert.equal(
    seenKeyFromStorage(memoryStorage({ [ONBOARDING_SEEN_KEY]: "1" })),
    true,
  );
  // 其它值(含 "0")按未看过处理
  assert.equal(
    seenKeyFromStorage(memoryStorage({ [ONBOARDING_SEEN_KEY]: "0" })),
    false,
  );
});

test("seenKeyFromStorage falls back to false when storage access throws", async () => {
  const { seenKeyFromStorage } = await loadOnboarding();
  const throwing: Pick<Storage, "getItem"> = {
    getItem: () => {
      throw new Error("storage denied");
    },
  };
  assert.equal(seenKeyFromStorage(throwing), false);
});

test("markSeenInStorage writes the seen flag and tolerates write failures", async () => {
  const { ONBOARDING_SEEN_KEY, markSeenInStorage } = await loadOnboarding();

  const storage = memoryStorage();
  markSeenInStorage(storage);
  assert.equal(storage.getItem(ONBOARDING_SEEN_KEY), "1");

  const throwing: Pick<Storage, "setItem"> = {
    setItem: () => {
      throw new Error("quota exceeded");
    },
  };
  // 写入异常静默,不抛错
  assert.doesNotThrow(() => markSeenInStorage(throwing));
});

// —— 组件接线:源码正则守卫 ——

// D5-T4:引导「已看过」必须走 useSyncExternalStore 订阅(localStorage 外部
// 存储;react-hooks lint 拦截 effect 内 setState,mounted 模式不可用),且
// getServerSnapshot 恒 false 与 SSR 首帧一致。
test("the app shell subscribes to onboarding with useSyncExternalStore", () => {
  const source = readFileSync(appShellPath, "utf8");

  assert.match(
    source,
    /useSyncExternalStore\(\s*subscribeOnboarding,\s*isOnboardingSeen,\s*\(\) => false,\s*\)/,
  );
  assert.match(source, /subscribeOnboarding/);
  assert.match(source, /markOnboardingSeen/);
});

// D5-T4:示例问题点击时序——先 await createSession() 再 streamSendMessage;
// createSession 失败返回 null 时跳过发送(见组件注释)。
test("example question clicks create a session before streaming the message", () => {
  const source = readFileSync(appShellPath, "utf8");

  assert.match(source, /const session = await createSession\(\)/);
  assert.match(source, /streamSendMessage\(question\)/);
  assert.match(source, /data-slot="example-question"/);
  assert.match(source, /data-slot="onboarding-skip"/);
});

// D5-T4:帮助文档——存在性 + FAQ 章节计数(## 标题 ≥ 5)。
test("HELP.md exists with at least five FAQ sections", () => {
  assert.ok(existsSync(helpPath), "missing HELP.md");
  const help = readFileSync(helpPath, "utf8");
  const sections = help.match(/^## /gm) ?? [];
  assert.ok(
    sections.length >= 5,
    `expected at least 5 FAQ sections, got ${sections.length}`,
  );
});
