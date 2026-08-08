import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import test from "node:test";

// D6-T7:学习进度仪表盘(基础统计版)。
// 页面是 "use client" 客户端组件,数据在挂载后拉取,SSR 只能锁定
// 初始态(标题/加载骨架/占位卡);挂载拉取、空数据渲染与错误归一由
// 源码正则守卫(与 knowledge-page 测试先例一致)。
const statsPagePath = new URL("../app/stats/page.tsx", import.meta.url);

async function loadStatsPage() {
  assert.ok(existsSync(statsPagePath), "missing stats page");
  return import("../app/stats/page");
}

test("the stats page SSR-renders the title, loading skeleton, and analysis placeholder", async () => {
  const { default: StatsPage } = await loadStatsPage();

  assert.equal(typeof StatsPage, "function", "missing default export page component");
  const markup = renderToStaticMarkup(createElement(StatsPage));

  // 标题 + 返回首页链接(渲染为 <a href="/">)
  assert.match(markup, /学习进度/);
  assert.match(markup, /href="\/"[^>]*>返回首页</);
  // 初始(SSR)只渲染加载骨架:数据卡片/错误行/空态提示均不出现
  assert.match(markup, /data-slot="stats-loading"/);
  assert.doesNotMatch(markup, /data-slot="stats-cards"/);
  assert.doesNotMatch(markup, /data-slot="stats-error"/);
  assert.doesNotMatch(markup, /data-slot="stats-empty"/);
  // 占位卡静态渲染(进度分析依赖后端能力,本期未实现)
  assert.match(markup, /data-slot="stats-analysis-placeholder"/);
  assert.match(markup, /进度分析\(错题\/知识图谱\)/);
  assert.match(markup, /待后端能力/);
});

// 交互实现要点源码守卫:挂载拉取时序、空数据渲染、错误归一、柱状条。
test("the stats page loads overview on mount and renders zero data without errors", () => {
  const source = readFileSync(statsPagePath, "utf8");

  // 客户端组件 + 直接调 api-client(不接 store,与 knowledge 页同构)
  assert.match(source, /"use client"/);
  assert.match(source, /apiClient\.getStatsOverview\(\)/);
  // 挂载拉取:useEffect 内局部 async 函数 + ignore 标志 + void load()
  // (setState 全部在 await 之后,符合 react-hooks「effect 内同步
  // setState」规则)
  assert.match(source, /useEffect\(\(\) => \{\s*\n\s*let ignore = false;/);
  assert.match(source, /void load\(\);/);
  assert.match(source, /setStats\(overview\)/);
  // 统计卡片三件套:会话数/消息数/最近活动时间
  assert.match(source, /data-slot="stat-card-sessions"/);
  assert.match(source, /data-slot="stat-card-messages"/);
  assert.match(source, /data-slot="stat-card-last-activity"/);
  // 空数据不报错:全 0 也渲染卡片,附空态提示;最近活动可空 "—" 兜底
  // (原样显示 ISO 字符串,不做时区格式化,避免 hydration mismatch)
  assert.match(source, /data-slot="stats-empty"/);
  assert.match(source, /暂无学习数据/);
  assert.match(source, /stats\.last_activity_at \?\? "—"/);
  // Agent 回答分布:固定四角色中文名 + 计数;宽度按最大计数归一(全 0
  // 时取 1 防除零,条宽 0%)
  assert.match(source, /data-slot="stats-agent-bar"/);
  assert.match(source, /ROLE_LABELS\[role\]/);
  assert.match(source, /Math\.max\(\.\.\.distributionCounts, 1\)/);
  assert.match(source, /width: `\$\{width\}%`/);
  // 错误归一:ApiClientError 分支 + 兜底文案
  assert.match(source, /instanceof ApiClientError/);
  assert.match(source, /请求失败,请稍后重试。/);
  // 占位卡:进度分析(错题/知识图谱)标注待后端能力
  assert.match(source, /data-slot="stats-analysis-placeholder"/);
});
