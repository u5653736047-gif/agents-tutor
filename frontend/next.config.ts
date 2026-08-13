import withBundleAnalyzer from "@next/bundle-analyzer";
import type { NextConfig } from "next";

// T1:bundle 分析器——仅在 ANALYZE=true 时启用,常规 dev/build 零开销。
// 用于 assistant-ui 接入的体积预算门禁(会话路由首屏增量 ≤120 KB gzipped,
// assistant-ui 代码必须进独立 async chunk)。
const bundleAnalyzer = withBundleAnalyzer({
  enabled: process.env.ANALYZE === "true",
});

const nextConfig: NextConfig = {
  // Playwright e2e:Next 16 dev server 默认拦截「跨源」dev 资源请求
  // (HMR/chunk 加载被阻,页面 JS 永不就绪)——本机回环地址加入白名单。
  // 仅影响 dev server,生产构建无此路径。
  allowedDevOrigins: ["127.0.0.1", "localhost"],
};

export default bundleAnalyzer(nextConfig);
