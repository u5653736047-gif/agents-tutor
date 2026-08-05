import type { Metadata } from "next";
import type { ReactNode } from "react";
import { ThemeProvider } from "next-themes";

import "./globals.css";

export const metadata: Metadata = {
  title: "协作式 Agent",
  description: "多 Agent 协作对话工作台",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: ReactNode;
}>) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <body>
        {/* D4-T6:主题提供器。attribute="class" 使暗色类落在 <html> 上,
            defaultTheme="system" 让首屏跟随 prefers-color-scheme;
            next-themes 内联脚本在 hydration 前设置主题,避免闪烁
            (SSR 首屏与客户端初始一致)。 */}
        <ThemeProvider attribute="class" defaultTheme="system" enableSystem>
          {children}
        </ThemeProvider>
      </body>
    </html>
  );
}
