# RAG 检索增强（S5）上线存证 — 2026-08-20

## 存证方式

真实 lifespan 装配（加载根目录 `.env`，与 `scripts/start-stage3.ps1` 同一语义），
ASGI 直连请求 `/healthz`，不依赖端口绑定。运行环境：Windows 11 + Python 3.11 +
fastembed 0.8.0（embedding 与 reranker 模型均已缓存本地，全程离线可用）。

## 启动日志（api.app）

```
INFO api.app: 知识检索模式=hybrid embedding_provider=FastEmbedProvider vector_dimension=512 query_rewrite=True reranker=True
```

## /healthz 响应

```json
{
  "status": "ok",
  "retrieval": {
    "mode": "hybrid",
    "embedding_provider": "FastEmbedProvider",
    "vector_dimension": 512,
    "rewrite_enabled": true,
    "reranker_enabled": true
  },
  "ocr": { "enabled": false }
}
```

## 结论

- `mode=hybrid` + `embedding_provider=FastEmbedProvider` + `vector_dimension=512`：
  真实语义检索在线（非哈希替身降级）；
- `rewrite_enabled=true`：LLM 查询改写器已装配（生产 key 已配置，
  轻量实例 timeout=10s / max_tokens=128 / max_retries=0）；
- `reranker_enabled=true`：Cross-Encoder 重排器（BAAI/bge-reranker-base）
  已装配；
- 检索质量量化对比（词法 / 混合 / 混合+重排）见同目录
  `retrieval-eval-*.md` / `retrieval-eval-*.json`。

## 备注

- 模型下载：本机直连 huggingface.co 被重置，经 `HF_ENDPOINT=https://hf-mirror.com`
  + `HF_HUB_DISABLE_XET=1`（镜像 xet 后端 401）完成首次下载；之后完全离线。
- `ocr.enabled=false`：未安装 `ocr` 可选依赖组，按预期优雅降级。

---

## π-agent 审核复核结论（2026-08-21）

**复核范围**：方向一（智能工作流编排）与方向二（多模态交互）的任务清单方案可行性审查。

**关键发现与决策**：

### 🟡 必须开工前解决

| 编号 | 问题 | 决策 |
| --- | --- | --- |
| A2-1 | on_failure 字段暴露 vs 内部语义冲突 | **选择最小风险方案**：on_failure 仅做核心层熔断控制，API DTO/前端零改动；二期再开放契约链。 |
| A4-stream | stream.py 无 plan 处理逻辑 | **冒烟验收标准明确化**：「计划步骤条与逐步结果可见」指**完成后点亮**（ChatResponse / SessionProcess），非流中实时更新；流中渲染纳入 P1 增强。 |
| B4-XSS | mammoth HTML/SHEETJS 安全漏洞 | **强制安全措施**：mammoth 启用 blockedTags + DOMPurify.sanitize；sheetjs pin ≥1.0.0（ReDoS 修复版）；预览容器用 iframe sandbox。

### 🔴 应纳入设计（影响体验但可降级）

| 编号 | 问题 | 决策 |
| --- | --- | --- |
| A3-conflict | ACTIVE 计划期间创建新计划行为未定义 | **策略**：工具层拒绝（错误码 GRAPH_INVALID_TARGET），提示「请先收口当前计划」。例外清单：detect_intent/shell 不受限。 |
| A4-fail-event | RUN_FAILED 事件误判整轮失败 | **策略**：移除发 RUN_FAILED 事件 → supervisor 仍可输出整合答案；plan-status 通过 data-part payload 传达。 |
| A5-abort-chain | shell 审批拒绝不应摧毁整个计划 | **默认策略**：`on_failure=continue`（原 abort × 审批拒绝连锁效应解除）；prompt 引导模型对可跳过步骤标记 continue。 |

### 🟢 建议项（不阻塞首批交付）

| 编号 | 问题 | 决策 |
| --- | --- | --- |
| B2-chunking | Markdown 表被切块截断 → 检索质量下降 | **容忍度**：不影响检索功能，展示略逊（接受度可谈）。扩展 evaluate_retrieval.py 至 2~3 条含表教材的查询词作为质量保障。 |
| B3-timeout | VLM 调用超时/预算约束缺失 | **参数复用**：timeout=10s、max_tokens=128、max_retries=0（参考 S5 改写器经验教训）。 |
| 门禁笔误 | `mypy src` 严格模式表述不一致 | **修正**：仓库配置 mypy.ini `strict=true` → CLI 用 `mypy src` 即可（已在任务清单中修正措辞）。 |
