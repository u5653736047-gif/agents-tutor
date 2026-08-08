// D4-T8:滚动跟随判定——距底部不足阈值视为「贴底」。
// 贴底时新消息应自动滚动跟随;否则视为用户上翻浏览,
// 暂停跟随(用户回到底部后由 onScroll 重新置位,自动恢复)。
export function isNearBottom(
  scrollTop: number,
  clientHeight: number,
  scrollHeight: number,
  threshold = 120,
): boolean {
  // 边界:scrollHeight=0(空列表)时差值 0 ≤ 阈值,视为贴底
  return scrollHeight - (scrollTop + clientHeight) <= threshold;
}
