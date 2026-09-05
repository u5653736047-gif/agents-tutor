"use client";

// S5-C1 决策 5/6：知识空间选择器（会话创建与上传表单共用）。
//
// 数据来自 GET /knowledge/namespaces 聚合端点；「＋ 新建空间」入口
// 解决自举死锁（选择器只能选已有空间 → 新空间永远无法创建）——
// 展开为带小写标识校验的文本输入，public 为保留值拒绝占用。
// 校验规则与后端一致：小写字母开头，只含小写字母/数字/连字符，
// 且不以连字符结尾（manifest source 标识规则的镜像实现）。
import { useEffect, useState } from "react";

import { apiClient } from "@/lib/api-client";

const NAMESPACE_PATTERN = /^[a-z]([a-z0-9]|-[a-z0-9])*$/;
const RESERVED_NAMESPACES = new Set(["public"]);

export function isValidNewNamespace(name: string): boolean {
  return (
    NAMESPACE_PATTERN.test(name) && !RESERVED_NAMESPACES.has(name)
  );
}

export function NamespaceSelector({
  value,
  onChange,
}: {
  value: string;
  onChange: (namespace: string) => void;
}) {
  const [namespaces, setNamespaces] = useState<string[]>([]);
  const [creating, setCreating] = useState(false);
  const [draft, setDraft] = useState("");
  const [draftError, setDraftError] = useState<string | null>(null);

  useEffect(() => {
    let ignore = false;
    apiClient
      .listNamespaces()
      .then((items) => {
        if (!ignore) {
          setNamespaces(items.map((item) => item.namespace));
        }
      })
      .catch(() => {
        // 加载失败静默降级：选择器仍含 public + 手动新建入口。
      });
    return () => {
      ignore = true;
    };
  }, []);

  const options = ["public", ...namespaces.filter((ns) => ns !== "public")];
  const known = options.includes(value);

  function commitDraft() {
    const name = draft.trim();
    if (!isValidNewNamespace(name)) {
      setDraftError("格式：小写字母开头，只含小写字母/数字/连字符");
      return;
    }
    if (!options.includes(name)) {
      setNamespaces((prev) => [...prev, name]);
    }
    onChange(name);
    setDraft("");
    setDraftError(null);
    setCreating(false);
  }

  return (
    <div data-slot="namespace-selector">
      {creating ? (
        <div className="flex flex-col gap-1">
          <input
            className="w-full rounded-md border border-border bg-background px-2 py-1.5 text-body text-foreground focus:border-primary focus:outline-none"
            data-slot="namespace-create-input"
            onChange={(event) => {
              setDraft(event.target.value);
              setDraftError(null);
            }}
            placeholder="如 ml-course-2026"
            value={draft}
          />
          {draftError ? (
            <p className="text-caption text-destructive" data-slot="namespace-create-error">
              {draftError}
            </p>
          ) : null}
          <div className="flex gap-2">
            <button
              className="rounded-md border border-border px-2 py-1 text-caption hover:border-primary"
              data-slot="namespace-create-cancel"
              onClick={() => {
                setCreating(false);
                setDraft("");
                setDraftError(null);
              }}
              type="button"
            >
              取消
            </button>
            <button
              className="rounded-md bg-primary px-2 py-1 text-caption text-primary-foreground"
              data-slot="namespace-create-confirm"
              onClick={commitDraft}
              type="button"
            >
              使用该空间
            </button>
          </div>
        </div>
      ) : (
        <div className="flex items-center gap-2">
          <select
            aria-label="课程空间"
            className="w-full rounded-md border border-border bg-background px-2 py-1.5 text-body text-foreground"
            data-slot="namespace-select"
            onChange={(event) => onChange(event.target.value)}
            value={value}
          >
            {!known ? (
              <option value={value}>{value}</option>
            ) : null}
            {options.map((ns) => (
              <option key={ns} value={ns}>
                {ns === "public" ? "public（公共教材库）" : ns}
              </option>
            ))}
          </select>
          <button
            className="shrink-0 rounded-md border border-dashed border-border px-2 py-1.5 text-caption text-muted-foreground hover:border-primary hover:text-primary"
            data-slot="namespace-create-toggle"
            onClick={() => setCreating(true)}
            type="button"
          >
            ＋ 新建空间
          </button>
        </div>
      )}
    </div>
  );
}
