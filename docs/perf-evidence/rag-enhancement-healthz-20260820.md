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
