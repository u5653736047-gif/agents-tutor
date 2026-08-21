"""检索质量离线评测（S5）：词法 / 混合 / 混合+重排（+改写）多配置对比。

用法（在 backend/ 目录下，使用项目 venv）：
    $env:PYTHONPATH="src"; .venv/Scripts/python.exe scripts/evaluate_retrieval.py
    $env:PYTHONPATH="src"; .venv/Scripts/python.exe scripts/evaluate_retrieval.py --no-rerank
    $env:PYTHONPATH="src"; .venv/Scripts/python.exe scripts/evaluate_retrieval.py --with-rewrite

设计说明（面向初学者）：
1. 为什么需要这个脚本
   ingest_books.py --verify 是「入库 sanity check」（期望 source 命中即
   PASS），回答不了「检索质量有多好、增强组件带来多少提升」。本脚本
   用带标注的评测集计算 Recall@1 / Recall@5 / MRR，并对多种检索配置
   做横向对比，产出赛题答辩与迭代调参所需的量化证据（报告落
   docs/perf-evidence/）。

2. 评测集来源（两部分合并，按 query 去重）
   - knowledge_manifest.json 的 verify 用例（跳过 blocked 书目）——
     入库脚本同源的「事实性」用例；
   - 本脚本内置的扩展用例（_EXTENDED_CASES）——以同义/口语化问法
     为主（如「过拟合怎么缓解」对应教材的「正则化」章节），模拟真实
     学生提问与教材术语之间的措辞差距，这正是改写/向量/重排要解决的
     问题。期望标注到「逻辑 source（哪本书）」粒度。

3. 评测配置（横向对比）
   - lexical：纯词法单路（SqliteKnowledgeIndex）；
   - hybrid：词法 + 向量 RRF 融合（向量库不可用自动降级词法，与生产
     同一「可用才开」语义）；
   - hybrid+rerank：混合初检 + Cross-Encoder 重排（--no-rerank 关闭；
     首次运行需联网下载重排模型，之后离线）；
   - hybrid+rewrite+rerank：再加 LLM 查询改写（--with-rewrite 显式
     开启，需要已配置的 DeepSeek key——评测默认离线，不依赖网络模型）。
   所有配置共用 KnowledgeService 默认行为（含 frontmatter 噪音抑制），
   与生产检索路径一致。

4. 指标与命中口径
   每个用例检索 top_k（默认 5）条结果，按 citation.source 与期望
   source 比对：首个命中所在的排名（1 起）记为 rank，未命中记 None。
   Recall@1 = rank==1 的用例占比；Recall@5 = 命中用例占比；MRR =
   mean(1/rank)。本脚本只做测量与报告，不因指标退化而失败（退出码
   0 = 评测完成，1 = 硬错误如词法库缺失）——是否构成回归由人读报告
   判断（单调性分析见报告汇总表）。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from core.knowledge.embedding import EmbeddingProvider, FastEmbedProvider, HashEmbeddingProvider
from core.knowledge.hybrid import HybridKnowledgeIndex, open_vector_index_if_available
from core.knowledge.index import SqliteKnowledgeIndex
from core.knowledge.llm_rewriter import LLMQueryRewriter
from core.knowledge.models import SearchHit
from core.knowledge.reranker import DEFAULT_RERANK_MODEL, FastEmbedReranker
from core.knowledge.retrieval import Reranker
from core.knowledge.service import KnowledgeService
from core.knowledge.vector_index import SqliteVectorKnowledgeIndex

# ── 路径约定：与 ingest_books.py 同一「脚本向上两级即仓库根」惯例 ──
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = REPO_ROOT / "data" / "knowledge.db"
DEFAULT_VECTOR_DB_PATH = REPO_ROOT / "data" / "vector_knowledge.db"
DEFAULT_MANIFEST = Path(__file__).resolve().parent / "knowledge_manifest.json"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "docs" / "perf-evidence"
DEFAULT_TOP_K = 5


@dataclass(frozen=True)
class EvalCase:
    """单个评测用例：查询 + 期望命中的逻辑 source + 来源标注。"""

    query: str
    expected_source: str
    origin: str  # "manifest"（清单 verify 用例）或 "extended"（内置扩展）


# ── 内置扩展用例（同义/口语化问法为主，期望标注到书粒度）──────────
# 选题原则：每本书的标志性核心章节，措辞刻意与教材术语保持差距
# （如「过拟合怎么缓解」vs 教材的「正则化」），覆盖四本已入库教材。
_EXTENDED_CASES: list[tuple[str, str]] = [
    # ml-zhouzhihua《机器学习》
    ("过拟合怎么缓解", "ml-zhouzhihua"),
    ("决策树怎么做特征划分", "ml-zhouzhihua"),
    ("什么是留出法和交叉验证", "ml-zhouzhihua"),
    ("主成分分析降维的原理", "ml-zhouzhihua"),
    ("K均值聚类怎么工作", "ml-zhouzhihua"),
    ("朴素贝叶斯分类器", "ml-zhouzhihua"),
    ("神经网络中的激活函数作用", "ml-zhouzhihua"),
    # dl-d2l《动手学深度学习》
    ("dropout 丢弃法是怎么防止过拟合的", "dl-d2l"),
    ("批量归一化有什么作用", "dl-d2l"),
    ("Adam 优化器和学习率调度", "dl-d2l"),
    ("如何从零实现线性回归", "dl-d2l"),
    ("卷积神经网络中的填充和步幅", "dl-d2l"),
    ("Transformer 的多头注意力", "dl-d2l"),
    # dl-goodfellow《深度学习》
    ("卷积网络中的池化有什么作用", "dl-goodfellow"),
    ("LSTM 如何缓解梯度消失", "dl-goodfellow"),
    ("生成对抗网络的判别器和生成器", "dl-goodfellow"),
    ("什么是表示学习", "dl-goodfellow"),
    ("正则化中的 L2 参数惩罚", "dl-goodfellow"),
    # ai-russell《人工智能：现代方法》
    ("A* 搜索的启发函数需要满足什么条件", "ai-russell"),
    ("贝叶斯网络如何表示条件独立", "ai-russell"),
    ("马尔可夫决策过程与价值迭代", "ai-russell"),
    ("博弈搜索中的 alpha-beta 剪枝", "ai-russell"),
    ("什么是遗传算法", "ai-russell"),
    # 表格内容专项（S5-B2 验收）：查询指向教材中以表格呈现的知识点，
    # 用于量化「PDF 表格转 Markdown」入库增强的检索收益。注意：需
    # --force 重入库且启用 pdf-table 后才能完全兑现；重入库前这些
    # 用例可能命中不佳（表格被 pypdf 拍平），属预期的基线对照。
    ("决策树 ID3 算法的信息增益对比", "ml-zhouzhihua"),
    ("常见激活函数的性质对比表", "dl-d2l"),
    ("各优化器的学习率自适应程度比较", "dl-d2l"),
]


def load_eval_cases(manifest_path: Path) -> list[EvalCase]:
    """合并清单 verify 用例与内置扩展用例（按 query 去重，清单优先）。

    不复用 ingest_books.load_manifest：它要求 PDF 文件存在于本地
    （入库语义），而评测只读已入库的索引库，不依赖 PDF 本体。
    blocked 书目的用例跳过（对应文档不在库中，期望永不可达）。
    """
    cases: list[EvalCase] = []
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取知识清单 {manifest_path}: {exc}") from exc
    for book in raw.get("books", []):
        if not isinstance(book, dict) or book.get("blocked"):
            continue
        source = str(book.get("source", ""))
        for case in book.get("verify", []):
            if isinstance(case, dict) and str(case.get("query", "")).strip():
                cases.append(
                    EvalCase(
                        query=str(case["query"]).strip(),
                        expected_source=str(case.get("expected_source", source)),
                        origin="manifest",
                    )
                )
    seen = {case.query for case in cases}
    for query, expected_source in _EXTENDED_CASES:
        if query not in seen:
            cases.append(
                EvalCase(query=query, expected_source=expected_source, origin="extended")
            )
    if not cases:
        raise ValueError("评测集为空：清单无可用 verify 用例且扩展用例缺失")
    return cases


# ── 指标（纯函数，可单测）────────────────────────────────────────


def rank_of_expected(hits: list[SearchHit], expected_source: str) -> int | None:
    """首个期望 source 命中的排名（1 起）；未命中返回 None。"""
    for rank, hit in enumerate(hits, start=1):
        if hit.citation.source == expected_source:
            return rank
    return None


def recall_at_k(ranks: list[int | None], k: int) -> float:
    """Recall@K：rank ≤ k 的用例占比（None 视为未命中）。"""
    if not ranks:
        return 0.0
    hits = sum(1 for rank in ranks if rank is not None and rank <= k)
    return hits / len(ranks)


def mean_reciprocal_rank(ranks: list[int | None]) -> float:
    """MRR：首个命中排名的倒数均值（未命中计 0）。"""
    if not ranks:
        return 0.0
    return sum(1.0 / rank if rank is not None else 0.0 for rank in ranks) / len(ranks)


@dataclass(frozen=True)
class ConfigReport:
    """单配置评测结果：配置名 + 逐用例 rank + 汇总指标。"""

    name: str
    ranks: tuple[int | None, ...]
    recall_1: float
    recall_5: float
    mrr: float


def run_config(
    name: str, service: KnowledgeService, cases: list[EvalCase], top_k: int
) -> ConfigReport:
    """对一组用例执行检索并汇总指标。"""
    ranks: list[int | None] = []
    for index, case in enumerate(cases, start=1):
        hits = service.search(case.query, top_k)
        ranks.append(rank_of_expected(hits, case.expected_source))
        if index % 10 == 0 or index == len(cases):
            print(f"  [{name}] 进度 {index}/{len(cases)}", flush=True)
    return ConfigReport(
        name=name,
        ranks=tuple(ranks),
        recall_1=recall_at_k(ranks, 1),
        recall_5=recall_at_k(ranks, top_k),
        mrr=mean_reciprocal_rank(ranks),
    )


# ── 装配：与 api/app.py 同一「可用才开」降级哲学 ──────────────────


def _open_vector_index(vector_db: Path) -> tuple[SqliteVectorKnowledgeIndex | None, str]:
    """按序尝试 embedding provider 打开向量库；返回 (向量索引或 None, 说明)。"""
    if not vector_db.exists():
        return None, "向量库不存在（降级纯词法）"
    candidates: list[tuple[str, EmbeddingProvider]] = []
    try:
        candidates.append(("FastEmbedProvider", FastEmbedProvider()))
    except (ImportError, RuntimeError, OSError):
        # 未安装 fastembed / 模型不可用：回退哈希替身（与生产 auto 一致）。
        pass
    candidates.append(("HashEmbeddingProvider", HashEmbeddingProvider()))
    for provider_name, provider in candidates:
        vector = open_vector_index_if_available(vector_db, provider=provider)
        if vector is not None:
            return vector, f"{provider_name}（{provider.dimension} 维）"
    return None, "向量库无法被任何 provider 打开（降级纯词法）"


def _build_rewrite_model() -> LLMQueryRewriter:
    """为 --with-rewrite 配置构建 LLM 改写器（需要已配置的 DeepSeek key）。"""
    from core.models import DeepSeekSettings, create_deepseek_model

    settings = DeepSeekSettings.from_env()  # 缺配置时抛 ValueError（尽早失败）
    # 与生产装配同一轻量参数（timeout/max_tokens 收紧），见 app.py。
    return LLMQueryRewriter(
        create_deepseek_model(settings, timeout=10, max_retries=0, max_tokens=128)
    )


def _render_markdown(
    reports: list[ConfigReport],
    cases: list[EvalCase],
    *,
    environment: dict[str, str],
    top_k: int,
) -> str:
    """渲染 Markdown 报告：环境信息 + 汇总表 + 逐用例明细表。"""
    lines = [
        "# 检索质量评测报告",
        "",
        f"- 生成时间（UTC）：{datetime.now(UTC).isoformat(timespec='seconds')}",
        f"- 用例数：{len(cases)}（manifest verify + 内置扩展，按 query 去重）",
        f"- top_k：{top_k}",
    ]
    for key, value in environment.items():
        lines.append(f"- {key}：{value}")
    lines += [
        "",
        "## 汇总指标",
        "",
        "| 配置 | Recall@1 | Recall@5 | MRR |",
        "| --- | --- | --- | --- |",
    ]
    for report in reports:
        lines.append(
            f"| {report.name} | {report.recall_1:.3f} | "
            f"{report.recall_5:.3f} | {report.mrr:.3f} |"
        )
    lines += [
        "",
        "## 逐用例明细（首个期望命中排名，— = 未命中）",
        "",
        "| 查询 | 期望来源 | " + " | ".join(report.name for report in reports) + " |",
        "| --- | --- | " + " | ".join("---" for _ in reports) + " |",
    ]
    for case_index, case in enumerate(cases):
        cells = [
            str(report.ranks[case_index]) if report.ranks[case_index] is not None else "—"
            for report in reports
        ]
        lines.append(f"| {case.query} | {case.expected_source} | " + " | ".join(cells) + " |")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检索质量离线评测（多配置对比）")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="词法库路径")
    parser.add_argument(
        "--vector-db", type=Path, default=DEFAULT_VECTOR_DB_PATH, help="向量库路径"
    )
    parser.add_argument(
        "--manifest", type=Path, default=DEFAULT_MANIFEST, help="知识源清单路径"
    )
    parser.add_argument(
        "--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="报告输出目录"
    )
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K, help="检索截断")
    parser.add_argument(
        "--no-rerank", action="store_true", help="关闭重排配置（不下载重排模型）"
    )
    parser.add_argument(
        "--rerank-model", default=DEFAULT_RERANK_MODEL, help="重排模型名"
    )
    parser.add_argument(
        "--with-rewrite",
        action="store_true",
        help="增加「混合+改写+重排」配置（需要已配置的 DeepSeek key，联网）",
    )
    args = parser.parse_args(argv)

    if not args.db.exists():
        print(f"词法库不存在：{args.db}（请先运行 ingest_books.py 入库）", flush=True)
        return 1
    cases = load_eval_cases(args.manifest)
    print(f"评测用例：{len(cases)} 条", flush=True)

    lexical = SqliteKnowledgeIndex(args.db)
    vector, vector_note = _open_vector_index(args.vector_db)
    print(f"向量路：{vector_note}", flush=True)
    hybrid = HybridKnowledgeIndex(lexical, vector)

    reranker: Reranker | None = None
    rerank_note = "已关闭（--no-rerank）"
    if not args.no_rerank:
        try:
            reranker = FastEmbedReranker(model_name=args.rerank_model)
            rerank_note = f"{args.rerank_model}（FastEmbedReranker）"
        except (RuntimeError, OSError, ValueError) as exc:
            rerank_note = f"不可用（{type(exc).__name__}，降级不重排）"
    print(f"重排器：{rerank_note}", flush=True)

    configs: list[tuple[str, KnowledgeService]] = [
        ("lexical", KnowledgeService(lexical)),
        ("hybrid", KnowledgeService(hybrid)),
    ]
    if reranker is not None:
        configs.append(("hybrid+rerank", KnowledgeService(hybrid, reranker=reranker)))
    rewrite_note = "未启用（默认离线；--with-rewrite 开启）"
    if args.with_rewrite:
        try:
            rewriter = _build_rewrite_model()
        except ValueError as exc:
            print(f"改写配置跳过：{exc}", flush=True)
        else:
            configs.append(
                (
                    "hybrid+rewrite+rerank",
                    KnowledgeService(hybrid, rewriter=rewriter, reranker=reranker),
                )
            )
            rewrite_note = "已启用（LLMQueryRewriter + DeepSeek）"

    reports = [run_config(name, service, cases, args.top_k) for name, service in configs]

    environment = {
        "向量路": vector_note,
        "重排器": rerank_note,
        "改写器": rewrite_note,
    }
    markdown = _render_markdown(reports, cases, environment=environment, top_k=args.top_k)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    md_path = args.output_dir / f"retrieval-eval-{stamp}.md"
    json_path = args.output_dir / f"retrieval-eval-{stamp}.json"
    md_path.write_text(markdown, encoding="utf-8")
    json_path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "top_k": args.top_k,
                "environment": environment,
                "cases": [
                    {
                        "query": case.query,
                        "expected_source": case.expected_source,
                        "origin": case.origin,
                    }
                    for case in cases
                ],
                "reports": [
                    {
                        "name": report.name,
                        "recall_1": report.recall_1,
                        "recall_5": report.recall_5,
                        "mrr": report.mrr,
                        "ranks": list(report.ranks),
                    }
                    for report in reports
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print("\n汇总：", flush=True)
    for report in reports:
        print(
            f"  {report.name}: Recall@1={report.recall_1:.3f} "
            f"Recall@5={report.recall_5:.3f} MRR={report.mrr:.3f}",
            flush=True,
        )
    print(f"\n报告已写入：{md_path}\n          {json_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
