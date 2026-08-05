"use client";

import { CircleCheck, CircleX, Menu, Moon, Sun } from "lucide-react";
import Link from "next/link";
import { useTheme } from "next-themes";
import { useCallback, useEffect, useRef, useSyncExternalStore, useState } from "react";

import { ConversationPanel } from "@/components/conversation-panel";
import { SessionSidebar } from "@/components/session-sidebar";
import { isOnboardingSeen, markOnboardingSeen, subscribeOnboarding } from "@/lib/onboarding";
import { useChatStore } from "@/stores/chat-store";

// D5-T4:空态示例问题——点击后按「建会话 → 流式提问」时序快速开始,
// 不依赖引导标记,新老用户始终可见。
const EXAMPLE_QUESTIONS = [
  "用通俗方式讲解反向传播",
  "如何规划一条机器学习学习路径",
  "对比卷积神经网络与全连接网络",
  "什么是注意力机制",
];

type AppShellProps = {
  apiConnected: boolean;
};

export function AppShell({ apiConnected }: AppShellProps) {
  const currentSessionId = useChatStore((state) => state.currentSessionId);
  const createSession = useChatStore((state) => state.createSession);
  const streamSendMessage = useChatStore((state) => state.streamSendMessage);

  // D5-T4:首次引导「已看过」标记——useSyncExternalStore 读 localStorage 外部
  // 存储(react-hooks lint 拦截 effect 内 setState,mounted 模式不可用,先例
  // 见 D4-T6)。getServerSnapshot 恒 false:SSR 首帧与客户端首帧都渲染引导,
  // 无 hydration mismatch;hydration 后若本地已有标记,订阅者收到变更重渲染
  // 隐藏引导(React 官方「服务端默认值 + 客户端真实值」模式)。
  const onboardingSeen = useSyncExternalStore(
    subscribeOnboarding,
    isOnboardingSeen,
    () => false,
  );

  // D5-T4:示例问题点击时序——先 await createSession()(成功时 currentSessionId
  // 已就位:chat-store 的 createSession 在 await 返回前同步 set),再流式发送
  // 问题。失败时 createSession 返回 null 并已写入 requestError(不抛错),此时
  // 跳过发送,避免无会话的守卫文案覆盖真实失败原因。
  const startExample = (question: string) => {
    void (async () => {
      const session = await createSession();
      if (session) {
        void streamSendMessage(question);
      }
    })();
  };

  // D4-T5:移动端抽屉状态。初始 false,SSR 首屏不渲染抽屉/遮罩,
  // 开合全部发生在客户端交互之后(SSR 安全)。
  const [sidebarOpen, setSidebarOpen] = useState(false);

  // D5-T5:焦点管理引用——drawerRef 指向抽屉容器(tabIndex=-1 使其可聚焦,
  // 打开时焦点移入);toggleRef 指向汉堡按钮(关闭时焦点归还)。两者均为
  // 稳定引用,useCallback 空依赖安全(见 closeDrawer)。
  const drawerRef = useRef<HTMLDivElement>(null);
  const toggleRef = useRef<HTMLButtonElement>(null);

  // D5-T5:关闭抽屉统一入口——遮罩点击/选中会话/Esc 全部走这里,
  // setState + 焦点归还汉堡按钮单点收敛(不再散落 setSidebarOpen(false))。
  // useCallback 空依赖:内部只引用 setSidebarOpen 与 toggleRef(均稳定),
  // 符合 exhaustive-deps;回调内访问 ref 的 .current 是调用期行为,不违反
  // react-hooks 的「渲染期不得访问 ref」规则。
  const closeDrawer = useCallback(() => {
    setSidebarOpen(false);
    toggleRef.current?.focus();
  }, []);

  // D5-T5:抽屉打开时焦点移入抽屉容器。effect 内只做 DOM 焦点同步
  // (focus()),不 setState——react-hooks lint 拦截 effect 内 setState,
  // 但「与外部系统同步(焦点/滚动)」的 DOM 操作是合法用法。
  // 完整焦点陷阱(Tab 循环限制在抽屉内)未实现:侧栏场景下焦点移入 +
  // 遮罩 aria-hidden + 关闭归还已满足键盘可操作需求,完整 trap 留给
  // 真正的模态对话框场景(验收口径:进入 + 归还)。
  useEffect(() => {
    if (sidebarOpen) {
      drawerRef.current?.focus();
    }
  }, [sidebarOpen]);

  // D4-T6:主题切换。图标 CSS 类驱动无需 mounted;aria-label/onClick
  // 直接读 resolvedTheme(SSR 首帧 undefined,hydration 后校准)。
  const { resolvedTheme, setTheme } = useTheme();

  // D4-T5:抽屉打开期间注册 keydown 监听,Esc 关闭;关闭后移除监听。
  // D5-T5:Esc 走 closeDrawer(而非直接 setSidebarOpen),保证焦点归还
  // 与遮罩/选中会话路径一致;closeDrawer 为 useCallback 稳定引用,
  // 依赖数组 [sidebarOpen, closeDrawer] 完整且不随渲染变化。
  useEffect(() => {
    if (!sidebarOpen) {
      return;
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        closeDrawer();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [closeDrawer, sidebarOpen]);

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
          {/* D5-T2:进入动画——遮罩淡入(200ms,对齐 D5-T1 tokens);关闭时
              条件渲染即时卸载无法播放退出动画,保持简单不做(若要关闭动画
              需延迟卸载状态机,超出本任务范围;reduced-motion 下本就应无动画,
              由 globals.css 全局媒体查询关闭)。 */}
          <div
            aria-hidden
            className="fixed inset-0 z-30 animate-in fade-in-0 bg-black/40 duration-[var(--app-duration-normal)]"
            data-slot="sidebar-overlay"
            onClick={closeDrawer}
          />
          {/* D5-T2:抽屉从左侧滑入(tw-animate-css slide-in-from-left-2 = 8px,
              已验证该类存在),淡入 + 位移均为 transform/opacity 动效,
              不触发重排。 */}
          {/* D5-T5:抽屉容器 tabIndex={-1} 使其可被程序化聚焦(focus()),
              打开时焦点移入(见上方 effect);ref 持有供聚焦。 */}
          <div
            className="fixed inset-y-0 left-0 z-40 w-72 animate-in fade-in-0 slide-in-from-left-2 duration-[var(--app-duration-normal)] ease-[var(--app-ease-out)]"
            data-slot="sidebar-drawer"
            ref={drawerRef}
            tabIndex={-1}
          >
            {/* D4-T5:选中会话后自动收起抽屉(方案 A:容器可选回调)。
                D5-T5:走 closeDrawer,焦点归还汉堡按钮。 */}
            <SessionSidebar onSessionSelected={closeDrawer} />
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
              ref={toggleRef}
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
            {/* D6-T4:知识库检索测试入口(教师端)——独立页面 /knowledge,
                不经过主会话 store;小链接样式与顶栏辅助文案一致 */}
            <Link
              className="text-caption text-muted-foreground hover:text-foreground"
              data-slot="knowledge-link"
              href="/knowledge"
            >
              知识库
            </Link>
            {/* D6-T7:学习进度入口——独立页面 /stats(基础统计版),与
                知识库入口并列,同样不经过主会话 store */}
            <Link
              className="text-caption text-muted-foreground hover:text-foreground"
              data-slot="stats-link"
              href="/stats"
            >
              进度
            </Link>
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
          <div className="flex flex-1 flex-col items-center justify-center gap-8 overflow-y-auto p-8">
            <div className="max-w-md text-center">
              <p className="text-title font-semibold text-foreground">请选择或新建会话</p>
              <p className="mt-3 text-body text-muted-foreground">
                从左侧创建一个会话，或选择已有会话后开始对话。
              </p>
            </div>

            {/* D5-T4:示例问题卡——始终展示(不依赖 seen),点击即建会话并提问 */}
            <section
              className="w-full max-w-md rounded-lg border border-border bg-card p-5"
              data-slot="example-questions"
            >
              <p className="text-caption font-medium text-foreground">
                选择一个示例问题快速开始，或手动创建会话
              </p>
              <div className="mt-4 grid gap-2">
                {EXAMPLE_QUESTIONS.map((question) => (
                  <button
                    className="rounded-md border border-border px-4 py-2 text-left text-body text-foreground hover:bg-muted"
                    data-slot="example-question"
                    key={question}
                    onClick={() => startExample(question)}
                    type="button"
                  >
                    {question}
                  </button>
                ))}
              </div>
            </section>

            {/* D5-T4:首次使用三步引导——仅「未看过」时展示;跳过即写入本地标记,
                事件驱动 useSyncExternalStore 订阅者重渲染隐藏本卡 */}
            {onboardingSeen ? null : (
              <section
                className="w-full max-w-md rounded-lg border border-border bg-card p-5"
                data-slot="onboarding"
              >
                <p className="text-caption font-medium text-foreground">首次使用？三步开始</p>
                <ol className="mt-3 list-decimal space-y-2 pl-5 text-body text-muted-foreground">
                  <li>创建会话：点击左侧「新建会话」，或直接选一个示例问题</li>
                  <li>提问：在输入框描述你的问题，等待多智能体协作回答</li>
                  <li>查看审批与协作过程：审批卡片确认任务，协作面板看事件时间线</li>
                </ol>
                <button
                  className="mt-4 rounded-md border border-border px-3 py-1.5 text-caption text-muted-foreground hover:bg-muted hover:text-foreground"
                  data-slot="onboarding-skip"
                  onClick={markOnboardingSeen}
                  type="button"
                >
                  跳过引导
                </button>
              </section>
            )}
          </div>
        )}
      </section>
    </main>
  );
}
