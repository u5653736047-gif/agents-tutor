import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import test from "node:test";

const slashCommandsPath = new URL("../lib/slash-commands.ts", import.meta.url);

async function loadSlashCommands() {
  assert.ok(existsSync(slashCommandsPath), "missing slash commands module");
  return import("../lib/slash-commands");
}

test("SLASH_COMMANDS registers explain/quiz/path with Chinese descriptions", async () => {
  const { SLASH_COMMANDS } = await loadSlashCommands();

  assert.deepEqual(
    SLASH_COMMANDS.map((command) => command.name),
    ["explain", "quiz", "path"],
  );
  // 每项都带中文描述与以自身指令名开头的示例(候选列表直接展示)
  for (const command of SLASH_COMMANDS) {
    assert.ok(command.description.length > 0, `${command.name} missing description`);
    assert.ok(command.example.startsWith(`/${command.name} `), `${command.name} example malformed`);
  }
});

test("isSlashCandidate only matches a slash followed by letters with no space yet", async () => {
  const { isSlashCandidate } = await loadSlashCommands();

  // 合法候选态:/ 后紧跟字母且尚无空格
  assert.equal(isSlashCandidate("/quiz"), true);
  assert.equal(isSlashCandidate("/q"), true);
  // 已输入空格(指令边界确定)后候选关闭
  assert.equal(isSlashCandidate("/quiz 卷积神经网络"), false);
  // 仅斜杠(未紧跟字母)不算候选
  assert.equal(isSlashCandidate("/"), false);
  // 不以斜杠开头
  assert.equal(isSlashCandidate("quiz"), false);
  assert.equal(isSlashCandidate(""), false);
});

test("filterCommands matches the prefix after the slash case-insensitively", async () => {
  const { filterCommands } = await loadSlashCommands();

  assert.deepEqual(filterCommands("/e").map((c) => c.name), ["explain"]);
  assert.deepEqual(filterCommands("/ex").map((c) => c.name), ["explain"]);
  assert.deepEqual(filterCommands("/q").map((c) => c.name), ["quiz"]);
  // 大小写不敏感
  assert.deepEqual(filterCommands("/Q").map((c) => c.name), ["quiz"]);
  assert.deepEqual(filterCommands("/P").map((c) => c.name), ["path"]);
  // 无匹配与非候选态恒为空
  assert.deepEqual(filterCommands("/xyz"), []);
  assert.deepEqual(filterCommands("/"), []);
  assert.deepEqual(filterCommands("/quiz 卷积"), []);
});

test("applyCommand swaps the typed prefix for the full command name keeping the rest", async () => {
  const { applyCommand, SLASH_COMMANDS } = await loadSlashCommands();

  const explain = SLASH_COMMANDS.find((c) => c.name === "explain");
  const quiz = SLASH_COMMANDS.find((c) => c.name === "quiz");
  const path = SLASH_COMMANDS.find((c) => c.name === "path");
  assert.ok(explain && quiz && path, "commands missing from registry");

  // 空前缀(仅 "/")补齐为完整指令名
  assert.equal(applyCommand("/", explain), "/explain");
  // 部分前缀替换为完整指令名
  assert.equal(applyCommand("/q", quiz), "/quiz");
  // 保留空格后的后续内容
  assert.equal(applyCommand("/p 支持向量机", path), "/path 支持向量机");
  // 前缀大小写不敏感地整体替换
  assert.equal(applyCommand("/P 保留后续", path), "/path 保留后续");
});
