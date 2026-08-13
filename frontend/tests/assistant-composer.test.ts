// assistant-ui 接入(T14):附件适配器与原生 Composer 的测试。
// 适配器是纯逻辑(上传函数注入),直接单测;Composer 组件走源码正则
// wiring 校验(与审批卡片测试同一策略)。
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import test from "node:test";

const adapterPath = new URL(
  "../components/assistant-ui/attachment-adapter.ts",
  import.meta.url,
);
const composerPath = new URL(
  "../components/assistant-ui/composer-native.tsx",
  import.meta.url,
);
const providerPath = new URL(
  "../components/assistant-ui/runtime-provider.tsx",
  import.meta.url,
);

async function loadAdapter() {
  assert.ok(existsSync(adapterPath), "missing attachment adapter");
  return import("../components/assistant-ui/attachment-adapter");
}

// —— 附件适配器(纯逻辑,上传函数注入) ——

test("add defers upload to send time (composer-send semantics)", async () => {
  const { createAttachmentAdapter } = await loadAdapter();
  let uploads = 0;
  const adapter = createAttachmentAdapter(async () => {
    uploads += 1;
    return { content_type: "text/plain", file_id: "f-1", name: "a.txt", size: 3 };
  });

  const pending = await adapter.add({
    file: new File(["abc"], "a.txt", { type: "text/plain" }),
  });
  // 登记即待定(requires-action/composer-send),不产生上传
  assert.equal(uploads, 0);
  assert.deepEqual(pending.status, {
    type: "requires-action",
    reason: "composer-send",
  });
  assert.equal(pending.name, "a.txt");
});

test("send uploads and packs the receipt as a data part", async () => {
  const { createAttachmentAdapter, ATTACHMENT_DATA_PART } = await loadAdapter();
  const adapter = createAttachmentAdapter(async () => ({
    content_type: "text/plain",
    file_id: "f-9",
    name: "notes.txt",
    size: 42,
  }));

  const pending = await adapter.add({
    file: new File(["x"], "notes.txt", { type: "text/plain" }),
  });
  const complete = await adapter.send(pending);

  assert.equal(complete.status.type, "complete");
  const part = complete.content[0];
  assert.equal(part?.type, "data");
  assert.equal(part && "name" in part ? part.name : null, ATTACHMENT_DATA_PART);
  assert.deepEqual(
    part && "data" in part ? part.data : null,
    { content_type: "text/plain", file_id: "f-9", name: "notes.txt", size: 42 },
  );
});

test("attachmentFromPart round-trips and tolerates dirty data", async () => {
  const { attachmentFromPart, createAttachmentAdapter } = await loadAdapter();
  const adapter = createAttachmentAdapter(async () => ({
    content_type: null,
    file_id: "f-1",
    name: "a.txt",
    size: 1,
  }));
  const pending = await adapter.add({
    file: new File(["x"], "a.txt", { type: "text/plain" }),
  });
  const complete = await adapter.send(pending);

  const restored = attachmentFromPart(complete);
  assert.deepEqual(restored, {
    content_type: null,
    file_id: "f-1",
    name: "a.txt",
    size: 1,
  });

  // 脏数据:缺 data part / 缺字段 → null(宽容读取)
  assert.equal(
    attachmentFromPart({ ...complete, content: [] }),
    null,
  );
  assert.equal(
    attachmentFromPart({
      ...complete,
      content: [{ type: "data", name: "attachment-ref", data: { name: "x" } }],
    }),
    null,
  );
});

// —— ComposerNative / runtime-provider wiring(源码正则) ——

test("composer native mirrors the chat input behavior matrix", () => {
  const source = readFileSync(composerPath, "utf8");

  // Enter 提交 / IME 由库内置守卫;输入区语义锚点与旧输入区一致
  assert.match(source, /submitMode="enter"/);
  assert.match(source, /aria-label="输入消息"/);
  assert.match(source, /data-slot="chat-input"/);
  assert.match(source, /data-slot="stop-generating"/);
  // 发送闸门四件套与 ChatInput 的 isBlocked 逐项一致
  for (const field of [
    "isSending",
    "isStreaming",
    "isDecidingToolApproval",
    "pendingToolApproval",
  ]) {
    assert.match(source, new RegExp(`state\\.${field}`), `missing ${field}`);
  }
  // 附件上限与停止语义
  assert.match(source, /MAX_ATTACHMENTS/);
  assert.match(source, /cancelStreaming/);
  // slash 命令复用同一 lib 与状态机
  assert.match(source, /filterCommands/);
  assert.match(source, /applyCommand/);
  assert.match(source, /isSlashCandidate/);
});

test("runtime provider restores attachments in onNew and registers the adapter", () => {
  const source = readFileSync(providerPath, "utf8");

  assert.match(source, /attachmentFromPart/);
  assert.match(source, /createAttachmentAdapter/);
  assert.match(source, /adapters: \{ attachments: attachmentAdapter \}/);
  // 附件随流式主通道提交(契约 Attachment[] 透传)
  assert.match(source, /streamSendMessage\(\s*text,/);
});

test("composer sub-flag defaults off and honors overrides", async () => {
  const flags = await import("../lib/feature-flags");

  assert.equal(flags.ASSISTANT_COMPOSER_ENV_DEFAULT, false);
  // 无 window(node 环境)恒为 env 默认
  assert.equal(flags.isAssistantComposerEnabled(), false);
  assert.doesNotThrow(() => flags.setAssistantComposerEnabled(true));
  const storage = new Map<string, string>();
  const stub = {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => {
      storage.set(key, value);
    },
  };
  assert.equal(flags.assistantComposerFlagFromStorage(stub), null);
  flags.writeAssistantComposerFlag(stub, true);
  assert.equal(flags.assistantComposerFlagFromStorage(stub), true);
  flags.writeAssistantComposerFlag(stub, false);
  assert.equal(flags.assistantComposerFlagFromStorage(stub), false);
});
