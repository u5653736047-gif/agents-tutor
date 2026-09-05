import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import test from "node:test";

// D6-T7:学习进度仪表盘(基础统计版) + 赛前可视化增强(学情洞察卡)。
// 页面是 "use client" 客户端组件,数据在挂载后拉取,SSR 只能锁定
// 初始态(标题/加载骨架);挂载拉取、空数据渲染、洞察降级与错误归一
// 由源码正则守卫(与 knowledge-page 测试先例一致)。
const statsPagePath = new URL("../app/stats/page.tsx", import.meta.url);

async function loadStatsPage() {
  assert.ok(existsSync(statsPagePath), "missing stats page");
  return import("../app/stats/page");
}

test("the stats page SSR-renders the title, loading skeleton, and AI notice", async () => {
  const { default: StatsPage } = await loadStatsPage();

  assert.equal(typeof StatsPage, "function", "missing default export page component");
  const markup = renderToStaticMarkup(createElement(StatsPage));

  // 标题 + 返回首页链接(渲染为 <a href="/">)
  assert.match(markup, /学习进度/);
  assert.match(markup, /href="\/"[^>]*>返回首页</);
  // 初始(SSR)只渲染加载骨架:数据卡片/错误行/空态提示/洞察区均不出现
  assert.match(markup, /data-slot="stats-loading"/);
  assert.doesNotMatch(markup, /data-slot="stats-cards"/);
  assert.doesNotMatch(markup, /data-slot="stats-error"/);
  assert.doesNotMatch(markup, /data-slot="stats-empty"/);
  assert.doesNotMatch(markup, /data-slot="stats-insights"/);
  // AI 生成内容标识:页脚全局声明常驻(伦理合规,不依赖数据拉取)
  assert.match(markup, /data-slot="ai-content-notice"/);
  assert.match(markup, /重要信息请人工复核/);
});

// 交互实现要点源码守卫:挂载拉取时序、三源降级、空数据渲染、洞察四卡。
test("the stats page loads overview and insights on mount with graceful degradation", () => {
  const source = readFileSync(statsPagePath, "utf8");

  // 客户端组件 + 直接调 api-client(不接 store,与 knowledge 页同构)
  assert.match(source, /"use client"/);
  assert.match(source, /apiClient\.getStatsOverview\(\)/);
  assert.match(source, /apiClient\.getDiagnosisSummary\(\)/);
  assert.match(source, /apiClient\.getLearningInsights\(\)/);
  // 挂载拉取:useEffect 内局部 async 函数 + ignore 标志 + void load()
  // (setState 全部在 await 之后,符合 react-hooks「effect 内同步
  // setState」规则)
  assert.match(source, /useEffect\(\(\) => \{\s*\n\s*let ignore = false;/);
  assert.match(source, /void load\(\);/);
  assert.match(source, /setStats\(overview\)/);
  // 洞察双源 allSettled:单侧失败照常渲染另一侧,全败才降级提示行
  assert.match(source, /Promise\.allSettled/);
  assert.match(source, /data-slot="stats-insights-unavailable"/);
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
  // 学情洞察四卡:薄弱点预警/错题归因/正确率趋势/路径回显
  assert.match(source, /data-slot="stats-weak-points"/);
  assert.match(source, /data-slot="stats-error-tags"/);
  assert.match(source, /data-slot="stats-accuracy-trend"/);
  assert.match(source, /data-slot="stats-path-plans"/);
  // 各卡空态引导文案(零数据不报错,照常渲染)
  assert.match(source, /data-slot="stats-weak-empty"/);
  assert.match(source, /data-slot="stats-error-tags-empty"/);
  assert.match(source, /data-slot="stats-trend-empty"/);
  assert.match(source, /data-slot="stats-path-empty"/);
});
