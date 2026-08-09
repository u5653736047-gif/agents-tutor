"use client";

import { ArrowUp, Folder, FolderOpen, LoaderCircle, X } from "lucide-react";
import { useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import {
  apiClient,
  type ApiClient,
  type WorkspaceDirectoryListing,
} from "@/lib/api-client";

export type WorkspaceDialogMode = "create" | "add";

type WorkspaceDialogClient = Pick<
  ApiClient,
  "listWorkspaceDirectories" | "validateWorkspace"
>;

type WorkspaceDialogProps = {
  client?: WorkspaceDialogClient;
  mode: WorkspaceDialogMode;
  onClose: () => void;
  onConfirm: (path: string) => Promise<boolean>;
  open: boolean;
  recentRoots: readonly string[];
};

export function WorkspaceDialog({
  client = apiClient,
  mode,
  onClose,
  onConfirm,
  open,
  recentRoots,
}: WorkspaceDialogProps) {
  const [path, setPath] = useState(recentRoots[0] ?? "");
  const [listing, setListing] = useState<WorkspaceDirectoryListing | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isBrowsing, setIsBrowsing] = useState(false);
  const [isConfirming, setIsConfirming] = useState(false);

  if (!open) {
    return null;
  }

  const browse = async (nextPath?: string) => {
    setError(null);
    setIsBrowsing(true);
    try {
      const nextListing = await client.listWorkspaceDirectories(
        nextPath?.trim() || undefined,
      );
      setListing(nextListing);
      setPath(nextListing.path);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "无法读取该目录。");
    } finally {
      setIsBrowsing(false);
    }
  };

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const requestedPath = path.trim();
    if (!requestedPath) {
      setError("请输入工作空间的绝对路径。");
      return;
    }

    setError(null);
    setIsConfirming(true);
    try {
      const validated = await client.validateWorkspace(requestedPath);
      if (await onConfirm(validated.path)) {
        onClose();
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "无法使用该目录。");
    } finally {
      setIsConfirming(false);
    }
  };

  const title = mode === "create" ? "选择工作空间" : "添加授权目录";
  const description =
    mode === "create"
      ? "主智能体和子代理只会读取这个文件夹及你随后明确授权的目录。"
      : "将另一个文件夹以只读方式授权给当前会话。";

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-foreground/45 p-4 backdrop-blur-sm"
      data-slot="workspace-dialog-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section
        aria-labelledby="workspace-dialog-title"
        aria-modal="true"
        className="flex max-h-[min(46rem,calc(100dvh-2rem))] w-full max-w-2xl flex-col overflow-hidden rounded-2xl border border-border bg-card shadow-2xl"
        data-slot="workspace-dialog"
        role="dialog"
      >
        <div className="flex items-start justify-between gap-4 border-b border-border/70 px-5 py-4">
          <div>
            <h2
              className="flex items-center gap-2 text-body font-semibold text-foreground"
              id="workspace-dialog-title"
            >
              <FolderOpen aria-hidden className="size-5 text-primary" />
              {title}
            </h2>
            <p className="mt-1 text-caption text-muted-foreground">{description}</p>
          </div>
          <button
            aria-label="关闭工作空间选择"
            className="inline-flex size-8 shrink-0 items-center justify-center rounded-lg text-muted-foreground hover:bg-muted hover:text-foreground"
            onClick={onClose}
            type="button"
          >
            <X aria-hidden className="size-4" />
          </button>
        </div>

        <form className="flex min-h-0 flex-1 flex-col" onSubmit={submit}>
          <div className="space-y-4 overflow-y-auto px-5 py-4">
            <div>
              <label
                className="text-caption font-medium text-foreground"
                htmlFor="workspace-absolute-path"
              >
                文件夹绝对路径
              </label>
              <div className="mt-2 flex gap-2">
                <input
                  aria-label="工作空间绝对路径"
                  autoComplete="off"
                  className="min-w-0 flex-1 rounded-lg border border-border bg-background px-3 py-2 text-body text-foreground outline-none placeholder:text-muted-foreground focus:ring-2 focus:ring-primary/30"
                  id="workspace-absolute-path"
                  onChange={(event) => setPath(event.target.value)}
                  placeholder="例如 D:\\Projects\\course"
                  spellCheck={false}
                  value={path}
                />
                <Button
                  disabled={isBrowsing}
                  onClick={() => void browse(path)}
                  type="button"
                  variant="outline"
                >
                  {isBrowsing ? (
                    <LoaderCircle aria-hidden className="size-4 animate-spin" />
                  ) : (
                    <Folder aria-hidden className="size-4" />
                  )}
                  浏览
                </Button>
              </div>
              <p className="mt-2 text-caption text-muted-foreground">
                路径会由后端校验；未授权目录和越界路径仍会被安全策略拦截。
              </p>
            </div>

            {recentRoots.length > 0 ? (
              <div>
                <p className="text-caption font-medium text-foreground">最近使用</p>
                <div className="mt-2 flex flex-wrap gap-2">
                  {recentRoots.map((root) => (
                    <button
                      className="max-w-full truncate rounded-lg border border-border bg-background px-3 py-1.5 text-left text-caption text-muted-foreground hover:border-primary/35 hover:text-foreground"
                      key={root}
                      onClick={() => setPath(root)}
                      title={root}
                      type="button"
                    >
                      {root}
                    </button>
                  ))}
                </div>
              </div>
            ) : null}

            {listing ? (
              <div className="overflow-hidden rounded-xl border border-border">
                <div className="flex items-center gap-2 border-b border-border/70 bg-muted/40 px-3 py-2">
                  {listing.parent ? (
                    <button
                      aria-label="打开上级目录"
                      className="inline-flex size-7 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground"
                      onClick={() => void browse(listing.parent ?? undefined)}
                      type="button"
                    >
                      <ArrowUp aria-hidden className="size-4" />
                    </button>
                  ) : null}
                  <span className="min-w-0 truncate text-caption text-muted-foreground">
                    {listing.path}
                  </span>
                </div>
                <div className="max-h-64 overflow-y-auto p-1">
                  {(listing.directories ?? []).length > 0 ? (
                    listing.directories?.map((directory) => (
                      <button
                        className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-body text-foreground hover:bg-muted"
                        key={directory.path}
                        onClick={() => void browse(directory.path)}
                        type="button"
                      >
                        <Folder aria-hidden className="size-4 shrink-0 text-primary" />
                        <span className="truncate">{directory.name}</span>
                      </button>
                    ))
                  ) : (
                    <p className="px-3 py-4 text-caption text-muted-foreground">
                      此目录下没有可浏览的子目录。
                    </p>
                  )}
                </div>
              </div>
            ) : null}

            {error ? (
              <p className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-caption text-destructive" role="alert">
                {error}
              </p>
            ) : null}
          </div>

          <div className="flex justify-end gap-2 border-t border-border/70 px-5 py-4">
            <Button onClick={onClose} type="button" variant="outline">
              取消
            </Button>
            <Button disabled={isConfirming || path.trim().length === 0} type="submit">
              {isConfirming ? <LoaderCircle aria-hidden className="size-4 animate-spin" /> : null}
              {mode === "create" ? "使用此文件夹" : "添加到当前会话"}
            </Button>
          </div>
        </form>
      </section>
    </div>
  );
}
