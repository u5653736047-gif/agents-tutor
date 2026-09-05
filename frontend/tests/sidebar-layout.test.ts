import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import test from "node:test";

const layoutPath = new URL("../lib/sidebar-layout.ts", import.meta.url);

async function loadSidebarLayout() {
  assert.ok(existsSync(layoutPath), "missing sidebar layout helpers");
  return import("../lib/sidebar-layout");
}

test("sidebar width stays within usable desktop bounds", async () => {
  const {
    DEFAULT_SIDEBAR_WIDTH,
    MAX_SIDEBAR_WIDTH,
    MIN_SIDEBAR_WIDTH,
    resizeSidebarWidth,
  } = await loadSidebarLayout();

  assert.equal(DEFAULT_SIDEBAR_WIDTH, 296);
  assert.equal(MIN_SIDEBAR_WIDTH, 248);
  assert.equal(MAX_SIDEBAR_WIDTH, 420);
  assert.equal(resizeSidebarWidth(296, 300, 180), 248);
  assert.equal(resizeSidebarWidth(296, 300, 520), 420);
  assert.equal(resizeSidebarWidth(296, 300, 340), 336);
});

test("keyboard resizing uses predictable steps and boundary shortcuts", async () => {
  const { sidebarWidthForKey } = await loadSidebarLayout();

  assert.equal(sidebarWidthForKey(296, "ArrowLeft"), 280);
  assert.equal(sidebarWidthForKey(296, "ArrowRight"), 312);
  assert.equal(sidebarWidthForKey(296, "Home"), 248);
  assert.equal(sidebarWidthForKey(296, "End"), 420);
  assert.equal(sidebarWidthForKey(296, "Escape"), 296);
});
