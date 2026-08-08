// D4-T7:会话时间分组——按 created_at 把会话分为「今天 / 近 7 天 / 更早」
// 三组,侧栏分组展示用。纯函数(now 可注入),便于 SSR 渲染与单测。

export type SessionGroup = "today" | "recent" | "older";

// 一个 UTC 自然日的毫秒数(分组按 UTC 自然日边界计算)。
const DAY_MS = 24 * 60 * 60 * 1000;

// 按 created_at 分组:今天(UTC 当天)/ 近 7 天(距今 1~7 个自然日,
// 不含今天)/ 更早(8 个自然日前及更早)。created_at 缺失或解析失败
// 的会话防御性归入 "older"。分组结果保持输入顺序(每组内原序)。
// 边界按 UTC 计算:created_at 为 ISO 字符串(带 Z),UTC 比较确定,
// 避免本地时区导致的「跨日」边界抖动,也便于测试注入 now。
export function groupSessions<T extends { created_at?: string }>(
  sessions: T[],
  now: Date = new Date(),
): Record<SessionGroup, T[]> {
  const groups: Record<SessionGroup, T[]> = { today: [], recent: [], older: [] };
  // 今天起点(UTC 自然日 00:00);近 7 天起点 = 今天起点往前 7 个自然日。
  const todayStart = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
  const todayEnd = todayStart + DAY_MS;
  const recentStart = todayStart - 7 * DAY_MS;

  for (const session of sessions) {
    const created = session.created_at ? Date.parse(session.created_at) : NaN;
    if (Number.isNaN(created)) {
      // 缺失/非法时间戳:防御性归入最老分组(无法判定则按「更早」展示)
      groups.older.push(session);
      continue;
    }
    if (created >= todayStart && created < todayEnd) {
      groups.today.push(session);
    } else if (created >= recentStart && created < todayStart) {
      // 距今 1~7 个自然日(不含今天);未来时间戳同样落入 older
      groups.recent.push(session);
    } else {
      groups.older.push(session);
    }
  }

  return groups;
}

const GROUP_LABELS: Record<SessionGroup, string> = {
  today: "今天",
  recent: "近 7 天",
  older: "更早",
};

// 组标题文案(侧栏组标题展示用)。
export function sessionGroupLabel(group: SessionGroup): string {
  return GROUP_LABELS[group];
}
