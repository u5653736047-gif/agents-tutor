import { defineConfig, devices } from "@playwright/test";

// ============================================================================
// D6-T8 E2E 自动化验收(Playwright)
//
// Mock 策略选型:前端路由拦截(page.route),不启动 FastAPI 替身。
// 理由:
// 1) 前端所有 API 调用(api-client / stream-client)都发往
//    NEXT_PUBLIC_API_BASE_URL,浏览器内 route 拦截即可按契约形状伪造
//    响应,无需第二个后端进程、无端口/生命周期管理;
// 2) 流式 SSE 同样可伪造:route.fulfill 按 stream-client 的解析格式
//    (data: {json}\n\n 帧序列)交付,覆盖 thinking / tool_call /
//    tool_result / message_end / done 全事件序列;
// 3) 契约以 contracts/api.generated.ts 为单一数据源(openapi-typescript
//    生成),mock 响应字段直接对照契约与 api-client 解析,不臆造。
//
// webServer 环境说明:首页 /healthz 由 Next 服务端 fetch 完成,
// page.route 拦不到服务端请求,因此把 NEXT_PUBLIC_API_BASE_URL 指向
// 不存在的 127.0.0.1:9999——首页健康徽章显示「后端暂不可用」,但
// E2E 断言不依赖该徽章;浏览器端 API 请求全部被 route mock 兜住,
// 主流程与真实后端无关。
// REAL_E2E=1(真实 DeepSeek 冒烟,手动执行)时不覆盖该变量,
// dev server 继承系统环境走真实后端(默认 127.0.0.1:8000)。
// ============================================================================

export default defineConfig({
  testDir: "./e2e",
  // 教学项目串行执行:mocks.ts 有模块级可变状态(会话/消息内存),
  // 串行保证用例间隔离,也避免并行 dev server 编译抖动
  fullyParallel: false,
  retries: 0,
  timeout: 30_000,
  use: {
    baseURL: "http://127.0.0.1:3000",
    // 失败时保留 trace,便于主线程排查(成功不产生 trace)
    trace: "retain-on-failure",
  },
  webServer: {
    command: "npm run dev",
    url: "http://127.0.0.1:3000",
    reuseExistingServer: true,
    env:
      process.env.REAL_E2E === "1"
        ? {}
        : { NEXT_PUBLIC_API_BASE_URL: "http://127.0.0.1:9999" },
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
