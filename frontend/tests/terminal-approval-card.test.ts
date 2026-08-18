import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import test from "node:test";

const cardPath = new URL(
  "../components/terminal-approval-card.tsx",
  import.meta.url,
);

async function loadCard() {
  assert.ok(existsSync(cardPath), "missing terminal approval card");
  return import("../components/terminal-approval-card");
}

const pending = {
  interrupt_id: "interrupt-shell-1",
  request: {
    agent_role: "supervisor" as const,
    arguments: {
      command: "git status; git diff --stat",
      cwd: "D:\\Projects\\course",
      description: "检查当前改动",
      timeout_seconds: 30,
    },
    tool_call_id: "shell-1",
    tool_name: "shell",
  },
};

test("the terminal approval card shows the exact command and security boundary", async () => {
  const { TerminalApprovalCard } = await loadCard();
  const markup = renderToStaticMarkup(
    createElement(TerminalApprovalCard, {
      isDeciding: false,
      onDecide: () => {},
      pending,
    }),
  );

  assert.match(markup, /data-slot="terminal-approval-card"/);
  assert.match(markup, /命令执行审批/);
  assert.match(markup, /git status; git diff --stat/);
  assert.match(markup, /D:\\Projects\\course/);
  assert.match(markup, /检查当前改动/);
  assert.match(markup, /30 秒/);
  assert.match(markup, /服务进程账号权限/);
  assert.match(markup, /data-slot="terminal-confirm"/);
  assert.match(markup, /data-slot="terminal-reject"/);
});

test("the office approval card joins an array command and shows the office title", async () => {
  const { TerminalApprovalCard } = await loadCard();
  const officePending = {
    interrupt_id: "interrupt-office-1",
    request: {
      agent_role: "supervisor" as const,
      arguments: {
        command: ["set", "成绩单.xlsx", "/Sheet1/A1", "--prop", "value=95"],
      },
      tool_call_id: "office-1",
      tool_name: "officecli_edit",
    },
  };
  const markup = renderToStaticMarkup(
    createElement(TerminalApprovalCard, {
      isDeciding: false,
      onDecide: () => {},
      pending: officePending,
    }),
  );

  // 数组命令必须完整拼接展示，绝不能出现「（未提供命令）」导致用户盲批
  assert.match(markup, /Office 文档修改审批/);
  assert.match(markup, /set 成绩单\.xlsx \/Sheet1\/A1 --prop value=95/);
  assert.doesNotMatch(markup, /未提供命令/);
  // office 命令没有 cwd 参数：不渲染「工作目录」行
  assert.doesNotMatch(markup, /工作目录/);
  assert.match(markup, /工作区内的 Office 文档/);
});

test("the terminal approval card is absent without a pending command", async () => {
  const { TerminalApprovalCard } = await loadCard();
  const markup = renderToStaticMarkup(
    createElement(TerminalApprovalCard, {
      isDeciding: false,
      onDecide: () => {},
      pending: null,
    }),
  );

  assert.equal(markup, "");
});

test("the terminal approval card locks both choices while executing", async () => {
  const { TerminalApprovalCard } = await loadCard();
  const markup = renderToStaticMarkup(
    createElement(TerminalApprovalCard, {
      isDeciding: true,
      onDecide: () => {},
      pending,
    }),
  );

  assert.match(markup, /data-slot="terminal-confirm"[^>]*disabled/);
  assert.match(markup, /data-slot="terminal-reject"[^>]*disabled/);
  assert.match(markup, /正在执行/);
});
