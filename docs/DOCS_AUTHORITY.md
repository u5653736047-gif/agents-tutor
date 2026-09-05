# 文档权威清单 — DOCS_AUTHORITY

> 生效：2026-08-26 — 防幻觉权威路由
> 原则：surgical，只动会致幻的文件。历史规划类保留原位隔离，不物理归档。横幅冻结零断链，移入 archive 必断 9 条活链，故除临时草稿外一律不移。

---

## 一、当前权威文档（新会话必读）

| 权威项 | 文件 | 口径 | 生效 |
|---|---|---|---|
| 赛题功能权威 | `docs/SIX_FEATURES_COMPETITION_MAPPING.md` | 六大核心功能 ↔ 赛题原文 ↔ 实现落点 ↔ 验收测试；范围唯一口径。自 2026-08-26 起为**范围权威** | 2026-08-26 |
| 里程碑权威 | `docs/TASKS_M3_CLOSE.md` | D1-D7 执行出口与门禁；阶段三收口调度。`149 行`已改为「已迁移，不再回写冻结文件」 | 2026-08-04 生成，2026-08-26 校准 |
| 阶段三细节权威 | `docs/TASKS_STAGE_3_DETAILS.md` | 阶段三 38 项全勾细节清单；`1053 行`门禁已校准 `744→979`，`D6-T8/D6-T9` 标 `[~]静态交付(待Linux CI实测)` | 2026-08-26 校准 |
| 基础设施权威 | `docs/EMBEDDING_SELECTION.md` 等 4 份 + `docs/perf-evidence/*` | 向量索引/混合检索/自适应 RAG 实现依据；`fts-benchmark-2026-08-23.md` 为 FTS 预滤基准 | 保留，无需改动 |

> 新会话检索"向量/自适应RAG/学习记录/知识库/命名空间/FTS"应命中 `SIX_FEATURES` / `M3_CLOSE` / `EMBEDDING_SELECTION`，而非 `TASK_BREAKDOWN_v2` 的"未开始"。

---

## 二、已冻结文档（禁止作为进度/范围权威）

| 文件 | 状态 | 冻结说明 | 替代指向 |
|---|---|---|---|
| `docs/TASK_BREAKDOWN_v2.md` | **已冻结 2026-08-26** | 历史范围归档，不再勾选。头部 `2026-08-02` 冻结，`51-57 行`进度总表「3 已完/14 部分/30 未开始」与 `TASKS_STAGE_3_DETAILS 38项全勾 + SIX_FEATURES 1074测试`完全脱节。高危幻觉源。禁止据此判断"47任务仅3完成/阶段四未开始"。`51-57 行`已加删除线，行尾注`[已冻结，实盘见 M3_CLOSE 出口检查 2026-08-26]`；`1.2.3/2.2.3/2.3.2` 已追加锚点 `→ store.py:1 / hybrid.py:75 / retrieval.py:adaptive_search` | 范围见 `SIX_FEATURES`，进度见 `M3_CLOSE` + `TASKS_STAGE_3_DETAILS` |
| `docs/TASKS_STAGE_1_2.md` | **局部冻结** | Sprint1-4 仍有效（历史保留），`Sprint5 S5-T1~T9` 已被 `SIX_FEATURES` + `officecli-integration-plan` + `EMBEDDING_SELECTION 5.2` 替代。`7-8 行`"总清单仍然是唯一的范围与进度权威"已改为删除线并追加`> 已冻结片段 — Sprint5见 SIX_FEATURES；S1-4历史保留` | Sprint5 见 `SIX_FEATURES` |
| `frontend/AGENT_NODE_IMPLEMENTATION.md` | **已冻结** | `2026-08-02`版，与 `backend/src/core/graph_builder.py:336 tool模式`脱节 | 图编排见 `graph_builder.py tool模式` + `SIX_FEATURES` |

**为何不物理归档**：移入 `docs/archive/` 会使 `TASKS_STAGE_1_2:7` / `BRIDGE:4` / `DETAILS:9` / `M3_CLOSE:149` 共 9 处相对链接 404，故保留原位加横幅隔离。

---

## 三、保留与隔离

| 文件 | 处置 | 说明 |
|---|---|---|
| `docs/TASKS_STAGE_3_BRIDGE.md` | E 保留 | 骨架已 `[x]`，`311 行`自声明细节在 DETAILS，无待办。`M3_CLOSE:141` 只读引用，无幻觉风险 |
| `docs/SIX_FEATURES_COMPETITION_MAPPING.md` | C 同步更新 | 头部日期已改 `2026-08-26` 并追加"自2026-08-26起为范围权威"；二.基础设施已追加 `命名空间/FTS预滤/知识树 f9628b3` 及 `0.97x低选择性回退` 引 `fts-benchmark-2026-08-23` |
| `docs/EMBEDDING_SELECTION.md` 等新鲜文档 + `perf-evidence` | E 保留 | `vector_index.py/hybrid.py` 已落地一致 |
| `frontend/DESIGN_SYSTEM.md` `HELP.md` | E 保留 | `2026-08-08/09` 新鲜有效 |
| `docs/superpowers/plans/*` `specs/*` | E 保留隔离 | `2026-08-02` 历史冻结，无待办，不参与当前执行链 |
| `docs/agent-streaming-ui-research/plug-and-play-streaming-ui-report.md` | D 已清理 | `gitignored` 本地草稿，已由 `.gitignore: docs/` 忽略，`git ls-files` 不含 |

---

## 四、使用指引

1. **新会话开工必读顺序**：`DOCS_AUTHORITY.md`（本文件）→ `SIX_FEATURES_COMPETITION_MAPPING.md`（范围）→ `TASKS_M3_CLOSE.md`（出口/门禁）→ `TASKS_STAGE_3_DETAILS.md`（细节勾选）。
2. **禁止行为**：禁止引用 `TASK_BREAKDOWN_v2` 判断完成度、禁止回写该冻结文件（`TASKS_M3_CLOSE:149` / `DETAILS:1027` 同步约定已改为"已迁移，不再回写冻结文件"）、禁止执行 `TASKS_STAGE_1_2 Sprint5` 重复造 `knowledge spaces/FTS`。
3. **代码重锚**：`backend/src/api/learning.py:14`、`app.py:132`、`tests/test_reference_verification.py:28` 已重锚至 `SIX_FEATURES §三/§二 / M3_CLOSE H-T1`，`grep -rn "TASK_BREAKDOWN" backend --include="*.py"` 仅剩冻结注释。
4. **验证命令**：
   ```bash
   grep -rn "唯一的范围与进度权威" docs --include="*.md"   # 应只命中删除线
   grep -rn "TASK_BREAKDOWN_v2" docs --include="*.md" | wc -l  # 入站降至历史溯源3处以内
   grep -rn "SIX_FEATURES" docs --include="*.md"              # 新增≥4入站
   git ls-files docs -- "*.md" | grep streaming                 # 应无输出
   git check-ignore -v docs/agent-streaming-ui-research/plug-and-play-streaming-ui-report.md
   ```

---

## 五、版本

- `2026-08-26` 创建本清单，冻结 `TASK_BREAKDOWN_v2` 等 4 处横幅，同步 `M3_CLOSE/DETAILS/SIX_FEATURES` 三处校准。方案来源：`过时文档清理方案 2026-08-26`（Step1-5）。
