"""Embedding 提供方协议与内置实现（S3-T4 向量检索的向量来源层）。

设计说明（按功能模块，面向初学者）：

1. 为什么需要「可替换协议」
   向量检索的第一步是把文本变成数字向量。谁来变、怎么变，决定了
   检索的「语义能力」：真实语义模型（如 bge-small-zh）能理解
   「土豆 = 马铃薯」这样的同义关系，而字符级哈希向量不能。
   为了让系统不绑定某一家实现，这里定义 `EmbeddingProvider` 协议：
   任何实现只需要会做一件事——`embed(texts)` 把一批文本变成一批
   等长向量。向量索引只依赖这个协议，不关心背后是本地模型还是
   在线 API，替换提供方不需要改索引代码（协议替换点）。

2. 两个内置实现
   - `HashEmbeddingProvider`：零依赖的确定性「字符特征哈希」向量，
     项目默认用它（离线、Windows 零风险、测试可复现）。语义能力
     有限，是「降级方案」，详见 docs/EMBEDDING_SELECTION.md；
   - `FastEmbedProvider`：真实语义模型适配器（fastembed +
     bge-small-zh-v1.5，onnxruntime，无需 torch）。采用「用到才导入」
     的惰性加载：不安装 fastembed 不影响项目其它功能，安装后即可用。
     首次使用会联网下载模型（约 100MB，一次性），之后完全离线。

3. 归一化职责划分
   本模块只负责「文本 → 原始向量」；向量的 L2 归一化由向量索引层
   统一完成（见 vector_index.py 的 `_normalize`）。这样即使某个
   提供方忘了归一化，索引的「余弦 = 点积」约定也不会被破坏。
"""

from __future__ import annotations

import importlib
import re
import zlib
from collections.abc import Callable
from typing import Any, Protocol

# 与词法索引（index.py）同一套「英文词 / 中文连续串」切分正则，
# 保证两种索引看到的字符特征空间一致（用途不同：词法用它计数命中，
# 这里用它把特征哈希进向量桶）。
_ENGLISH_WORD = re.compile(r"[A-Za-z0-9]+")
_CHINESE_RUN = re.compile(r"[\u4e00-\u9fff]+")


class EmbeddingProvider(Protocol):
    """Embedding 提供方可替换协议（S3-T4）。

    契约（面向初学者）：
    - `dimension`：向量维度（正整数）。索引层用它校验 embed 结果长度
      一致，防止不同提供方（维度不同）混用导致相似度计算错误；
    - `embed(texts)`：输入一批文本，输出同长度的一批向量，每个向量
      长度必须等于 `dimension`。实现必须是确定性的（同一文本重复调用
      得到同一向量），否则持久化的向量与查询向量对不上。
    """

    dimension: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        """把一批文本转成等长向量列表（每个向量长度 = dimension）。"""
        ...


def _char_features(text: str) -> list[str]:
    """把文本拆成哈希特征列表：英文词（小写）+ 单个汉字 + 相邻汉字二元组。

    为什么选这三类特征（面向初学者）：
    - 英文词整体作为特征，保证 "CNN" 与 "cnn" 落在同一桶（大小写无关）；
    - 单个汉字 + 相邻二元组覆盖中文：中文没有空格分词，二元组是
      最朴素的分词近似，「马铃薯」「铃薯」都能被捕捉到。
    与词法索引的 _lexical_terms 特征同构，但这里是「可重复的特征序列」
    （词频计数用），不是去重后的集合。
    """
    features: list[str] = []
    for match in _ENGLISH_WORD.finditer(text):
        features.append(match.group().lower())
    for match in _CHINESE_RUN.finditer(text):
        run = match.group()
        features.extend(run)  # 每个汉字单独一个特征
        features.extend(run[index : index + 2] for index in range(len(run) - 1))
    return features


class HashEmbeddingProvider:
    """确定性「字符特征哈希」向量（零依赖降级方案，S3-T4 默认实现）。

    原理（面向初学者）：
    1. 把文本拆成小特征（英文词 / 单字 / 二元组，见 _char_features）；
    2. 每个特征用 crc32 哈希到固定维度向量里的一个桶
       （桶号 = crc32(特征) % 维度），桶里累加该特征出现次数
       （词频权重）——「土豆」出现 3 次，它的桶里就累加 3；
    3. 输出原始词频向量，L2 归一化由索引层统一做（职责划分见模块注释）。

    为什么是「降级方案」：哈希只编码字符表面特征，不理解同义关系，
    语义能力有限。它的价值：零依赖、完全确定（同一文本永远同一向量，
    跨进程稳定——crc32 与 Python 内置 hash 不同，不受进程随机化影响，
    这是向量能持久化重载的前提）、离线可用。
    真实语义效果请用 FastEmbedProvider（bge-small-zh-v1.5 离线模型），
    对比与取舍详见 docs/EMBEDDING_SELECTION.md。

    可选参数 normalize（面向初学者）：文本进入特征抽取前的归一化函数，
    如小写化、繁体转简体。测试用它注入「同义词替换」来模拟语义模型
    的等价映射（见 test_knowledge_vector_index.py 的语义命中用例）；
    生产默认 None（不做任何改写）。
    """

    def __init__(
        self,
        *,
        dimension: int = 256,
        normalize: Callable[[str], str] | None = None,
    ) -> None:
        # 维度默认 256：对 1.5 万 chunk 规模足够区分，内存占用小
        # （256 × 4 字节 × 15000 ≈ 15MB）；可配置以便测试用小维度。
        if dimension <= 0:
            raise ValueError("dimension must be positive")
        self.dimension = dimension
        self._normalize = normalize

    def embed(self, texts: list[str]) -> list[list[float]]:
        """把一批文本转成词频哈希向量（未归一化，归一化由索引层做）。"""
        vectors: list[list[float]] = []
        for text in texts:
            if self._normalize is not None:
                text = self._normalize(text)
            vector = [0.0] * self.dimension
            for feature in _char_features(text):
                bucket = zlib.crc32(feature.encode("utf-8")) % self.dimension
                vector[bucket] += 1.0
            vectors.append(vector)
        return vectors


class FastEmbedProvider:
    """fastembed + bge-small-zh-v1.5 的真实语义 Embedding（可选，S3-T4）。

    选型结论（详见 docs/EMBEDDING_SELECTION.md）：bge-small-zh 系列
    在中文语义相似度上表现好、模型小（约 100MB）、完全离线可用
    （首次下载后缓存在本地 ~/.cache/fastembed，之后不再联网），依赖
    比 sentence-transformers 轻（onnxruntime，无需 torch）。
    未把 fastembed 写入项目锁文件的原因：保持依赖锁定与 Windows
    兼容零风险；本类采用「用到才导入」的惰性加载——不安装 fastembed
    时其它功能完全不受影响，安装后本类即可用。

    注意（面向初学者）：首次构造会联网下载模型，因此测试一律用
    HashEmbeddingProvider 替身（CI 无网络也能跑）；真实语义效果的
    人工验证方式见选型文档「真实语义效果的验证方式」一节。

    更换 provider 警告（I-1）：不同模型的向量维度可能不同（本类为
    512，哈希替身为 256）。更换 embedding provider 后，旧向量库的
    维度与新查询向量不一致会被索引加载时拒绝——需要 --force 重新
    入库重建向量库（详见 docs/EMBEDDING_SELECTION.md 第 6 节）。
    """

    def __init__(self, model_name: str = "BAAI/bge-small-zh-v1.5") -> None:
        # 惰性导入 + 明确报错：未安装时给出可执行的安装指引，
        # 而不是一屏 ImportError 堆栈。
        try:
            importlib.import_module("fastembed")
        except ImportError as exc:
            raise RuntimeError(
                "FastEmbedProvider 需要 fastembed 包：请先运行 "
                "`uv pip install fastembed`（或写入 pyproject 的可选依赖组），"
                "首次使用会联网下载 bge-small-zh-v1.5 模型（约 100MB，一次性）"
            ) from exc
        fastembed_module: Any = importlib.import_module("fastembed")  # 已确认可导入
        self._model = fastembed_module.TextEmbedding(model_name=model_name)
        # 维度探测：拿一个占位文本跑一次，取向量长度
        # （bge-small-zh-v1.5 为 512）。注意：首次构造可能触发模型
        # 下载（一次性），之后才从本地缓存加载。
        sample = next(iter(self._model.embed(["维度探测"])))
        self.dimension = len(sample)

    def embed(self, texts: list[str]) -> list[list[float]]:
        """用真实语义模型把文本转成向量（模型输出转成普通 float 列表）。"""
        return [list(map(float, item)) for item in self._model.embed(texts)]


__all__ = ["EmbeddingProvider", "FastEmbedProvider", "HashEmbeddingProvider"]
