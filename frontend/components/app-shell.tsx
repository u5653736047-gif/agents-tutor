"use client";

import { CircleCheck, CircleX, Menu, Moon, Sun } from "lucide-react";
import { useTheme } from "next-themes";
import { useEffect, useState } from "react";

import { ConversationPanel } from "@/components/conversation-panel";
import { SessionSidebar } from "@/components/session-sidebar";
import { useChatStore } from "@/stores/chat-store";

type AppShellProps = {
  apiConnected: boolean;
};

export function AppShell({ apiConnected }: AppShellProps) {
  const currentSessionId = useChatStore((state) => state.currentSessionId);

  // D4-T5:移动端抽屉状态。初始 false,SSR 首屏不渲染抽屉/遮罩,
  // 开合全部发生在客户端交互之后(SSR 安全)。
  const [sidebarOpen, setSidebarOpen] = useState(false);

  // D4-T6:主题切换。图标 CSS 类驱动无需 mounted;aria-label/onClick
  // 直接读 resolvedTheme(SSR 首帧 undefined,hydration 后校准)。
  const { resolvedTheme, setTheme } = useTheme();

  // D4-T5:抽屉打开期间注册 keydown 监听,Esc 关闭;关闭后移除监听。
  useEffect(() => {
    if (!sidebarOpen) {
      return;
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setSidebarOpen(false);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [sidebarOpen]);

  return (
    <main
      // D4-T5:断点语义——移动端单栏(grid-cols-1,主区独占整行),
      // md 起恢复桌面两栏(18rem 侧栏 + 自适应主区)。
      className="grid min-h-screen grid-cols-1 bg-background md:grid-cols-[18rem_minmax(0,1fr)]"
      data-layout="desktop-two-column"
      data-slot="app-shell"
    >
      {/* D4-T5:桌面分支——静态侧栏仅 md 及以上可见;移动端隐藏,
          改由下方抽屉承担。 */}
      <div className="hidden md:block">
        <SessionSidebar />
      </div>

      {/* D4-T5:移动端分支——抽屉只在 sidebarOpen 时渲染(SSR 初始态
          不输出)。遮罩 z-30 铺满视口,点击即收起;抽屉 z-40 贴左全高
          (w-72 与桌面列宽一致),内含同一个 SessionSidebar。 */}
      {sidebarOpen ? (
        <>
          <div
            aria-hidden
            className="fixed inset-0 z-30 bg-black/40"
            data-slot="sidebar-overlay"
            onClick={() => setSidebarOpen(false)}
          />
          <div className="fixed inset-y-0 left-0 z-40 w-72">
            {/* D4-T5:选中会话后自动收起抽屉(方案 A:容器可选回调)。 */}
            <SessionSidebar onSessionSelected={() => setSidebarOpen(false)} />
          </div>
        </>
      ) : null}

      <section className="flex min-w-0 flex-col" data-slot="conversation-area">
        {/* D4-T5:汉堡按钮仅移动端可见(md:hidden),位于顶栏左侧;抽屉
            打开后遮罩(固定全屏)会盖住它,关闭走遮罩/Esc/选中会话。 */}
        <header className="flex items-center justify-between border-b border-border px-4 py-4 md:px-8">
          <div className="flex items-center gap-3">
            <button
              aria-label="打开会话侧栏"
              className="inline-flex size-9 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground md:hidden"
              data-slot="sidebar-toggle"
              onClick={() => setSidebarOpen(true)}
              type="button"
            >
              <Menu aria-hidden className="size-5" />
            </button>
            <div>
              <p className="text-caption font-medium text-primary">阶段三 · 协作工作台</p>
              <h2 className="text-title font-semibold text-foreground">对话区</h2>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <div className="flex items-center gap-2 text-caption text-muted-foreground">
              {apiConnected ? (
                <CircleCheck aria-hidden className="size-4 text-emerald-600" />
              ) : (
                <CircleX aria-hidden className="size-4 text-destructive" />
              )}
              <span>{apiConnected ? "后端已连接" : "后端暂不可用"}</span>
            </div>
            {/* D4-T6:主题切换按钮。图标用 CSS 类驱动(亮色显月亮/
                暗色显太阳,next-themes 内联脚本在 hydration 前设置
                html 的 dark 类,CSS 即时生效——无 JS 状态、无闪烁、
                不触发 react-hooks 的 effect setState lint);aria-label
                与 onClick 用 resolvedTheme(SSR 首帧 undefined 显示
                「切换到暗色模式」,hydration 后校准)。 */}
            <button
              aria-label={resolvedTheme === "dark" ? "切换到亮色模式" : "切换到暗色模式"}
              className="inline-flex size-9 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground"
              data-slot="theme-toggle"
              onClick={() => setTheme(resolvedTheme === "dark" ? "light" : "dark")}
              type="button"
            >
              <Moon aria-hidden className="size-5 dark:hidden" />
              <Sun aria-hidden className="hidden size-5 dark:block" />
            </button>
          </div>
        </header>

        {currentSessionId ? (
          <ConversationPanel />
        ) : (
          <div className="flex flex-1 items-center justify-center p-8">
            <div className="max-w-md text-center">
              <p className="text-title font-semibold text-foreground">请选择或新建会话</p>
              <p className="mt-3 text-body text-muted-foreground">
                从左侧创建一个会话，或选择已有会话后开始对话。
              </p>
            </div>
          </div>
        )}
      </section>
    </main>
  );
}
