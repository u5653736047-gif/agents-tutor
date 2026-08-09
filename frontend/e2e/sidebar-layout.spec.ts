import { expect, test } from "@playwright/test";

import { installMocks, mockSession } from "./mocks";

test.beforeEach(async ({ page }) => {
  await installMocks(page, {
    seedSessions: [mockSession("mock-session-ui", "最近学习：注意力机制")],
  });
});

test("桌面侧栏拖拽与收起时主会话区同步改变尺寸", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/");

  const sidebar = page.locator('[data-slot="desktop-sidebar"]');
  const conversation = page.locator('[data-slot="conversation-area"]');
  const resizer = page.locator('[data-slot="sidebar-resizer"]');

  await expect(sidebar).toBeVisible();
  await expect(resizer).toBeVisible();

  const initialSidebar = await sidebar.boundingBox();
  const initialConversation = await conversation.boundingBox();
  const resizerBox = await resizer.boundingBox();
  expect(initialSidebar).not.toBeNull();
  expect(initialConversation).not.toBeNull();
  expect(resizerBox).not.toBeNull();

  await page.mouse.move(
    resizerBox!.x + resizerBox!.width / 2,
    resizerBox!.y + resizerBox!.height / 2,
  );
  await page.mouse.down();
  await page.mouse.move(resizerBox!.x + 96, resizerBox!.y + resizerBox!.height / 2, {
    steps: 6,
  });
  await page.mouse.up();

  await expect
    .poll(async () => (await sidebar.boundingBox())?.width ?? 0)
    .toBeGreaterThan(initialSidebar!.width + 70);

  const resizedConversation = await conversation.boundingBox();
  expect(resizedConversation).not.toBeNull();
  expect(resizedConversation!.x).toBeGreaterThan(initialConversation!.x + 70);
  expect(resizedConversation!.width).toBeLessThan(initialConversation!.width - 70);

  await page.getByRole("button", { name: "收起会话侧栏" }).click();
  await expect(page.locator('[data-slot="sidebar-collapsed"]')).toBeVisible();
  await expect(resizer).toHaveCount(0);

  const collapsedSidebar = await sidebar.boundingBox();
  const expandedConversation = await conversation.boundingBox();
  expect(collapsedSidebar).not.toBeNull();
  expect(expandedConversation).not.toBeNull();
  expect(collapsedSidebar!.width).toBeLessThan(initialSidebar!.width);
  expect(expandedConversation!.x).toBeLessThan(initialConversation!.x);
  expect(expandedConversation!.width).toBeGreaterThan(initialConversation!.width);

  await page.getByRole("button", { name: "展开会话侧栏" }).click();
  await expect(resizer).toBeVisible();
  await resizer.dblclick();
  await expect(resizer).toHaveAttribute("aria-valuenow", "296");

  if (process.env.UI_UX_SCREENSHOT === "1") {
    await page.screenshot({
      animations: "disabled",
      path: "test-results/ui-ux-desktop.png",
      fullPage: true,
    });
  }

  await page.getByRole("button", { name: "切换到暗色模式" }).click();
  await expect(page.locator("html")).toHaveClass(/dark/);
  await expect
    .poll(async () => {
      const background = await page
        .locator('[data-slot="example-question"]')
        .first()
        .evaluate((element) => getComputedStyle(element).backgroundColor);
      return Number(background.match(/^oklab\(([\d.]+)/)?.[1] ?? 1);
    })
    .toBeLessThan(0.4);

  if (process.env.UI_UX_SCREENSHOT === "1") {
    await page.screenshot({
      animations: "disabled",
      path: "test-results/ui-ux-desktop-dark.png",
      fullPage: true,
    });
  }
});
