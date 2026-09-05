// D4-T7:会话时间分组(session-groups)的测试。
// groupSessions / sessionGroupLabel 为纯函数,now 可注入,直接断言;
// 分组按 UTC 自然日计算,测试时间戳全部使用带 Z 的 ISO 字符串,
// 与实现边界(UTC)一致,不受运行环境时区影响。
import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import test from "node:test";

const groupsPath = new URL("../lib/session-groups.ts", import.meta.url);

async function loadSessionGroups() {
  assert.ok(existsSync(groupsPath), "missing session grouping helpers");
  return import("../lib/session-groups");
}

// 固定基准时间(UTC 中午),便于构造确定的「今天 / 近 7 天 / 更早」样本:
// 今天 = 2025-01-10;近 7 天 = 2025-01-03 ~ 2025-01-09;更早 <= 2025-01-02。
const now = new Date("2025-01-10T12:00:00Z");

test("groupSessions splits sessions into today, recent, and older", async () => {
  const { groupSessions } = await loadSessionGroups();

  const sessions = [
    { created_at: "2025-01-01T00:00:00Z", id: "older-9d" }, // 9 天前 → 更早
    { created_at: "2025-01-05T00:00:00Z", id: "recent-5d" }, // 5 天前 → 近 7 天
    { created_at: "2025-01-10T08:00:00Z", id: "today-1" }, // 当天 → 今天
    { created_at: "2025-01-03T00:00:00Z", id: "recent-edge" }, // 恰 7 天前 → 近 7 天
    { created_at: "2025-01-02T23:59:59Z", id: "older-edge" }, // 8 天前 → 更早
    { created_at: "2025-01-09T23:59:59Z", id: "recent-2" }, // 昨天 → 近 7 天
    { created_at: "2025-01-10T00:00:00Z", id: "today-2" }, // 当天零点 → 今天
  ];

  const groups = groupSessions(sessions, now);

  assert.deepEqual(
    groups.today.map((session) => session.id),
    ["today-1", "today-2"],
  );
  assert.deepEqual(
    groups.recent.map((session) => session.id),
    ["recent-2", "recent-5d", "recent-edge"],
  );
  assert.deepEqual(
    groups.older.map((session) => session.id),
    ["older-edge", "older-9d"],
  );
});

test("groupSessions groups and sorts by the latest conversation activity", async () => {
  const { groupSessions } = await loadSessionGroups();

  const groups = groupSessions(
    [
      {
        created_at: "2024-12-01T00:00:00Z",
        updated_at: "2025-01-10T08:00:00Z",
        id: "revived-today",
      },
      {
        created_at: "2025-01-10T09:00:00Z",
        updated_at: "2025-01-10T09:00:00Z",
        id: "newest-today",
      },
      {
        created_at: "2025-01-05T00:00:00Z",
        updated_at: "2025-01-09T20:00:00Z",
        id: "recent",
      },
    ],
    now,
  );

  assert.deepEqual(
    groups.today.map((session) => session.id),
    ["newest-today", "revived-today"],
  );
  assert.deepEqual(groups.recent.map((session) => session.id), ["recent"]);
  assert.deepEqual(groups.older, []);
});

test("groupSessions puts sessions without created_at into older", async () => {
  const { groupSessions } = await loadSessionGroups();

  const groups = groupSessions(
    [
      { id: "no-date" },
      { created_at: "2025-01-10T00:00:00Z", id: "today" },
      { created_at: "not-a-date", id: "invalid-date" },
    ],
    now,
  );

  // 缺失与非法时间戳都防御性归入「更早」,合法当天时间戳不受影响
  assert.deepEqual(groups.today.map((session) => session.id), ["today"]);
  assert.deepEqual(groups.older.map((session) => session.id), ["no-date", "invalid-date"]);
});

test("groupSessions orders each group by latest activity", async () => {
  const { groupSessions } = await loadSessionGroups();

  const sessions = [
    { created_at: "2025-01-09T00:00:00Z", id: "a" },
    { created_at: "2025-01-03T00:00:00Z", id: "b" },
    { created_at: "2025-01-08T00:00:00Z", id: "c" },
  ];

  // 三笔都属「近 7 天」,组内按最近活跃时间倒序。
  const groups = groupSessions(sessions, now);
  assert.deepEqual(groups.recent.map((session) => session.id), ["a", "c", "b"]);
  assert.deepEqual(groups.today, []);
  assert.deepEqual(groups.older, []);
});

test("groupSessions defaults now to the current time", async () => {
  const { groupSessions } = await loadSessionGroups();

  // 不传 now:使用当前时间。构造「今天」样本(当前 UTC 日期的零点),
  // 断言其落入 today 组——默认参数路径可用(边界由上方注入测试覆盖)。
  const todayStart = new Date();
  todayStart.setUTCHours(0, 0, 0, 0);
  const groups = groupSessions([{ created_at: todayStart.toISOString(), id: "now" }]);
  assert.deepEqual(groups.today.map((session) => session.id), ["now"]);
});

test("sessionGroupLabel returns the Chinese group titles", async () => {
  const { sessionGroupLabel } = await loadSessionGroups();

  assert.equal(sessionGroupLabel("today"), "今天");
  assert.equal(sessionGroupLabel("recent"), "近 7 天");
  assert.equal(sessionGroupLabel("older"), "更早");
});
