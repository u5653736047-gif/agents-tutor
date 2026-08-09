"use client";

import { LoaderCircle, ShieldAlert, Terminal } from "lucide-react";
import { useEffect, useRef } from "react";

import { AgentBadge } from "@/components/agent-badge";
import { Button } from "@/components/ui/button";
import type { components } from "@/contracts/api.generated";

type PendingToolApproval = components["schemas"]["PendingToolApproval"];

export type TerminalApprovalCardProps = {
  errorMessage?: string | null;
  isDeciding: boolean;
  onDecide: (action: "confirm" | "reject") => void;
  pending: PendingToolApproval | null;
};

function textArgument(
  arguments_: Record<string, unknown>,
  name: string,
  fallback: string,
): string {
  const value = arguments_[name];
  return typeof value === "string" ? value : fallback;
}

export function TerminalApprovalCard({
  errorMessage,
  isDeciding,
  onDecide,
  pending,
}: TerminalApprovalCardProps) {
  const cardRef = useRef<HTMLElement>(null);
  useEffect(() => {
    if (pending) {
      cardRef.current?.focus();
    }
  }, [pending]);

  if (!pending) {
    return null;
  }

  const { request } = pending;
  const command = textArgument(request.arguments, "command", "（未提供命令）");
  const cwd = textArgument(request.arguments, "cwd", ".");
  const description = textArgument(request.arguments, "description", "");
  const rawTimeout = request.arguments.timeout_seconds;
  const timeout = typeof rawTimeout === "number" ? rawTimeout : null;

  return (
    <section
      aria-live="polite"
      className="overflow-hidden rounded-lg border border-warning/40 bg-card animate-in fade-in-0 slide-in-from-bottom-1 duration-[var(--app-duration-normal)] ease-[var(--app-ease-out)]"
      data-slot="terminal-approval-card"
      ref={cardRef}
      tabIndex={-1}
    >
      <header className="flex items-center gap-2 border-b border-border px-4 py-2">
        <Terminal aria-hidden className="size-4 text-warning" />
        <h3 className="text-caption font-medium text-foreground">
          命令执行审批
        </h3>
        <div className="ml-auto">
          <AgentBadge agent={request.agent_role} />
        </div>
      </header>

      <div className="space-y-3 px-4 py-3">
        {description ? (
          <p className="text-body text-foreground">{description}</p>
        ) : null}
        <div>
          <p className="text-caption font-medium text-muted-foreground">
            完整命令
          </p>
          <pre className="mt-1 max-h-56 overflow-auto whitespace-pre-wrap break-all rounded-md bg-muted/70 p-3 font-mono text-caption text-foreground">
            {command}
          </pre>
        </div>
        <dl className="grid gap-1 text-caption text-muted-foreground sm:grid-cols-[5rem_1fr]">
          <dt>工作目录</dt>
          <dd className="break-all font-mono text-foreground/80">{cwd}</dd>
          {timeout !== null ? (
            <>
              <dt>超时时间</dt>
              <dd>{timeout} 秒</dd>
            </>
          ) : null}
        </dl>
        <div className="flex gap-2 rounded-md border border-warning/30 bg-warning/10 p-3 text-caption text-foreground/85">
          <ShieldAlert aria-hidden className="mt-0.5 size-4 shrink-0 text-warning" />
          <p>
            命令将以后端服务进程账号权限运行。批准前请核对完整命令与工作目录；
            工作区限制只校验启动目录，不是操作系统级沙箱。
          </p>
        </div>
      </div>

      {errorMessage ? (
        <p className="px-4 pb-2 text-caption text-destructive" role="alert">
          {errorMessage}
        </p>
      ) : null}

      <footer className="flex items-center gap-2 border-t border-border px-4 py-3">
        <Button
          data-slot="terminal-reject"
          disabled={isDeciding}
          type="button"
          variant="outline"
          onClick={() => onDecide("reject")}
        >
          拒绝
        </Button>
        <Button
          data-slot="terminal-confirm"
          disabled={isDeciding}
          type="button"
          onClick={() => onDecide("confirm")}
        >
          批准并运行
        </Button>
        {isDeciding ? (
          <span className="ml-auto flex items-center gap-1.5 text-caption text-muted-foreground">
            <LoaderCircle aria-hidden className="size-3.5 animate-spin" />
            正在执行…
          </span>
        ) : null}
      </footer>
    </section>
  );
}
