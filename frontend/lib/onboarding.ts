// D5-T4:首次使用引导的「已看过」标记——localStorage 封装。
// 拆分两层:
//   1. 纯逻辑(seenKeyFromStorage / markSeenInStorage):接受 Storage 接口
//      注入,node:test 用内存 stub 可测;
//   2. window 全局薄封装(isOnboardingSeen / markOnboardingSeen /
//      subscribeOnboarding):组件层入口,配合 useSyncExternalStore 订阅
//      外部存储变化。
//
// 事件驱动说明:
// - localStorage 的 "storage" 事件只在【其它标签页】修改时触发,同一标签页
//   内 setItem 不触发任何事件——markOnboardingSeen 写完标记后显式派发自定义
//   事件,useSyncExternalStore 的订阅者(空态引导组件)据此立即重渲染,
//   无需刷新页面。
// - subscribeOnboarding 同时监听自定义事件(同标签页)与 storage 事件
//   (跨标签页同步),返回取消函数供 useSyncExternalStore 卸载时清理。

/** localStorage 中「已看过首次引导」标记的键(值存 "1") */
export const ONBOARDING_SEEN_KEY = "m3-onboarding-seen";

/** 同标签页内引导标记变更的自定义事件名(与键同名,避免记忆两套名字) */
export const ONBOARDING_SEEN_EVENT = "m3-onboarding-seen";

// —— 纯逻辑:接受 Storage 接口注入,可测 ——

/** 从注入的 storage 读取「已看过」标记;读取异常(隐私模式/无权限)按未看过处理 */
export function seenKeyFromStorage(storage: Pick<Storage, "getItem">): boolean {
  try {
    return storage.getItem(ONBOARDING_SEEN_KEY) === "1";
  } catch {
    return false;
  }
}

/** 把「已看过」标记写入注入的 storage;写入异常静默(只影响下次仍展示引导) */
export function markSeenInStorage(storage: Pick<Storage, "setItem">): void {
  try {
    storage.setItem(ONBOARDING_SEEN_KEY, "1");
  } catch {
    // 静默:localStorage 不可用(隐私模式/配额)时不阻断主流程
  }
}

// —— window 全局薄封装:组件层入口 ——

/** 当前是否已看过引导(SSR / 无 window 时恒为 false,与 getServerSnapshot 一致) */
export function isOnboardingSeen(): boolean {
  if (typeof window === "undefined") {
    return false;
  }
  return seenKeyFromStorage(window.localStorage);
}

/**
 * 标记已看过并派发自定义事件,驱动同标签页内的 useSyncExternalStore
 * 订阅者立即更新(事件处理器内调用,无 effect/setState lint 问题)。
 */
export function markOnboardingSeen(): void {
  if (typeof window === "undefined") {
    return;
  }
  markSeenInStorage(window.localStorage);
  window.dispatchEvent(new Event(ONBOARDING_SEEN_EVENT));
}

/**
 * 订阅引导标记变化:自定义事件 = 同标签页内变更;storage 事件 = 跨标签页
 * 同步。返回取消函数;SSR / 无 window 时返回 no-op(useSyncExternalStore
 * 只在客户端调用 subscribe)。
 */
export function subscribeOnboarding(callback: () => void): () => void {
  if (typeof window === "undefined") {
    return () => {};
  }
  const onCustom = () => callback();
  const onStorage = (event: StorageEvent) => {
    // 只关心引导标记键;event.key 为 null 表示 clear(),不属于本标记变更
    if (event.key === ONBOARDING_SEEN_KEY) {
      callback();
    }
  };
  window.addEventListener(ONBOARDING_SEEN_EVENT, onCustom);
  window.addEventListener("storage", onStorage);
  return () => {
    window.removeEventListener(ONBOARDING_SEEN_EVENT, onCustom);
    window.removeEventListener("storage", onStorage);
  };
}
