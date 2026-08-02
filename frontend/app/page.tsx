import { CircleCheck, CircleX } from "lucide-react";

import { Button } from "@/components/ui/button";
import type { paths } from "@/contracts/api.generated";
import { apiBaseUrl } from "@/lib/api-base-url";

export const dynamic = "force-dynamic";

type HealthzResponse = paths["/healthz"]["get"]["responses"][200]["content"]["application/json"];

async function readHealth(): Promise<HealthzResponse | null> {
  try {
    const response = await fetch(`${apiBaseUrl}/healthz`, { cache: "no-store" });
    if (!response.ok) {
      return null;
    }
    return (await response.json()) as HealthzResponse;
  } catch {
    return null;
  }
}

export default async function Home() {
  const health = await readHealth();
  const connected = health?.status === "ok";

  return (
    <main className="mx-auto flex min-h-screen max-w-3xl items-center px-6 py-16">
      <section className="w-full rounded-xl border border-border bg-card p-8 shadow-sm">
        <p className="text-sm font-medium text-primary">阶段三 · 前端骨架</p>
        <h1 className="mt-3 text-3xl font-semibold tracking-tight text-foreground">
          协作式 Agent 工作台
        </h1>
        <p className="mt-3 max-w-xl text-sm leading-6 text-muted-foreground">
          Next.js App Router、Tailwind 与 shadcn/ui 基线已经就绪。会话侧栏、消息流与输入区将在后续 W1
          任务中逐步接入。
        </p>

        <div className="mt-8 flex flex-wrap items-center gap-3 rounded-lg border border-border bg-muted/40 p-4 text-sm">
          {connected ? (
            <CircleCheck aria-hidden className="size-5 text-emerald-600" />
          ) : (
            <CircleX aria-hidden className="size-5 text-destructive" />
          )}
          <span className="font-medium text-foreground">
            后端健康检查：{connected ? "已连接" : "暂不可用"}
          </span>
          <span className="text-muted-foreground">{apiBaseUrl}/healthz</span>
        </div>

        <div className="mt-6">
          <Button type="button" variant="outline" disabled>
            W1 对话界面待接入
          </Button>
        </div>
      </section>
    </main>
  );
}
