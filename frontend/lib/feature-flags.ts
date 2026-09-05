// assistant-ui 接入:功能开关(灰度 + 秒级回滚)。
// 分层复刻 onboarding.ts(D5-T4)的已验证模式,并经 createLocalStorageFlag
// 工厂收敛(T14 起有第二个开关——原生 Composer 子开关):
//   1. 纯逻辑(flagFromStorage / writeFlag):接受 Storage 接口注入,
//      node:test 用内存 stub 可测;
//   2. window 全局薄封装(isEnabled / setEnabled / subscribe):组件层入口,
//      配合 useSyncExternalStore 订阅。
//
// 取值语义(三层,优先级从高到低):
//   - localStorage 覆盖:显式 "1"/"0",面向灰度与回滚操作;
//   - env 默认:NEXT_PUBLIC_* 为 "1"/"true" 时默认开启(构建期内联,
//     SSR/客户端首帧一致,无 hydration mismatch);
//   - 缺省:关闭(灰度期安全姿态)。

/** 单个 localStorage 开关的工厂产物。 */
export type LocalStorageFlag = {
  /** localStorage 键(值存 "1"/"0") */
  key: string;
  /** 同标签页内变更的自定义事件名(与键同名) */
  eventName: string;
  /** 构建期内联的 env 默认值 */
  envDefault: boolean;
  /** 纯逻辑:从注入的 storage 读覆盖;无覆盖/脏值/异常返回 null */
  flagFromStorage(storage: Pick<Storage, "getItem">): boolean | null;
  /** 纯逻辑:写入覆盖;异常静默 */
  writeFlag(storage: Pick<Storage, "setItem">, enabled: boolean): void;
  /** 有效值(覆盖优先,其次 env 默认);无 window 时恒为 env 默认 */
  isEnabled(): boolean;
  /** 写入覆盖并派发事件驱动订阅者重渲染 */
  setEnabled(enabled: boolean): void;
  /** 订阅变更(自定义事件同标签页 + storage 事件跨标签页) */
  subscribe(callback: () => void): () => void;
};

function createLocalStorageFlag(
  key: string,
  envDefault: boolean,
): LocalStorageFlag {
  const flagFromStorage = (
    storage: Pick<Storage, "getItem">,
  ): boolean | null => {
    try {
      const raw = storage.getItem(key);
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
  };

  const writeFlag = (
    storage: Pick<Storage, "setItem">,
    enabled: boolean,
  ): void => {
    try {
      storage.setItem(key, enabled ? "1" : "0");
    } catch {
      // 静默:localStorage 不可用(隐私模式/配额)时不阻断主流程
    }
  };

  const isEnabled = (): boolean => {
    if (typeof window === "undefined") {
      return envDefault;
    }
    return flagFromStorage(window.localStorage) ?? envDefault;
  };

  const setEnabled = (enabled: boolean): void => {
    if (typeof window === "undefined") {
      return;
    }
    writeFlag(window.localStorage, enabled);
    window.dispatchEvent(new Event(key));
  };

  const subscribe = (callback: () => void): (() => void) => {
    if (typeof window === "undefined") {
      return () => {};
    }
    const onCustom = () => callback();
    const onStorage = (event: StorageEvent) => {
      // 只关心本开关键;event.key 为 null 表示 clear(),不属于本开关变更
      if (event.key === key) {
        callback();
      }
    };
    window.addEventListener(key, onCustom);
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener(key, onCustom);
      window.removeEventListener("storage", onStorage);
    };
  };

  return {
    key,
    eventName: key,
    envDefault,
    flagFromStorage,
    writeFlag,
    isEnabled,
    setEnabled,
    subscribe,
  };
}

// —— 主开关:assistant-ui 渲染路径(新 Thread 替换旧 ConversationPanel) ——

const assistantUiFlag = createLocalStorageFlag(
  "assistant-ui-enabled",
  process.env.NEXT_PUBLIC_ASSISTANT_UI === "1" ||
    process.env.NEXT_PUBLIC_ASSISTANT_UI === "true",
);

// 既有导出(T2 契约,测试与 app-shell 依赖)——保持签名不变
export const ASSISTANT_UI_FLAG_KEY = assistantUiFlag.key;
export const ASSISTANT_UI_FLAG_EVENT = assistantUiFlag.eventName;
export const ASSISTANT_UI_ENV_DEFAULT = assistantUiFlag.envDefault;
export const assistantUiFlagFromStorage = assistantUiFlag.flagFromStorage;
export const writeAssistantUiFlag = assistantUiFlag.writeFlag;
export const isAssistantUiEnabled = assistantUiFlag.isEnabled;
export const setAssistantUiEnabled = assistantUiFlag.setEnabled;
export const subscribeAssistantUiFlag = assistantUiFlag.subscribe;

// —— 子开关(T14):原生 Composer 输入区(默认关,旧 ChatInput 是生产路径) ——

const assistantComposerFlag = createLocalStorageFlag(
  "assistant-ui-composer",
  process.env.NEXT_PUBLIC_ASSISTANT_UI_COMPOSER === "1" ||
    process.env.NEXT_PUBLIC_ASSISTANT_UI_COMPOSER === "true",
);

export const ASSISTANT_COMPOSER_FLAG_KEY = assistantComposerFlag.key;
export const ASSISTANT_COMPOSER_ENV_DEFAULT = assistantComposerFlag.envDefault;
export const assistantComposerFlagFromStorage =
  assistantComposerFlag.flagFromStorage;
export const writeAssistantComposerFlag = assistantComposerFlag.writeFlag;
export const isAssistantComposerEnabled = assistantComposerFlag.isEnabled;
export const setAssistantComposerEnabled = assistantComposerFlag.setEnabled;
export const subscribeAssistantComposerFlag = assistantComposerFlag.subscribe;
