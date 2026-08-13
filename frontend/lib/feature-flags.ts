// assistant-ui 接入:渲染路径功能开关(灰度 + 秒级回滚)。
// 分层复刻 onboarding.ts(D5-T4)的已验证模式:
//   1. 纯逻辑(assistantUiFlagFromStorage / writeAssistantUiFlag):接受
//      Storage 接口注入,node:test 用内存 stub 可测;
//   2. window 全局薄封装(isAssistantUiEnabled / setAssistantUiEnabled /
//      subscribeAssistantUiFlag):组件层入口,配合 useSyncExternalStore 订阅。
//
// 取值语义(三层,优先级从高到低):
//   - localStorage 覆盖:显式 "1"/"0",面向灰度与回滚操作;
//   - env 默认:NEXT_PUBLIC_ASSISTANT_UI=1 时默认开启(构建期内联,
//     SSR/客户端首帧一致,无 hydration mismatch——服务端快照与客户端
//     首帧都取 env 默认值,hydration 后 localStorage 覆盖经订阅生效);
//   - 缺省:关闭(旧渲染路径是默认路径,灰度期安全姿态)。

/** localStorage 中 assistant-ui 渲染路径开关的键(值存 "1"/"0") */
export const ASSISTANT_UI_FLAG_KEY = "assistant-ui-enabled";

/** 同标签页内开关变更的自定义事件名(与键同名,避免记忆两套名字) */
export const ASSISTANT_UI_FLAG_EVENT = "assistant-ui-enabled";

/** 构建期内联的 env 默认:为 "1"/"true" 时默认开启新渲染路径 */
export const ASSISTANT_UI_ENV_DEFAULT =
  process.env.NEXT_PUBLIC_ASSISTANT_UI === "1" ||
  process.env.NEXT_PUBLIC_ASSISTANT_UI === "true";

// —— 纯逻辑:接受 Storage 接口注入,可测 ——

/**
 * 从注入的 storage 读取开关覆盖;无覆盖返回 null(由调用方回落 env 默认)。
 * 读取异常(隐私模式/无权限)按无覆盖处理。
 */
export function assistantUiFlagFromStorage(
  storage: Pick<Storage, "getItem">,
): boolean | null {
  try {
    const raw = storage.getItem(ASSISTANT_UI_FLAG_KEY);
    if (raw === "1") {
      return true;
    }
    if (raw === "0") {
      return false;
    }
    return null;
  } catch {
    return null;
  }
}

/** 把开关覆盖写入注入的 storage;写入异常静默(只影响本次灰度操作) */
export function writeAssistantUiFlag(
  storage: Pick<Storage, "setItem">,
  enabled: boolean,
): void {
  try {
    storage.setItem(ASSISTANT_UI_FLAG_KEY, enabled ? "1" : "0");
  } catch {
    // 静默:localStorage 不可用(隐私模式/配额)时不阻断主流程
  }
}

// —— window 全局薄封装:组件层入口 ——

/**
 * 当前开关有效值(localStorage 覆盖优先,其次 env 默认)。
 * SSR / 无 window 时恒为 env 默认,与 getServerSnapshot 语义一致。
 */
export function isAssistantUiEnabled(): boolean {
  if (typeof window === "undefined") {
    return ASSISTANT_UI_ENV_DEFAULT;
  }
  return (
    assistantUiFlagFromStorage(window.localStorage) ?? ASSISTANT_UI_ENV_DEFAULT
  );
}

/**
 * 写入开关覆盖并派发自定义事件,驱动同标签页内的 useSyncExternalStore
 * 订阅者立即更新(事件处理器内调用,无 effect/setState lint 问题)。
 */
export function setAssistantUiEnabled(enabled: boolean): void {
  if (typeof window === "undefined") {
    return;
  }
  writeAssistantUiFlag(window.localStorage, enabled);
  window.dispatchEvent(new Event(ASSISTANT_UI_FLAG_EVENT));
}

/**
 * 订阅开关变化:自定义事件 = 同标签页内变更;storage 事件 = 跨标签页
 * 同步。返回取消函数;SSR / 无 window 时返回 no-op(useSyncExternalStore
 * 只在客户端调用 subscribe)。
 */
export function subscribeAssistantUiFlag(callback: () => void): () => void {
  if (typeof window === "undefined") {
    return () => {};
  }
  const onCustom = () => callback();
  const onStorage = (event: StorageEvent) => {
    // 只关心本开关键;event.key 为 null 表示 clear(),不属于本开关变更
    if (event.key === ASSISTANT_UI_FLAG_KEY) {
      callback();
    }
  };
  window.addEventListener(ASSISTANT_UI_FLAG_EVENT, onCustom);
  window.addEventListener("storage", onStorage);
  return () => {
    window.removeEventListener(ASSISTANT_UI_FLAG_EVENT, onCustom);
    window.removeEventListener("storage", onStorage);
  };
}
