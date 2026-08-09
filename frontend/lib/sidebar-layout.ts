export const DEFAULT_SIDEBAR_WIDTH = 296;
export const MIN_SIDEBAR_WIDTH = 248;
export const MAX_SIDEBAR_WIDTH = 420;
export const COLLAPSED_SIDEBAR_WIDTH = 72;

const KEYBOARD_RESIZE_STEP = 16;

export function clampSidebarWidth(width: number): number {
  return Math.min(MAX_SIDEBAR_WIDTH, Math.max(MIN_SIDEBAR_WIDTH, width));
}

export function resizeSidebarWidth(
  startWidth: number,
  startPointerX: number,
  pointerX: number,
): number {
  return clampSidebarWidth(startWidth + pointerX - startPointerX);
}

export function sidebarWidthForKey(width: number, key: string): number {
  if (key === "Home") return MIN_SIDEBAR_WIDTH;
  if (key === "End") return MAX_SIDEBAR_WIDTH;
  if (key === "ArrowLeft") return clampSidebarWidth(width - KEYBOARD_RESIZE_STEP);
  if (key === "ArrowRight") return clampSidebarWidth(width + KEYBOARD_RESIZE_STEP);
  return width;
}
