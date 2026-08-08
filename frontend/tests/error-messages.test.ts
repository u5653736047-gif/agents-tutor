import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import test from "node:test";

const libPath = new URL("../lib/error-messages.ts", import.meta.url);

async function loadErrorMessages() {
  assert.ok(existsSync(libPath), "missing error messages lib");
  return import("../lib/error-messages");
}

// core ErrorCode(后端 RunError.error_code 可能值,与契约 ErrorCode 联合类型一致)
const coreCodes = [
  "tool_unknown",
  "tool_unauthorized",
  "tool_invalid_arguments",
  "tool_execution_failed",
  "tool_timeout",
  "model_call_failed",
  "react_iteration_limit",
  "graph_handoff_limit",
  "graph_switch_limit",
  "graph_invalid_target",
  "graph_aggregation_invalid",
  "agent_output_invalid",
] as const;

// ApiErrorCode(前端 ApiClientError.code 可能值,与契约 ApiErrorCode 联合类型一致)
const apiCodes = [
  "invalid_request",
  "internal_error",
  "handoff_not_pending",
  "session_already_exists",
  "session_busy",
  "session_not_found",
] as const;

test("errorMessageFor maps every core ErrorCode to a non-empty preset", async () => {
  const { errorMessageFor } = await loadErrorMessages();

  for (const code of coreCodes) {
    const preset = errorMessageFor(code);
    assert.ok(preset.title.length > 0, `${code} should have a title`);
    assert.ok(preset.detail.length > 0, `${code} should have a detail`);
  }

  // 分组文案抽查:工具 / 轮次 / 协作异常各覆盖一个代表码
  assert.equal(errorMessageFor("tool_timeout").title, "工具执行异常");
  assert.equal(errorMessageFor("tool_unknown").title, "工具不存在");
  assert.equal(errorMessageFor("react_iteration_limit").title, "执行轮次超限");
  assert.equal(
    errorMessageFor("graph_handoff_limit").title,
    "执行轮次超限",
  );
  assert.equal(errorMessageFor("graph_invalid_target").title, "协作目标无效");
  assert.equal(errorMessageFor("agent_output_invalid").title, "Agent 输出无效");
});

test("errorMessageFor maps every ApiErrorCode to a non-empty preset", async () => {
  const { errorMessageFor } = await loadErrorMessages();

  for (const code of apiCodes) {
    const preset = errorMessageFor(code);
    assert.ok(preset.title.length > 0, `${code} should have a title`);
    assert.ok(preset.detail.length > 0, `${code} should have a detail`);
  }

  assert.equal(errorMessageFor("session_busy").title, "会话正忙");
  assert.equal(errorMessageFor("session_busy").action, "稍后再试");
  assert.equal(errorMessageFor("handoff_not_pending").title, "审批已处理");
  assert.equal(errorMessageFor("session_not_found").title, "会话不存在");
  assert.equal(errorMessageFor("session_already_exists").title, "会话已存在");
  assert.equal(errorMessageFor("invalid_request").title, "请求无效");
  assert.equal(errorMessageFor("internal_error").title, "服务内部错误");
});

test("errorMessageFor treats null and undefined as network failures", async () => {
  const { errorMessageFor } = await loadErrorMessages();

  // 请求层错误:网络失败 / 超时(ApiClientError.code === null)
  const network = errorMessageFor(null);
  assert.equal(network.title, "网络请求失败");
  assert.equal(network.detail, "请检查网络连接后重试。");
  assert.equal(network.action, "重新发送上一条消息");

  const fromUndefined = errorMessageFor(undefined);
  assert.equal(fromUndefined.title, "网络请求失败");
  assert.equal(fromUndefined.action, "重新发送上一条消息");
});

test("errorMessageFor falls back to a generic preset for unknown codes", async () => {
  const { errorMessageFor } = await loadErrorMessages();

  const preset = errorMessageFor("some_future_error_code");
  assert.equal(preset.title, "出了点问题");
  assert.equal(preset.detail, "请求未能完成,请稍后重试。");
});
