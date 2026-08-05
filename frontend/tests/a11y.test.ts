import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import test from "node:test";

// D5-T5:可访问性收口基线。无 jsdom,焦点行为无法运行时测试——实现要点
// 用源码正则守卫(与既有 app-shell/handoff-card 测试先例一致),静态输出
// 由各组件测试的 SSR 断言覆盖;动态行为(焦点移入/归还、读屏播报)由
// 手动验收清单执行(见 docs/TASKS_STAGE_3_DETAILS.md D5-T5 完成备注)。
//
// 手动验收清单(仅键盘,主线程执行并记录):
// 1. 新建 → 提问:Tab 导航到「新建会话」Enter 创建,输入提问 Enter 发送;
//    Tab 遍历全程每个可交互元素有可见 focus-visible 高亮环(鼠标点击不显示)。
// 2. 审批:卡片出现后焦点自动进入;Tab 依次到达 拒绝/确认/修改并继续;
//    确认(Enter)后卡片消失,焦点按浏览器默认回落 body,可继续 Tab 操作。
// 3. 归档:Tab 到会话操作菜单,键盘完成归档;空态出现后可继续新建会话。
// 4. 抽屉:移动端汉堡打开抽屉,焦点移入抽屉;Esc 关闭且焦点回到汉堡按钮;
//    遮罩点击、选中会话关闭同样归还焦点。
// 5. 读屏(可选,有环境时执行):流式回答期间播报「助手正在生成回答…」,
//    发送期间播报「正在发送…」;审批卡片出现时播报卡片内容。

const globalsPath = new URL("../app/globals.css", import.meta.url);
const appShellPath = new URL("../components/app-shell.tsx", import.meta.url);
const panelPath = new URL("../components/conversation-panel.tsx", import.meta.url);
const cardPath = new URL("../components/handoff-card.tsx", import.meta.url);

test("globals.css 提供全局 focus-visible 高亮环(键盘焦点可见性)", () => {
  assert.ok(existsSync(globalsPath), "missing globals.css");
  const globals = readFileSync(globalsPath, "utf8");

  // :focus-visible 统一高亮环:仅键盘/辅助输入触发,鼠标点击不显示
  assert.match(globals, /:focus-visible\s*\{/);
  assert.match(globals, /outline: 2px solid var\(--ring\)/);
  assert.match(globals, /outline-offset: 2px/);
  // ring token 亮/暗双模式已定义(D5-T1),焦点环随主题自动适配
  assert.match(globals, /--ring: var\(--brand\)/);
});

test("app-shell:抽屉打开时焦点移入、关闭时统一归还汉堡按钮", () => {
  assert.ok(existsSync(appShellPath), "missing app shell");
  const source = readFileSync(appShellPath, "utf8");

  // 抽屉容器 tabIndex={-1} + ref:打开时可被程序化聚焦
  assert.match(source, /data-slot="sidebar-drawer"/);
  assert.match(source, /tabIndex=\{-1\}/);
  assert.match(source, /ref=\{drawerRef\}/);
  // 打开时焦点移入:effect 内只做 DOM 焦点同步(focus(),不 setState,
  // react-hooks lint 认可的「与外部系统同步」合法用法)
  assert.match(source, /drawerRef\.current\?\.focus\(\)/);
  // 关闭统一入口 closeDrawer:setState + 焦点归还汉堡按钮(toggleRef)
  assert.match(source, /setSidebarOpen\(false\);\s*\n\s*toggleRef\.current\?\.focus\(\)/);
  // 遮罩点击 / Esc / 选中会话三条关闭路径全部走 closeDrawer
  assert.match(source, /onClick=\{closeDrawer\}/);
  assert.match(source, /onSessionSelected=\{closeDrawer\}/);
  assert.match(source, /event\.key === "Escape"\) \{\s*\n\s*closeDrawer\(\)/);
});

test("conversation-panel:消息流 aria-live 区域 + sr-only 状态播报 + 骨架 aria-hidden", () => {
  assert.ok(existsSync(panelPath), "missing conversation panel");
  const source = readFileSync(panelPath, "utf8");

  // 消息流区是 aria-live=polite 区域:新消息/流式追加与状态文本自然播报
  assert.match(source, /aria-live="polite"[\s\S]*?data-slot="message-list"/);
  // sr-only 状态行:流式「助手正在生成回答…」/发送「正在发送…」
  assert.match(source, /data-slot="live-status"/);
  assert.match(source, /助手正在生成回答…/);
  assert.match(source, /正在发送…/);
  // 骨架(视觉占位)aria-hidden:避免读屏朗读骨架噪音,状态由 live-status
  // 播报。断言与 JSX 属性顺序一致(aria-hidden → className → data-slot 同元素)
  assert.match(
    source,
    /aria-hidden\s*\n\s*className="mt-2 space-y-2"\s*\n\s*data-slot="streaming-skeleton"/,
  );
  assert.match(
    source,
    /aria-hidden\s*\n\s*className="flex justify-start"\s*\n\s*data-slot="message-skeleton"/,
  );
});

test("handoff-card:卡片 aria-live 播报 + tabIndex 焦点进入", () => {
  assert.ok(existsSync(cardPath), "missing handoff card");
  const source = readFileSync(cardPath, "utf8");

  // 容器 aria-live="polite":卡片出现/消失时审批状态变化可感知
  assert.match(source, /aria-live="polite"/);
  // tabIndex={-1} + ref + effect:出现时焦点移入(仅 pending 变化触发)
  assert.match(source, /tabIndex=\{-1\}/);
  assert.match(source, /ref=\{cardRef\}/);
  assert.match(source, /cardRef\.current\?\.focus\(\)/);
  // 取舍:卡片非模态对话框——不做 Esc 关闭(确认/拒绝需显式操作,
  // 无 keydown 监听),决定后焦点按浏览器默认回落(注释见组件)
  assert.doesNotMatch(source, /addEventListener/);
});
