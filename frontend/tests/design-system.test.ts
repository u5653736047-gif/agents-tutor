import assert from "node:assert/strict";
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

// D5-T1:设计系统落地基线。守护四件事:① DESIGN_SYSTEM.md 五节齐全;
// ② globals.css 动效 tokens 与补齐的语义 token;③ 四角色 token 在
// 文档与 CSS 双处存在;④ 组件内无浅色硬编码残留(bg-white/text-black),
// bg-black 仅允许抽屉遮罩(文档 3.2 允许清单登记)。
const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const globalsPath = resolve(root, "app/globals.css");
const designSystemPath = resolve(root, "DESIGN_SYSTEM.md");
const componentsDir = resolve(root, "components");

const globals = readFileSync(globalsPath, "utf8");
const designSystem = readFileSync(designSystemPath, "utf8");

function collectTsx(dir: string): string[] {
  const files: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      files.push(...collectTsx(full));
    } else if (entry.endsWith(".tsx")) {
      files.push(full);
    }
  }
  return files;
}

test("DESIGN_SYSTEM.md 存在且五节标题齐全", () => {
  assert.ok(existsSync(designSystemPath), "missing DESIGN_SYSTEM.md");
  for (const section of [
    "## 1. Tokens 总表",
    "## 2. 四角色徽章规范",
    "## 3. 组件样式约定",
    "## 4. 暗色模式规则",
    "## 5. 移动端规则",
  ]) {
    assert.ok(
      designSystem.includes(section),
      `DESIGN_SYSTEM.md 缺少节标题:${section}`,
    );
  }
});

test("globals.css 含动效 tokens 与补齐的语义 token", () => {
  for (const token of [
    "--app-duration-fast:",
    "--app-duration-normal:",
    "--app-duration-slow:",
    "--app-ease-out:",
    "--app-ease-in-out:",
    "--input:",
    "--ring:",
    "--color-input:",
    "--color-ring:",
  ]) {
    assert.ok(globals.includes(token), `globals.css 缺少 ${token}`);
  }
  // 动效时长 3 档 + 缓动 2 条,值非空
  assert.match(globals, /--app-duration-fast:\s*[^;]+/);
  assert.match(globals, /--app-duration-normal:\s*[^;]+/);
  assert.match(globals, /--app-duration-slow:\s*[^;]+/);
  assert.match(globals, /--app-ease-out:\s*cubic-bezier\(/);
  assert.match(globals, /--app-ease-in-out:\s*cubic-bezier\(/);
});

test("四角色 token 在文档与 CSS 双处存在", () => {
  for (const role of [
    "supervisor",
    "teaching-assistant",
    "learning-assistant",
    "evaluator",
  ]) {
    assert.ok(
      globals.includes(`--role-${role}:`),
      `globals.css 缺少 --role-${role}`,
    );
    assert.ok(
      globals.includes(`--color-role-${role}:`),
      `globals.css @theme inline 缺少 --color-role-${role}`,
    );
    assert.ok(
      designSystem.includes(role),
      `DESIGN_SYSTEM.md 缺少角色 ${role}`,
    );
  }
});

test("组件内无浅色硬编码残留(bg-white/text-black)", () => {
  const violations: string[] = [];
  for (const file of collectTsx(componentsDir)) {
    const source = readFileSync(file, "utf8");
    if (source.includes("bg-white") || source.includes("text-black")) {
      violations.push(file);
    }
  }
  assert.deepEqual(
    violations,
    [],
    "组件内出现 bg-white/text-black 浅色硬编码残留(暗色下不可读),必须改为语义类",
  );
});

test("bg-black 仅允许抽屉遮罩(app-shell.tsx),且 DESIGN_SYSTEM.md 已登记", () => {
  for (const file of collectTsx(componentsDir)) {
    const source = readFileSync(file, "utf8");
    if (source.includes("bg-black")) {
      assert.ok(
        file.endsWith("app-shell.tsx"),
        `${file} 出现 bg-black 但非遮罩允许文件`,
      );
    }
  }
  // 文档 3.2 允许清单已登记该遮罩例外,文档与实现自洽
  assert.match(designSystem, /bg-black\/40/);
  assert.match(designSystem, /抽屉遮罩/);
});

// D5-T2:动效与过渡——尊重系统「减少动态效果」偏好(WCAG 2.3.3)。
test("globals.css 含 prefers-reduced-motion 全局关闭守卫", () => {
  // 动画与过渡统一收敛为 0.01ms(视觉上等于关闭),滚动平滑关闭
  assert.match(globals, /@media \(prefers-reduced-motion: reduce\)/);
  assert.match(globals, /animation-duration: 0\.01ms !important/);
  assert.match(globals, /animation-iteration-count: 1 !important/);
  assert.match(globals, /transition-duration: 0\.01ms !important/);
  assert.match(globals, /scroll-behavior: auto !important/);
});

test("暗色画布保持柔和层级而不是近黑背景与突兀卡片", () => {
  const darkBlock = globals.match(/\.dark \{([\s\S]*?)\n\}/)?.[1] ?? "";
  const background = Number(
    darkBlock.match(/--background:\s*oklch\(([\d.]+)/)?.[1],
  );
  const card = Number(darkBlock.match(/--card:\s*oklch\(([\d.]+)/)?.[1]);

  assert.ok(background >= 0.16, "暗色画布过黑，长时间阅读压抑");
  assert.ok(card > background, "卡片必须高于画布形成层级");
  assert.ok(card - background <= 0.06, "卡片与画布反差过大，界面会碎片化");
});
