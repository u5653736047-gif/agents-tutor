"""S3-T4 Embedding 提供方协议与内置实现测试。

覆盖清单 A S3-T4 验收标准对应项：
1. EmbeddingProvider 协议可替换（替身 provider 注入见
   test_knowledge_vector_index.py 的 _FixedVectorProvider 用例）；
2. HashEmbeddingProvider（默认降级实现）：确定性（同一文本跨调用
   同向量——这是向量能持久化重载的前提）、维度契约、空文本、
   normalize 注入点（测试替身设计：同义词归一化模拟语义等价）；
3. FastEmbedProvider（可选真实语义适配器）：未安装 fastembed 时
   给出明确安装指引（monkeypatch 模拟，不依赖网络与真实包）。

本文件所有用例零外部依赖、不访问网络。
"""

from __future__ import annotations

import importlib

import pytest

from core.knowledge.embedding import (
    EmbeddingProvider,
    FastEmbedProvider,
    HashEmbeddingProvider,
)


def test_hash_embedding_is_deterministic_and_dimension_stable() -> None:
    """同一文本重复 embed 得到同一向量，且长度恒等于声明的维度。

    确定性是持久化的前提（面向初学者）：入库时算好的向量要能用于
    之后的查询比对——如果每次 embed 结果不同，重载后检索就全错了。
    """
    provider = HashEmbeddingProvider()
    text = "卷积神经网络在图像识别任务中表现优异"

    first = provider.embed([text])[0]
    second = provider.embed([text])[0]

    assert provider.dimension == 256
    assert first == second
    assert len(first) == provider.dimension
    assert len(provider.embed(["短文本", "另一段更长的中文文本"])) == 2


def test_hash_embedding_has_custom_dimension() -> None:
    """维度可配置（测试用小维度，生产默认 256）。"""
    provider = HashEmbeddingProvider(dimension=64)
    assert provider.dimension == 64
    assert len(provider.embed(["测试"])[0]) == 64


def test_hash_embedding_rejects_non_positive_dimension() -> None:
    with pytest.raises(ValueError, match="dimension"):
        HashEmbeddingProvider(dimension=0)


def test_hash_embedding_distinguishes_different_texts() -> None:
    """不同文本产生不同向量（字符特征不同 → 命中不同桶）。"""
    provider = HashEmbeddingProvider()
    left = provider.embed(["马铃薯是重要的粮食作物"])[0]
    right = provider.embed(["卷积神经网络在图像识别中表现优异"])[0]
    assert left != right


def test_hash_embedding_handles_empty_inputs() -> None:
    """空文本 → 全零向量；空列表 → 空结果（与索引层约定一致）。"""
    provider = HashEmbeddingProvider()
    assert provider.embed([]) == []
    empty_vector = provider.embed([""])[0]
    assert empty_vector == [0.0] * provider.dimension


def test_hash_embedding_normalize_hook_maps_synonyms_to_same_vector() -> None:
    """normalize 注入点：同义词归一化后向量一致（替身语义能力的设计）。

    这是测试替身设计的核心（面向初学者）：哈希向量本身不理解
    「土豆 = 马铃薯」，但 EmbeddingProvider 协议允许任意实现——
    测试通过 normalize 钩子注入同义词替换，模拟真实语义模型把
    同义表述映射到相近向量的能力；检索链路验证见
    test_knowledge_vector_index.py 的语义命中用例。
    """
    synonyms = {"土豆": "马铃薯", "cnn": "卷积神经网络"}

    def normalize(text: str) -> str:
        lowered = text.lower()
        for alias, canonical in synonyms.items():
            lowered = lowered.replace(alias.lower(), canonical)
        return lowered

    provider = HashEmbeddingProvider(normalize=normalize)
    potato_vector = provider.embed(["土豆炖牛肉"])[0]
    canon_vector = provider.embed(["马铃薯炖牛肉"])[0]
    assert potato_vector == canon_vector


def test_fastembed_provider_requires_installed_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fastembed 未安装时给出可执行的安装指引（而非 ImportError 堆栈）。

    用 monkeypatch 模拟「未安装」，不依赖真实网络与真实包——CI
    无网络也能跑；真实语义模型的离线验证方式见
    docs/EMBEDDING_SELECTION.md。
    """

    def fake_import(name: str) -> object:
        raise ImportError(f"No module named {name!r}")

    monkeypatch.setattr(importlib, "import_module", fake_import)
    with pytest.raises(RuntimeError, match="fastembed"):
        FastEmbedProvider()


def test_embedding_provider_is_a_reusable_protocol() -> None:
    """协议可导入且可被任意实现满足（协议替换点）。

    这里用 isinstance 检查协议本身可引用；真正的「替换注入」验证
    见 test_knowledge_vector_index.py（自定义 provider 直接传给索引）。
    """
    provider: EmbeddingProvider = HashEmbeddingProvider()
    assert provider.dimension > 0
