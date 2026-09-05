// 会话时间分组——按最后活跃时间把会话分为「今天 / 近 7 天 / 更早」
// 三组,侧栏分组展示用。纯函数(now 可注入),便于 SSR 渲染与单测。

export type SessionGroup = "today" | "recent" | "older";

// 一个 UTC 自然日的毫秒数(分组按 UTC 自然日边界计算)。
const DAY_MS = 24 * 60 * 60 * 1000;

type SessionActivity = { created_at?: string; updated_at?: string };

function activityTimestamp(session: SessionActivity): number {
  const timestamp = Date.parse(session.updated_at ?? session.created_at ?? "");
  return Number.isNaN(timestamp) ? Number.NEGATIVE_INFINITY : timestamp;
}

// 按 updated_at 分组并倒序；旧契约缺少 updated_at 时回退 created_at。
// 缺失或非法时间戳防御性归入 "older"。边界按 UTC 计算，避免本地
// 时区导致的「跨日」边界抖动,也便于测试注入 now。
export function groupSessions<T extends SessionActivity>(
  sessions: T[],
  now: Date = new Date(),
): Record<SessionGroup, T[]> {
  const groups: Record<SessionGroup, T[]> = { today: [], recent: [], older: [] };
  // 今天起点(UTC 自然日 00:00);近 7 天起点 = 今天起点往前 7 个自然日。
  const todayStart = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
  const todayEnd = todayStart + DAY_MS;
  const recentStart = todayStart - 7 * DAY_MS;

  const orderedSessions = [...sessions].sort(
    (left, right) => activityTimestamp(right) - activityTimestamp(left),
  );

  for (const session of orderedSessions) {
    const activity = activityTimestamp(session);
    if (!Number.isFinite(activity)) {
      // 缺失/非法时间戳:防御性归入最老分组(无法判定则按「更早」展示)
      groups.older.push(session);
      continue;
    }
    if (activity >= todayStart && activity < todayEnd) {
      groups.today.push(session);
    } else if (activity >= recentStart && activity < todayStart) {
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
