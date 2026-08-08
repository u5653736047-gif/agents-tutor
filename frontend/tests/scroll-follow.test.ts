import assert from "node:assert/strict";
import test from "node:test";

import { isNearBottom } from "../lib/scroll-follow";

test("isNearBottom treats a distance under the threshold as at the bottom", () => {
  // 距底部 80px < 默认阈值 120 → 贴底,新消息应自动滚动跟随
  assert.equal(isNearBottom(100, 500, 680), true);
});

test("isNearBottom treats an exact threshold distance as at the bottom", () => {
  // 距底部恰好 120px = 阈值 → 贴底(边界含等号)
  assert.equal(isNearBottom(100, 500, 720), true);
});

test("isNearBottom pauses follow when scrolled up beyond the threshold", () => {
  // 距底部 200px > 阈值 → 用户上翻浏览,暂停跟随
  assert.equal(isNearBottom(100, 500, 800), false);
});

test("isNearBottom treats an empty list as at the bottom", () => {
  // scrollHeight=0(空列表)→ 贴底,不会误判为上翻
  assert.equal(isNearBottom(0, 0, 0), true);
});

test("isNearBottom honors a custom threshold", () => {
  // 自定义阈值 40:距底部 50px 不算贴底,40px(含)算贴底
  assert.equal(isNearBottom(0, 100, 150, 40), false);
  assert.equal(isNearBottom(0, 100, 140, 40), true);
});
