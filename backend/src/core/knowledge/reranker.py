"""Cross-Encoder 重排器：Reranker 协议（retrieval.py 第 7 节）的真实模型实现。

（面向初学者的设计说明，按功能模块）

1. 本模块的位置：检索链路的「精排」组件
   初检（词法/向量/混合 RRF）衡量的是「检索层面的相似」，重排器用更强的
   Cross-Encoder 模型对「查询 × 候选正文」逐对精细打分，把「检索排在前面
   但实际不相关」的候选压下去、把「排名靠后但确实相关」的候选提上来。
   流程位置由检索层固定：初检 → 截候选窗口（max(top_k×2, 10)）→ 重排 →
   截断最终 top_k；本模块只负责「重排」这一步的协议实现。

2. 选型：fastembed TextCrossEncoder（bge-reranker-base）
   - 与 FastEmbedProvider 同一依赖（fastembed 可选依赖组，onnxruntime，
     无需 torch）——重排能力**零新增包**，沿用 embedding 组的装配与
     锁定现状（pyproject 不变）；
   - bge-reranker-base 是中英可用的 Cross-Encoder，候选窗口只有
     max(top_k×2, 10) ≤ 10~20 对，CPU 推理开销在毫秒~百毫秒级，
     对检索路径的延迟影响可控；
   - 首次构造会联网下载模型（约 280MB，一次性），之后完全离线
     （与 FastEmbedProvider 首次下载 bge-small-zh 同一模式）。

3. 只改顺序、不改分数（与检索层的分数契约）
   rerank() 返回重排后的 SearchHit 列表，但**保留每个 hit 的原始
   score**（词法命中数 / RRF 融合分 / 余弦相似度）。原因：
   - retrieval.py 的 Reranker 协议只约定「返回顺序即最终顺序」，不强制
     更新 score——保留原分数是协议内的合法实现；
   - adaptive_search 的相关性阈值判定（top_score ≥ threshold）用的是
     SearchHit.score，其量纲由索引决定（生产为 RRF 融合分）；若把 score
     改写成 Cross-Encoder 分数，阈值量纲会静默改变，既有配置
     （API_RETRIEVAL_THRESHOLD=0.01，RRF 量纲）与审计口径全部失准；
   - 事件与审计链路（retrieval metadata → core 事件）记录的分数保持
     初检量纲，重排只影响「最终展示顺序」，口径清晰可解释。
   若未来需要按 Cross-Encoder 分数做阈值过滤，应新增独立阈值配置，
   而不是复用 RRF 量纲的阈值（量纲问题见 retrieval.py 第 8 节第 2 点）。

4. 可注入的打分函数（测试零模型依赖）
   构造参数 scorer 是「(query, 候选正文列表) -> 分数列表」的批量打分
   callable：None 时按 model_name 惰性构造 fastembed Cross-Encoder
   （未安装 fastembed 抛 RuntimeError，由装配方捕获降级，与
   FastEmbedProvider 同一模式）；测试注入确定性替身，不碰真实模型、
   不联网。打分函数返回的分数长度必须与候选数一致、每项必须是数值，
   否则抛 ValueError（长度不符）/ TypeError（非数值）——异常由检索层
   _safe_rerank 兜底降级为「保持初检候选顺序」，检索永不因重排失败而阻断。
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any

from .models import SearchHit

# 批量打分函数签名：(查询, 候选正文列表) -> 与候选一一对应的分数列表。
BatchScorer = Callable[[str, list[str]], list[float]]

# 默认重排模型：fastembed 支持列表中的中英可用 Cross-Encoder（选型见
# 模块注释第 2 节）；可通过装配层 env（API_RERANK_MODEL）覆盖。
DEFAULT_RERANK_MODEL = "BAAI/bge-reranker-base"


class FastEmbedReranker:
    """用 fastembed Cross-Encoder 对初检候选做精排（Reranker 协议实现）。

    参数（面向初学者）：
    - scorer：批量打分 callable，None 时按 model_name 惰性构造真实
      Cross-Encoder（测试注入确定性替身，零模型零网络）；
    - model_name：fastembed 支持的重排模型名（默认 bge-reranker-base）。

    异常约定：构造期未安装 fastembed → RuntimeError（装配方捕获降级）；
    rerank 期打分函数异常 / 返回长度不符 / 分数非数值 → 抛错，由检索层
    _safe_rerank 降级为保持初检顺序（可用性优先，见 retrieval.py 第 7
    节第 5 点）。
    """

    def __init__(
        self,
        scorer: BatchScorer | None = None,
        *,
        model_name: str = DEFAULT_RERANK_MODEL,
    ) -> None:
        if not model_name.strip():
            raise ValueError("model_name must not be blank")
        self._scorer = scorer if scorer is not None else _fastembed_scorer(model_name)

    def rerank(self, query: str, hits: list[SearchHit], top_k: int) -> list[SearchHit]:
        """对候选重新打分排序：返回重排后的列表，保留原始 score 字段。

        - 空候选直接返回空（不发推理，重排器对空列表的合法裁决与协议
          语义一致——检索层对空返回原样透传）；
        - 平局规则：重排分相同按 chunk_id 升序，与索引层及编排层的
          平局约定一致（结果确定、可复现）；
        - top_k 参数本实现不消费（截断由检索层在重排后统一执行），
          保留签名与协议一致。
        """
        if not hits:
            return []
        documents = [hit.chunk.content for hit in hits]
        scores = self._scorer(query, documents)
        if not isinstance(scores, list) or len(scores) != len(hits):
            raise ValueError("rerank scorer must return one score per candidate")
        numeric_scores: list[float] = []
        for score in scores:
            # bool 是 int 子类，但「True/False 当分数」几乎一定是实现
            # 错误，拒绝而不是静默当 1.0/0.0（与 context.py 的
            # token_counter 校验同一防御哲学）。
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                raise TypeError("rerank scorer must return numeric scores")
            numeric_scores.append(float(score))
        order = sorted(
            range(len(hits)),
            key=lambda index: (-numeric_scores[index], hits[index].chunk.chunk_id),
        )
        return [hits[index] for index in order]


def _load_cross_encoder_class() -> Any:
    """惰性导入 fastembed 的 TextCrossEncoder 类；未安装抛 RuntimeError。

    与 FastEmbedProvider 的惰性导入同一模式：不安装 fastembed 不影响
    项目其它功能；装配方（api/app.py）捕获 RuntimeError 后降级为
    「不重排」，启动不阻断。独立成函数是为了让测试可以整体替换加载
    入口（注入替身 encoder），无需联网下载模型。
    """
    try:
        module: Any = importlib.import_module("fastembed.rerank.cross_encoder")
    except ImportError as exc:
        raise RuntimeError(
            "FastEmbedReranker 需要 fastembed 包：请先运行 "
            "`uv sync --extra embedding`（fastembed 已在 embedding 可选组中），"
            "首次使用会联网下载重排模型（约 280MB，一次性）"
        ) from exc
    return module.TextCrossEncoder


def _fastembed_scorer(model_name: str) -> BatchScorer:
    """构造真实的 Cross-Encoder 批量打分函数（闭包捕获 encoder 实例）。"""
    encoder: Any = _load_cross_encoder_class()(model_name=model_name)

    def score(query: str, documents: list[str]) -> list[float]:
        # fastembed TextCrossEncoder.rerank(query, documents) 返回分数
        # 迭代器（numpy 标量），统一转成普通 float 列表。
        return [float(item) for item in encoder.rerank(query, documents)]

    return score


__all__ = ["DEFAULT_RERANK_MODEL", "BatchScorer", "FastEmbedReranker"]
