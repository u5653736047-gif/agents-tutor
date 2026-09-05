"""FTS5 候选预筛基准（S5-C4）：全表扫描 vs FTS 路由的可复现对比。

用法（backend 目录下）：
    .venv/Scripts/python.exe scripts/bench_fts.py --chunks 15000

语料构造（与 docs/perf-evidence/fts-benchmark-2026-08-23.md 同构）：
每 chunk = 25 个公共领域词（低选择性填充）+ 1 个唯一字母数字 token
（`xref{hex}`，高选择性词项）。查询负载分两类：
- 高选择性：唯一 token 精确检索（候选 ≈ 0.03%）；
- 低选择性：多字中文词组合（单字词项全库命中，候选 ≈ 100%，
  触发 COUNT 选择性路由回退全表扫描）。

输出 markdown 表格：两场景各自的全表扫描 / FTS 路由耗时与加速比，
以及两路径结果逐项等价性断言（chunk_id + 分数）。
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

from core.knowledge.index import SqliteKnowledgeIndex
from core.knowledge.models import KnowledgeChunk

COMMON_WORDS = [
    "机器学习", "模型", "训练", "数据", "算法",
    "优化", "样本", "特征", "预测", "评估",
]


def _make_chunk(index: int, rng: random.Random) -> KnowledgeChunk:
    filler = " ".join(rng.choice(COMMON_WORDS) for _ in range(25))
    uid = f"xref{index:05x}"
    content = f"{filler} {uid} ref{index % 997:03d}"
    return KnowledgeChunk(
        chunk_id=f"d{index}",
        document_id=f"doc{index // 10}",
        content=content,
        source="bench.pdf",
        page=1,
        start=0,
        end=len(content),
        metadata={"namespace": "public"},
    )


def _bench(index: SqliteKnowledgeIndex, queries: list[str], fts_on: bool, rounds: int) -> list[float]:
    index._fts_enabled = fts_on
    timings: list[float] = []
    for _ in range(rounds):
        start = time.perf_counter()
        for query in queries:
            index.search(query, top_k=5)
        timings.append(time.perf_counter() - start)
    return timings


def _stats(timings: list[float]) -> str:
    return f"{statistics.mean(timings) * 1000:.2f}ms"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FTS5 候选预筛基准")
    parser.add_argument("--chunks", type=int, default=15000, help="语料 chunk 数")
    parser.add_argument("--seed", type=int, default=42, help="随机种子")
    parser.add_argument("--rounds", type=int, default=5, help="每场景计时轮数")
    args = parser.parse_args(argv)

    rng = random.Random(args.seed)
    index = SqliteKnowledgeIndex(Path(tempfile.mkdtemp()) / "bench.db")

    t0 = time.perf_counter()
    index.upsert(_make_chunk(i, rng) for i in range(args.chunks))
    upsert_seconds = time.perf_counter() - t0

    selective_queries = [f"xref{i:05x}" for i in (0x17, 0x4F31, 0x8AA, 0x7A2C)]
    common_queries = ["机器学习 模型 训练", "数据 算法 特征"]

    print(f"语料 {args.chunks} chunks / upsert {upsert_seconds:.2f}s")
    print()
    print("| 场景 | 全表扫描 | FTS 路由后 | 加速比 |")
    print("| --- | --- | --- | --- |")

    all_queries = selective_queries + common_queries
    equivalent = True
    for label, queries in (
        ("高选择性 token", selective_queries),
        ("低选择性中文", common_queries),
    ):
        full_times = _bench(index, queries, False, args.rounds)
        fts_times = _bench(index, queries, True, args.rounds)
        ratio = statistics.mean(full_times) / statistics.mean(fts_times)
        print(
            f"| {label} | {_stats(full_times)} | {_stats(fts_times)} "
            f"| {ratio:.1f}x |"
        )

    # 等价性终验：路由开/关两条路径对同批查询返回一致结果。
    for query in all_queries:
        index._fts_enabled = True
        via_fts = [
            (hit.chunk.chunk_id, round(hit.score, 6))
            for hit in index.search(query, top_k=5)
        ]
        index._fts_enabled = False
        via_scan = [
            (hit.chunk.chunk_id, round(hit.score, 6))
            for hit in index.search(query, top_k=5)
        ]
        if via_fts != via_scan:
            equivalent = False

    print()
    print(f"等价性: {'一致' if equivalent else '不一致（存在回归！）'}")
    return 0 if equivalent else 1


if __name__ == "__main__":
    sys.exit(main())
