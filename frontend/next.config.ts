import withBundleAnalyzer from "@next/bundle-analyzer";
import type { NextConfig } from "next";

// T1:bundle 分析器——仅在 ANALYZE=true 时启用,常规 dev/build 零开销。
// 用于 assistant-ui 接入的体积预算门禁(会话路由首屏增量 ≤120 KB gzipped,
// assistant-ui 代码必须进独立 async chunk)。
const bundleAnalyzer = withBundleAnalyzer({
  enabled: process.env.ANALYZE === "true",
});

const nextConfig: NextConfig = {};

export default bundleAnalyzer(nextConfig);
