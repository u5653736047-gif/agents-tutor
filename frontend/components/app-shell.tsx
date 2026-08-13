"use client";

import {
  ArrowUpRight,
  BookOpen,
  CircleCheck,
  CircleX,
  FolderPlus,
  Menu,
  Moon,
  Sparkles,
  Sun,
} from "lucide-react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useTheme } from "next-themes";
import {
  useCallback,
  useEffect,
  useRef,
  useSyncExternalStore,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";

import { ConversationPanel } from "@/components/conversation-panel";
import { SessionSidebar } from "@/components/session-sidebar";
import { Skeleton } from "@/components/ui/skeleton";
import { WorkspaceDialog, type WorkspaceDialogMode } from "@/components/workspace-dialog";
import {
  ASSISTANT_UI_ENV_DEFAULT,
  isAssistantUiEnabled,
  subscribeAssistantUiFlag,
} from "@/lib/feature-flags";
import { apiBaseUrl } from "@/lib/api-base-url";
import { isOnboardingSeen, markOnboardingSeen, subscribeOnboarding } from "@/lib/onboarding";
import {
  COLLAPSED_SIDEBAR_WIDTH,
  DEFAULT_SIDEBAR_WIDTH,
  MAX_SIDEBAR_WIDTH,
  MIN_SIDEBAR_WIDTH,
  resizeSidebarWidth,
  sidebarWidthForKey,
} from "@/lib/sidebar-layout";
import { useChatStore } from "@/stores/chat-store";

// assistant-ui 接入(T4):新渲染路径按需加载——动态导入保证 assistant-ui
// 代码进独立 async chunk,不增首屏体积(T1 预算门禁);灰度关闭时本分支
// 完全不加载。loading 骨架与消息气泡结构对齐(徽章 + 两行文本占位)。
const AssistantThread = dynamic(
  () => import("@/components/assistant-ui/assistant-thread"),
  {
    ssr: false,
    loading: () => (
      <div className="flex min-h-0 flex-1 flex-col" data-slot="assistant-thread-loading">
        <div className="mx-auto flex w-full max-w-4xl flex-1 flex-col gap-5 px-4 py-8 md:px-8">
          <div className="max-w-[90%] rounded-2xl rounded-bl-md border border-border/70 bg-card/80 px-5 py-4 shadow-sm md:max-w-[85%]">
            <div className="flex items-center gap-2">
              <Skeleton className="size-5 rounded-full" />
              <Skeleton className="h-4 w-16" />
            </div>
            <div className="mt-3 space-y-2">
              <Skeleton className="h-3 w-full" />
              <Skeleton className="h-3 w-4/5" />
            </div>
          </div>
        </div>
      </div>
    ),
  },
);

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
  const addWorkspaceRoot = useChatStore((state) => state.addWorkspaceRoot);
  const currentSessionId = useChatStore((state) => state.currentSessionId);
  const createSession = useChatStore((state) => state.createSession);
  const sessions = useChatStore((state) => state.sessions);
  const streamSendMessage = useChatStore((state) => state.streamSendMessage);
  const [workspaceDialogMode, setWorkspaceDialogMode] =
    useState<WorkspaceDialogMode | null>(null);

  const [sidebarWidth, setSidebarWidth] = useState(DEFAULT_SIDEBAR_WIDTH);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const resizeStartRef = useRef<{
    pointerId: number;
    pointerX: number;
    width: number;
  } | null>(null);
  const activeSidebarWidth = sidebarCollapsed
    ? COLLAPSED_SIDEBAR_WIDTH
    : sidebarWidth;
  const shellStyle = {
    "--sidebar-width": `${activeSidebarWidth}px`,
  } as CSSProperties;
  const currentSession = sessions.find(
    (session) => session.session_id === currentSessionId,
  );
  const currentSessionTitle = currentSessionId
    ? currentSession?.title?.trim() || "未命名会话"
    : "开始新的学习";
  const recentWorkspaceRoots = Array.from(
    new Set(
      sessions
        .map((session) => session.workspace_root)
        .filter((root): root is string => Boolean(root)),
    ),
  );

  const confirmWorkspace = async (path: string) => {
    if (workspaceDialogMode === "add") {
      return (await addWorkspaceRoot(path)) !== null;
    }
    return (await createSession(path)) !== null;
  };

  const handleResizePointerDown = (
    event: ReactPointerEvent<HTMLDivElement>,
  ) => {
    event.preventDefault();
    resizeStartRef.current = {
      pointerId: event.pointerId,
      pointerX: event.clientX,
      width: sidebarWidth,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
  };

  const handleResizePointerMove = (
    event: ReactPointerEvent<HTMLDivElement>,
  ) => {
    const start = resizeStartRef.current;
    if (!start || start.pointerId !== event.pointerId) return;
    setSidebarWidth(
      resizeSidebarWidth(start.width, start.pointerX, event.clientX),
    );
  };

  const finishResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (resizeStartRef.current?.pointerId !== event.pointerId) return;
    resizeStartRef.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  };

  const handleResizeKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    const nextWidth = sidebarWidthForKey(sidebarWidth, event.key);
    if (nextWidth === sidebarWidth) return;
    event.preventDefault();
    setSidebarWidth(nextWidth);
  };

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

  // assistant-ui 接入(T4):渲染路径开关——三快照模式复刻上方 onboarding
  // 先例:服务端快照恒为 env 默认(SSR/客户端首帧一致,无 hydration
  // mismatch);localStorage 覆盖在 hydration 后经订阅生效(灰度/回滚操作)。
  const assistantUiEnabled = useSyncExternalStore(
    subscribeAssistantUiFlag,
    isAssistantUiEnabled,
    () => ASSISTANT_UI_ENV_DEFAULT,
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

  // D4-T6 修订(hydration 修复):主题切换。图标仍用 CSS 类驱动(亮色显
  // 月亮/暗色显太阳,next-themes 内联脚本在 hydration 前设置 html 的
  // dark 类,CSS 即时生效——无 JS 状态、无闪烁);但 aria-label 不能直接
  // 读 resolvedTheme:SSR 首帧 resolvedTheme 恒 undefined(三元走假分支
  // 输出「切换到暗色模式」),暗色系统用户的客户端首帧解析为 dark 走真
  // 分支输出「切换到亮色模式」→ 服务端/客户端首帧属性不一致,
  // React 报 hydration mismatch。改用 mounted 门控(useSyncExternalStore
  // 的「服务端快照 + 客户端快照」模式,与 D5-T4 引导标记同一先例;
  // 不用 effect 内 setMounted——react-hooks 的 set-state-in-effect 拦截
  // 同步 setState):SSR 与客户端首帧都取服务端快照(false)渲染固定值
  // 「切换到暗色模式」,hydration 完成后切真实值。onClick 保留读
  // resolvedTheme 的原始表达式(点击必然发生在 hydration 之后,mounted
  // 已为 true,行为与修复前一致)。
  const mounted = useSyncExternalStore(
    () => () => {},
    () => true,
    () => false,
  );
  const { resolvedTheme, setTheme } = useTheme();
  const isDark = mounted && resolvedTheme === "dark";

  // D8 修复:后端连接状态自愈——apiConnected 是 SSR 时的一次性健康探测
  // 快照,后端在页面打开后才就绪(如 docker compose 先起前端、后端尚在
  // 启动,或后端中途重启恢复)时,徽章会永久停留在「后端暂不可用」且无
  // 重探机制。这里仅当当前未连接时每 10s 客户端重探 /healthz,恢复后
  // 停止轮询;已连接时不产生额外请求(运行中掉线由各接口的 requestError
  // 通道呈现,不重复打扰)。setState 全部发生在 await 后的异步回调里,
  // 不触发 react-hooks 的 set-state-in-effect 规则。
  const [connected, setConnected] = useState(apiConnected);

  useEffect(() => {
    if (connected) {
      return;
    }
    const timer = setInterval(() => {
      void (async () => {
        try {
          const response = await fetch(`${apiBaseUrl}/healthz`, { cache: "no-store" });
          if (response.ok && (await response.json()).status === "ok") {
            setConnected(true);
          }
        } catch {
          // 后端仍不可达:保持「后端暂不可用」,下一轮再试
        }
      })();
    }, 10_000);
    return () => clearInterval(timer);
  }, [connected]);

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
      // 移动端单栏；桌面列宽由 --sidebar-width 驱动，拖拽/收起时主区
      // 使用同一个 Grid 自动联动，不需要手工计算内容宽度。
      // UX-20260808#2:min-h-screen → h-dvh + overflow-hidden——「最小一屏」
      // 会让整页随消息撑高、浏览器滚动 body(侧栏被一起顶走);固定视口
      // 高度后,侧栏会话列表与消息区各自成为独立滚动容器(两者内部均有
      // flex-1 overflow-y-auto,此前因祖先无高度上限从未生效)。dvh 兼容
      // 移动端浏览器地址栏伸缩。
      className="grid h-dvh grid-cols-1 overflow-hidden bg-background md:grid-cols-[var(--sidebar-width)_minmax(0,1fr)]"
      data-layout="desktop-two-column"
      data-slot="app-shell"
      style={shellStyle}
    >
      {/* D4-T5:桌面分支——静态侧栏仅 md 及以上可见;移动端隐藏,
          改由下方抽屉承担。 */}
      <div className="relative hidden min-w-0 md:block" data-slot="desktop-sidebar">
        <SessionSidebar
          collapsed={sidebarCollapsed}
          onCreateSession={() => setWorkspaceDialogMode("create")}
          onToggleCollapsed={() => setSidebarCollapsed((value) => !value)}
        />
        {!sidebarCollapsed ? (
          <div
            aria-label="调整会话侧栏宽度"
            aria-orientation="vertical"
            aria-valuemax={MAX_SIDEBAR_WIDTH}
            aria-valuemin={MIN_SIDEBAR_WIDTH}
            aria-valuenow={sidebarWidth}
            className="group absolute inset-y-0 -right-1 z-20 w-2 cursor-col-resize touch-none outline-none"
            data-slot="sidebar-resizer"
            onDoubleClick={() => setSidebarWidth(DEFAULT_SIDEBAR_WIDTH)}
            onKeyDown={handleResizeKeyDown}
            onPointerCancel={finishResize}
            onPointerDown={handleResizePointerDown}
            onPointerMove={handleResizePointerMove}
            onPointerUp={finishResize}
            role="separator"
            tabIndex={0}
            title="拖拽调整宽度，双击恢复默认"
          >
            <span className="absolute inset-y-0 left-1/2 w-px -translate-x-1/2 bg-transparent transition-colors group-hover:bg-primary/50 group-focus-visible:bg-primary" />
          </div>
        ) : null}
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
            className="fixed inset-y-0 left-0 z-40 w-[min(20rem,calc(100vw-2rem))] animate-in fade-in-0 slide-in-from-left-2 duration-[var(--app-duration-normal)] ease-[var(--app-ease-out)]"
            data-slot="sidebar-drawer"
            ref={drawerRef}
            tabIndex={-1}
          >
            {/* D4-T5:选中会话后自动收起抽屉(方案 A:容器可选回调)。
                D5-T5:走 closeDrawer,焦点归还汉堡按钮。 */}
            <SessionSidebar
              onClose={closeDrawer}
              onCreateSession={() => {
                closeDrawer();
                setWorkspaceDialogMode("create");
              }}
              onSessionSelected={closeDrawer}
            />
          </div>
        </>
      ) : null}

      {/* UX-20260808#2:h-dvh 与主容器对齐——消息区(ConversationPanel 内
          flex-1 overflow-y-auto)以本 section 为高度上限独立滚动;
          min-w-0 保留(grid 子项横向防撑破)。 */}
      <section className="flex h-dvh min-w-0 flex-col" data-slot="conversation-area">
        {/* D4-T5:汉堡按钮仅移动端可见(md:hidden),位于顶栏左侧;抽屉
            打开后遮罩(固定全屏)会盖住它,关闭走遮罩/Esc/选中会话。 */}
        <header className="flex min-h-16 items-center justify-between border-b border-border/70 bg-card/50 px-4 py-3 backdrop-blur md:px-6">
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
              <h2 className="max-w-[min(50vw,32rem)] truncate text-body font-semibold text-foreground">
                {currentSessionTitle}
              </h2>
              <p className="text-caption text-muted-foreground">多智能体学习空间</p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            {currentSession?.workspace_root ? (
              <button
                aria-label="添加工作空间授权目录"
                className="hidden max-w-64 items-center gap-2 rounded-lg border border-border/70 bg-background/60 px-3 py-1.5 text-caption text-muted-foreground transition-colors hover:border-primary/35 hover:text-foreground xl:flex"
                onClick={() => setWorkspaceDialogMode("add")}
                title={`${currentSession.workspace_root} · 添加授权目录`}
                type="button"
              >
                <FolderPlus aria-hidden className="size-4 shrink-0 text-primary" />
                <span className="truncate">{currentSession.workspace_root}</span>
              </button>
            ) : null}
            {/* D6-T4:知识库检索测试入口(教师端)——独立页面 /knowledge,
                不经过主会话 store;小链接样式与顶栏辅助文案一致 */}
            <Link
              // UX-20260807#4:顶栏入口 hover 品牌化(交互热区变蓝)
              className="text-caption text-muted-foreground hover:text-primary inline-flex h-9 items-center gap-2 rounded-lg px-3 transition-colors hover:bg-muted"
              data-slot="knowledge-link"
              href="/knowledge"
            >
              <BookOpen aria-hidden className="size-4" />
              知识库
            </Link>
            {/* D6-T7:学习进度入口——独立页面 /stats(基础统计版),与
                知识库入口并列,同样不经过主会话 store */}
            <Link
              // UX-20260807#4:顶栏入口 hover 品牌化(交互热区变蓝)
              className="text-caption text-muted-foreground hover:text-primary inline-flex h-9 items-center rounded-lg px-3 transition-colors hover:bg-muted"
              data-slot="stats-link"
              href="/stats"
            >
              进度
            </Link>
            <div className="hidden items-center gap-2 rounded-full border border-border/70 bg-background/60 px-3 py-1.5 text-caption text-muted-foreground lg:flex">
              {connected ? (
                <CircleCheck aria-hidden className="size-4 text-success" />
              ) : (
                <CircleX aria-hidden className="size-4 text-destructive" />
              )}
              <span>{connected ? "后端已连接" : "后端暂不可用"}</span>
            </div>
            {/* D4-T6:主题切换按钮。图标用 CSS 类驱动(亮色显月亮/
                暗色显太阳,next-themes 内联脚本在 hydration 前设置
                html 的 dark 类,CSS 即时生效——无 JS 状态、无闪烁、
                不触发 react-hooks 的 effect setState lint);aria-label
                走 mounted 门控:未 mounted(SSR 首帧与客户端首帧)渲染
                与 SSR 一致的固定值「切换到暗色模式」,mounted 后再按
                resolvedTheme 校准——修复「SSR 输出暗色、客户端首帧
                亮色」导致的 hydration 属性不匹配(根因见上方注释)。 */}
            <button
              aria-label={isDark ? "切换到亮色模式" : "切换到暗色模式"}
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

        {/* assistant-ui 接入(T4):渲染路径唯一切换点——开关开 =
            AssistantThread(动态加载),关 = 旧 ConversationPanel(封存基线,
            行为零变化)。除此条件表达式外本文件无任何 assistant-ui 耦合。 */}
        {currentSessionId ? (
          assistantUiEnabled ? <AssistantThread /> : <ConversationPanel />
        ) : (
          <div className="flex flex-1 items-center justify-center overflow-y-auto px-4 py-10 md:px-8">
            <div className="w-full max-w-2xl">
              <div className="text-center">
                <div className="mx-auto flex size-12 items-center justify-center rounded-2xl bg-primary/10 text-primary ring-1 ring-primary/15">
                  <Sparkles aria-hidden className="size-5" />
                </div>
                <h2 className="mt-5 text-display font-semibold tracking-tight text-foreground">
                  今天想学点什么？
                </h2>
                <p className="mx-auto mt-3 max-w-lg text-body text-muted-foreground">
                  提出问题后，主智能体会按需调用助教、助学与评价智能体，并整合成一份完整回答。
                </p>
              </div>

              {/* 示例问题始终展示，点击后创建会话并立即开始流式回答。 */}
              <section className="mt-8" data-slot="example-questions">
                <p className="text-caption font-medium text-muted-foreground">从一个示例开始</p>
                <div className="mt-3 grid gap-3 sm:grid-cols-2">
                  {EXAMPLE_QUESTIONS.map((question) => (
                    <button
                      className="group flex min-h-16 items-center justify-between gap-3 rounded-xl border border-border/70 bg-card/70 px-4 py-3 text-left text-body text-foreground shadow-sm transition-[border-color,background-color,transform] hover:-translate-y-0.5 hover:border-primary/35 hover:bg-card"
                      data-slot="example-question"
                      key={question}
                      onClick={() => startExample(question)}
                      type="button"
                    >
                      <span>{question}</span>
                      <ArrowUpRight
                        aria-hidden
                        className="size-4 shrink-0 text-muted-foreground transition-colors group-hover:text-primary"
                      />
                    </button>
                  ))}
                </div>
              </section>

              {/* 首次使用说明降为轻量提示，不再与核心示例问题争夺视觉层级。 */}
              {onboardingSeen ? null : (
                <section
                  className="mt-6 rounded-xl border border-dashed border-border bg-muted/30 p-4"
                  data-slot="onboarding"
                >
                  <div className="flex items-start justify-between gap-4">
                    <div>
                      <p className="text-caption font-medium text-foreground">三步开始协作学习</p>
                      <ol className="mt-2 grid gap-1 text-caption text-muted-foreground sm:grid-cols-3 sm:gap-4">
                        <li>1. 选择示例或新建会话</li>
                        <li>2. 描述你的学习问题</li>
                        <li>3. 等待智能体整合答案</li>
                      </ol>
                    </div>
                    <button
                      className="shrink-0 rounded-lg px-2 py-1 text-caption text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                      data-slot="onboarding-skip"
                      onClick={markOnboardingSeen}
                      type="button"
                    >
                      知道了
                    </button>
                  </div>
                </section>
              )}
            </div>
          </div>
        )}
      </section>
      {workspaceDialogMode ? (
        <WorkspaceDialog
          key={workspaceDialogMode}
          mode={workspaceDialogMode}
          onClose={() => setWorkspaceDialogMode(null)}
          onConfirm={confirmWorkspace}
          open
          recentRoots={recentWorkspaceRoots}
        />
      ) : null}
    </main>
  );
}
