"""S3-T5 混合检索测试：RRF 融合正确性、过滤先于融合、两路失效场景、
默认路径与降级路径。

覆盖清单 A S3-T5 验收标准：
1. 融合排序正确性：构造确定的两路分数（固定向量替身 + 词法内容），
   精确断言 RRF 公式算出的融合分与排序；
2. metadata 过滤在融合前生效（过滤后融合、空结果返回 []）；
3. 两路各自失效场景下融合优于单路：
   - 场景 A：词法失效（土豆/马铃薯同义）→ 词法单路 0 命中，混合命中；
   - 场景 B：向量失效（「词法高分但向量余弦为 0」的反例）→ 向量单路
     丢失该 chunk，混合救回；融合结果同时包含两路各自场景的命中项，
     且排序确定（同分按 chunk_id 升序）；
4. 默认路径为混合：KnowledgeService 挂 HybridKnowledgeIndex 即走
   混合检索（search_knowledge 工具的 service 构造点）；向量库按
   「可用才开」启用（open_vector_index_if_available）；
5. 降级路径：无向量库（vector=None / 文件不存在 / 打开失败）→ 纯词法
   单路，不抛错，分数与排序和词法单路逐项一致；
6. 与 S3-T3/T4 语义兼容：upsert/delete 两路同步、非法 metadata_filter
   的报错行为与单路一致（既有测试不退化）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.knowledge.embedding import HashEmbeddingProvider
from core.knowledge.hybrid import (
    HybridKnowledgeIndex,
    open_vector_index_if_available,
)
from core.knowledge.index import InMemoryKnowledgeIndex
from core.knowledge.models import KnowledgeChunk, KnowledgeDocument
from core.knowledge.service import KnowledgeService
from core.knowledge.vector_index import (
    InMemoryVectorKnowledgeIndex,
    SqliteVectorKnowledgeIndex,
)

# ── 测试替身（与 test_knowledge_vector_index.py 同一套设计）──────


def _chunk(
    chunk_id: str,
    content: str,
    *,
    document_id: str = "doc-1",
    metadata: dict[str, Any] | None = None,
) -> KnowledgeChunk:
    return KnowledgeChunk(
        chunk_id=chunk_id,
        document_id=document_id,
        content=content,
        source=f"{document_id}.txt",
        page=None,
        start=0,
        end=len(content),
        metadata=metadata or {},
    )


class _FixedVectorProvider:
    """测试替身：文本 → 手工指定向量（维度 3，故意不归一化）。

    用途（面向初学者）：把「embedding 质量」从「融合排序正确性」中
    剥离——向量是测试写死的，RRF 融合行为就能被精确断言。
    """

    def __init__(self, mapping: dict[str, list[float]]) -> None:
        self.dimension = 3
        self._mapping = mapping

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._mapping.get(text, [0.0, 0.0, 0.0]) for text in texts]


def _synonym_provider() -> HashEmbeddingProvider:
    """测试替身：同义词归一化哈希向量（模拟语义模型的等价映射能力）。

    归一化把「土豆」替换成「马铃薯」后再做字符特征哈希 → 查询「土豆」
    与分块「马铃薯…」共享特征、相似度 > 0，而词法索引看不到这层关系
    （语义命中机制详见 test_knowledge_vector_index.py）。
    """

    def normalize(text: str) -> str:
        return text.replace("土豆", "马铃薯").replace("cnn", "卷积神经网络")

    return HashEmbeddingProvider(normalize=normalize)


# ── 1. RRF 融合正确性（精确断言公式）─────────────────────────────


def test_hybrid_fuses_rankings_with_rrf() -> None:
    """融合分 = Σ 1/(k+排名)，两路都命中者排最前，词法失效项仍入选。

    构造（全部确定、可手算）：
    - 词法路（查询 "alpha"）：a1、a2 各命中 1 个词 → 同分按 chunk_id
      a1 第 1、a2 第 2；g1 0 命中（词法失效项）；
    - 向量路：a1 余弦 1.0 第 1、a2 ≈0.994 第 2、g1 ≈0.707 第 3。
    - RRF(k=60)：a1 = 1/61+1/61 = 2/61；a2 = 1/62+1/62 = 1/31；
      g1 = 1/63（单项分兜底，仍进入融合结果）。
    """
    provider = _FixedVectorProvider(
        {
            "alpha": [1.0, 0.0, 0.0],
            "alpha beta": [0.9, 0.1, 0.0],
            "gamma": [0.5, 0.5, 0.0],
        }
    )
    hybrid = HybridKnowledgeIndex(
        InMemoryKnowledgeIndex(),
        InMemoryVectorKnowledgeIndex(provider),
    )
    hybrid.upsert(
        [
            _chunk("a1", "alpha"),
            _chunk("a2", "alpha beta"),
            _chunk("g1", "gamma"),
        ]
    )

    hits = hybrid.search("alpha", top_k=3)

    assert [hit.chunk.chunk_id for hit in hits] == ["a1", "a2", "g1"]
    assert hits[0].score == pytest.approx(2 / 61)
    assert hits[1].score == pytest.approx(1 / 31)
    assert hits[2].score == pytest.approx(1 / 63)
    # 对照：g1 在词法单路 0 命中（词法失效），靠向量路进入融合结果。
    lexical = InMemoryKnowledgeIndex()
    lexical.upsert(
        [
            _chunk("a1", "alpha"),
            _chunk("a2", "alpha beta"),
            _chunk("g1", "gamma"),
        ]
    )
    assert [hit.chunk.chunk_id for hit in lexical.search("alpha", top_k=3)] == [
        "a1",
        "a2",
    ]


def test_hybrid_custom_rrf_k_changes_scores() -> None:
    """rrf_k 可调：更小的 k 让排名靠前的项优势更大（教学点：权重旋钮）。"""
    provider = _FixedVectorProvider({"alpha": [1.0, 0.0, 0.0]})
    hybrid = HybridKnowledgeIndex(
        InMemoryKnowledgeIndex(),
        InMemoryVectorKnowledgeIndex(provider),
        rrf_k=1,
    )
    hybrid.upsert([_chunk("c1", "alpha")])

    hits = hybrid.search("alpha", top_k=5)
    # 两路都排第 1：融合分 = 1/(1+1) + 1/(1+1) = 1.0。
    assert hits[0].score == pytest.approx(1.0)
    # 非法 rrf_k 直接拒绝。
    with pytest.raises(ValueError, match="rrf_k"):
        HybridKnowledgeIndex(InMemoryKnowledgeIndex(), rrf_k=0)


# ── 2. metadata 过滤在融合之前生效 ────────────────────────────────


def test_hybrid_metadata_filter_applies_before_fusion() -> None:
    """过滤先于融合：被过滤的 chunk 即便分数最高也不进融合结果。

    若过滤发生在融合之后，c2（词法/向量都命中、分数不低）会混入
    结果——断言结果只含 c1 即证明过滤在融合前（与 S3-T3 语义一致）。
    """
    provider = _FixedVectorProvider(
        {
            "alpha": [1.0, 0.0, 0.0],
            "beta": [0.9, 0.0, 0.0],
        }
    )
    hybrid = HybridKnowledgeIndex(
        InMemoryKnowledgeIndex(),
        InMemoryVectorKnowledgeIndex(provider),
    )
    hybrid.upsert(
        [
            _chunk("c1", "alpha", metadata={"difficulty": "advanced"}),
            _chunk("c2", "beta", metadata={"difficulty": "beginner"}),
        ]
    )

    # 不过滤：c1 两路都排第 1（融合分 2/61），c2 仅向量路命中（1/62）。
    hits = hybrid.search("alpha", top_k=5)
    assert [hit.chunk.chunk_id for hit in hits] == ["c1", "c2"]
    # 过滤 advanced：只留 c1——c2 在两路候选里都被剔除。
    hits = hybrid.search(
        "alpha", top_k=5, metadata_filter={"difficulty": "advanced"}
    )
    assert [hit.chunk.chunk_id for hit in hits] == ["c1"]
    # 过滤后无匹配 → 空列表（不报错，与单路行为一致）。
    assert (
        hybrid.search(
            "alpha",
            top_k=5,
            metadata_filter={"difficulty": "advanced", "source": "other.txt"},
        )
        == []
    )


# ── 3. 两路各自失效场景下融合优于单路 ─────────────────────────────


def test_hybrid_wins_when_lexical_fails_synonym() -> None:
    """场景 A：词法失效（土豆/马铃薯同义）→ 词法单路 0 命中，混合命中。"""
    chunks = [
        _chunk("potato", "马铃薯是重要的粮食作物，块茎富含淀粉", document_id="agri"),
        _chunk("corn", "玉米是重要的粮食作物，籽粒富含淀粉", document_id="agri"),
    ]
    # 词法单路对照：查询「土豆」与两个 chunk 无共享字符特征 → 0 命中。
    lexical = InMemoryKnowledgeIndex()
    lexical.upsert(chunks)
    assert lexical.search("土豆", top_k=5) == []

    # 混合检索：向量路经同义词归一化命中 potato chunk。
    hybrid = HybridKnowledgeIndex(
        InMemoryKnowledgeIndex(),
        InMemoryVectorKnowledgeIndex(_synonym_provider()),
    )
    hybrid.upsert(chunks)
    hits = hybrid.search("土豆", top_k=5)
    assert hits, "混合检索应命中词法单路无法命中的同义表述"
    assert hits[0].chunk.chunk_id == "potato"
    # 宽松补充断言：即使哈希碰撞发生，目标 chunk 也应出现在结果中。
    assert "potato" in [hit.chunk.chunk_id for hit in hits]


def test_hybrid_wins_when_vector_fails_lexical_high_score() -> None:
    """场景 B：向量失效（词法高分但向量余弦为 0 的反例）→ 混合救回。

    反例构造（面向初学者）：查询文本与 chunk 内容必须「不同文本、
    词法共享特征」——因为同一 Embedding 提供方对相同文本必然返回
    相同向量（"apple" 查询与 "apple" 内容余弦恒为 1），无法构造
    正交。所以：
    - apple-c 的内容是 "apple pie"：词法路命中（含词 apple），但向量
      被替身设为 [0,1,0]，与查询 "apple" 的向量 [1,0,0] 正交 → 余弦
      0 → 向量路彻底失效（模拟哈希替身/低质量向量下语义近邻难构造）；
    - banana-c 内容 "banana"：词法 0 命中，但向量 [0.9,0,0]（归一化后
      [1,0,0]）与查询同向 → 余弦 1.0 → 向量路命中。
    融合结果应同时包含两个 chunk（两路各救一个），且顺序确定（两路
    都排第 1 → 融合分同分 1/61，按 chunk_id 升序）。
    """
    provider = _FixedVectorProvider(
        {
            "apple": [1.0, 0.0, 0.0],  # 查询 "apple" 的向量
            "apple pie": [0.0, 1.0, 0.0],  # apple-c：与查询正交 → 向量路失效
            "banana": [0.9, 0.0, 0.0],  # banana-c：归一化后 [1,0,0]，余弦 1.0
        }
    )
    chunks = [_chunk("apple-c", "apple pie"), _chunk("banana-c", "banana")]
    lexical = InMemoryKnowledgeIndex()
    lexical.upsert(chunks)
    vector = InMemoryVectorKnowledgeIndex(provider)
    vector.upsert(chunks)

    # 单路对照：向量单路丢失词法高分项 apple-c（余弦 0 被跳过）；
    # 词法单路丢失 banana-c（无共享字符）。
    assert [hit.chunk.chunk_id for hit in vector.search("apple", top_k=5)] == [
        "banana-c"
    ]
    assert [hit.chunk.chunk_id for hit in lexical.search("apple", top_k=5)] == [
        "apple-c"
    ]

    # 融合：同时包含两路各自场景的命中项（命中集合 + 相对顺序都断言）。
    hybrid = HybridKnowledgeIndex(lexical, vector)
    hits = hybrid.search("apple", top_k=5)
    assert [hit.chunk.chunk_id for hit in hits] == ["apple-c", "banana-c"]
    assert all(hit.score == pytest.approx(1 / 61) for hit in hits)


# ── 4. 默认路径：KnowledgeService 挂混合索引 ──────────────────────


def test_knowledge_service_hybrid_default_path() -> None:
    """默认路径：KnowledgeService 挂混合索引后，service.search 走混合
    （search_knowledge 工具的构造点——工具本身不构造 service）。"""
    service = KnowledgeService(
        HybridKnowledgeIndex(
            InMemoryKnowledgeIndex(),
            InMemoryVectorKnowledgeIndex(_synonym_provider()),
        ),
        chunk_size=30,
        overlap=5,
    )
    service.add_documents(
        [
            KnowledgeDocument(
                document_id="agri",
                content="马铃薯是重要的粮食作物，块茎富含淀粉",
                source="agri.txt",
            )
        ]
    )

    # 词法单路查「土豆」必然 0 命中；经 service 的混合检索能命中。
    hits = service.search("土豆的种植", top_k=5)
    assert hits
    assert hits[0].chunk.document_id == "agri"
    assert hits[0].score > 0


# ── 5. 降级路径（无向量库 → 词法单路，不抛错）────────────────────


def test_hybrid_falls_back_to_lexical_without_vector() -> None:
    """vector=None 降级：结果与词法单路逐项一致（分数、排序、边界行为）。"""
    chunks = [
        _chunk("c1", "alpha beta"),
        _chunk("c2", "alpha gamma"),
        _chunk("c3", "beta gamma"),
    ]
    lexical = InMemoryKnowledgeIndex()
    lexical.upsert(chunks)
    hybrid = HybridKnowledgeIndex(lexical)  # 不传向量路 → 降级

    assert hybrid.vector_enabled is False
    single = lexical.search("alpha", top_k=5)
    merged = hybrid.search("alpha", top_k=5)
    assert [(h.chunk.chunk_id, h.score) for h in merged] == [
        (h.chunk.chunk_id, h.score) for h in single
    ]
    # 边界行为与词法单路一致：空查询 / 非法 top_k 返回 []。
    assert hybrid.search("", top_k=5) == []
    assert hybrid.search("   ", top_k=5) == []
    assert hybrid.search("alpha", top_k=0) == []


def test_hybrid_upsert_and_delete_sync_both_retrievers() -> None:
    """upsert/delete_document 两路同步：写入后两路都可检索，删除后都消失。"""
    hybrid = HybridKnowledgeIndex(
        InMemoryKnowledgeIndex(),
        InMemoryVectorKnowledgeIndex(
            _FixedVectorProvider({"alpha": [1.0, 0.0, 0.0]})
        ),
    )
    hybrid.upsert([_chunk("c1", "alpha")])
    assert [hit.chunk.chunk_id for hit in hybrid.search("alpha", top_k=5)] == ["c1"]

    hybrid.delete_document("doc-1")
    assert hybrid.search("alpha", top_k=5) == []


def test_hybrid_rejects_invalid_metadata_filter() -> None:
    """非法 metadata_filter 的报错与单路一致（S3-T3 校验语义兼容）。"""
    hybrid = HybridKnowledgeIndex(InMemoryKnowledgeIndex())
    with pytest.raises(ValueError, match="metadata_filter"):
        hybrid.search(
            "alpha",
            top_k=5,
            metadata_filter={"subject!": "机器学习"},  # type: ignore[dict-item]
        )
    with pytest.raises(TypeError, match="metadata_filter"):
        hybrid.search(  # type: ignore[arg-type]
            "alpha", top_k=5, metadata_filter=[]
        )


# ── 6. 向量库「可用才开」：默认路径的启用/降级判定 ────────────────


def test_open_vector_index_if_available_returns_none_without_creating_file(
    tmp_path: Path,
) -> None:
    """向量库文件不存在 → 返回 None，且不创建文件（只读场景不落盘）。"""
    missing = tmp_path / "no-vector.db"
    assert open_vector_index_if_available(missing) is None
    assert not missing.exists()


def test_open_vector_index_if_available_opens_existing_db(tmp_path: Path) -> None:
    """向量库文件存在且可打开 → 返回可用索引（混合检索的启用入口）。"""
    db_path = tmp_path / "vector.db"
    first = SqliteVectorKnowledgeIndex(db_path, provider=HashEmbeddingProvider())
    first.upsert([_chunk("c1", "马铃薯是重要的粮食作物")])
    first.close()

    opened = open_vector_index_if_available(db_path)
    assert opened is not None
    try:
        assert [hit.chunk.chunk_id for hit in opened.search("马铃薯", top_k=5)] == [
            "c1"
        ]
    finally:
        opened.close()


def test_open_vector_index_if_available_falls_back_on_dimension_mismatch(
    tmp_path: Path,
) -> None:
    """维度不匹配的旧库（换过 provider）→ 返回 None 降级，不抛错。"""
    db_path = tmp_path / "vector.db"
    first = SqliteVectorKnowledgeIndex(
        db_path, provider=HashEmbeddingProvider(dimension=512)
    )
    first.upsert([_chunk("c1", "任何文本")])
    first.close()

    # 默认 provider 是 256 维 → 加载旧库时维度校验失败 → 降级（不抛错）。
    assert open_vector_index_if_available(db_path) is None


def test_open_vector_index_if_available_falls_back_on_corrupt_file(
    tmp_path: Path,
) -> None:
    """库文件损坏（不是 SQLite 文件）→ 返回 None 降级，不抛错。

    覆盖降级触发条件中的 sqlite3.Error 分支：SQLite 在建表/读库时
    发现文件不是数据库格式会抛 sqlite3.DatabaseError（sqlite3.Error
    的子类），同样按「向量路不可用」降级（见 hybrid.py 的
    open_vector_index_if_available 异常注释）。
    """
    db_path = tmp_path / "corrupt.db"
    db_path.write_bytes(b"this is not a sqlite database")
    assert open_vector_index_if_available(db_path) is None
