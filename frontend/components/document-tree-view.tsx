"use client";

// S5-C2：文档结构树展示（章→节两级 + chunk 计数徽标；扁平页列表回退）。
// 纯展示组件（props 驱动），渲染逻辑独立于取数——便于 renderToStaticMarkup
// 直接测试（与 collaboration-panel 测试先例一致）。
import type { components } from "@/contracts/api.generated";

type TreeChapter = components["schemas"]["KnowledgeTreeChapterDto"];
type TreeResponse = components["schemas"]["KnowledgeDocumentTreeResponse"];

export function DocumentTreeView({ tree }: { tree: TreeResponse }) {
  const flatPages = tree.flat_pages ?? [];
  const chapters = tree.chapters ?? [];

  if (tree.kind === "flat") {
    return (
      <div data-slot="document-tree-flat">
        {flatPages.length === 0 ? (
          <p className="text-caption text-muted-foreground">该文档无可展示内容</p>
        ) : (
          <>
            <p className="text-caption text-muted-foreground">无章节结构，按页平铺：</p>
            <div className="mt-1 flex flex-wrap gap-1.5">
              {flatPages.map((page) => (
                <span
                  className="rounded border border-border px-1.5 py-0.5 text-caption text-muted-foreground"
                  data-slot="document-tree-page"
                  key={page}
                >
                  第 {page} 页
                </span>
              ))}
            </div>
          </>
        )}
      </div>
    );
  }
  return (
    <div data-slot="document-tree">
      {chapters.map((chapter) => (
        <div
          className="border-l-2 border-border/60 pl-3"
          data-slot="document-tree-chapter"
          key={chapter.chapter}
        >
          <p className="text-caption font-medium text-foreground">
            {chapter.chapter}
            <span
              className="ml-2 rounded bg-muted px-1.5 text-caption text-muted-foreground"
              data-slot="document-tree-chunk-badge"
            >
              {chapter.chunk_count}
            </span>
          </p>
          <ul className="ml-2 mt-1 space-y-0.5">
            {(chapter.sections ?? []).map((section) => {
              const tags = section.tags ?? [];
              return (
                <li
                  className="flex items-center gap-2 text-caption text-muted-foreground"
                  data-slot="document-tree-section"
                  key={section.section}
                >
                  <span>{section.section}</span>
                  <span
                    className="rounded bg-muted px-1.5"
                    data-slot="document-tree-section-badge"
                  >
                    {section.chunk_count}
                  </span>
                  {tags.length > 0 ? (
                    <span className="truncate">{tags.join(" / ")}</span>
                  ) : null}
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </div>
  );
}

export type { TreeChapter, TreeResponse };
