# S3-T4 Embedding 选型与向量索引 — 选型记录

> 任务：清单 A（`docs/TASKS_STAGE_1_2.md`）S3-T4「Embedding 选型与向量索引」
> 日期：2026-08-04（实现时）
> 状态：已落地（实现见 `backend/src/core/knowledge/embedding.py`、
> `backend/src/core/knowledge/vector_index.py`、`backend/scripts/ingest_books.py` --vector）

本文档是 S3-T4 的书面选型结论：Embedding 提供方三维对比、向量库对比、
推荐与降级路径、协议设计、真实语义效果的验证方式。面向编程基础较差的
读者，结论与理由都尽量口语化。

---

## 1. 约束与目标

本项目做向量检索要满足四条硬约束：

1. **测试不依赖外部网络**：CI 与日常测试必须离线可跑（在线 Embedding API
   需要替身封装，真实调用不可进测试）；
2. **Windows 兼容**：开发机是 Windows，选型必须避免在 Windows 上安装/运行
   有坑的二进制依赖（如部分 onnxruntime/torch 版本）；
3. **依赖锁定**：新增依赖要写进 `pyproject.toml` 并 `uv lock` 锁定；
4. **规模现实**：当前知识库约 1.5 万 chunk，不是百万级——不需要分布式
   向量库，单机内存/单文件即可。

---

## 2. Embedding 提供方对比（中文效果 / 离线可用性 / 成本）

| 候选 | 中文效果 | 离线可用性 | 成本 | 依赖重量 | 结论 |
| --- | --- | --- | --- | --- | --- |
| **fastembed + bge-small-zh-v1.5**（onnxruntime） | **好**：bge-small-zh 系列在中文语义相似度基准（C-MTEB 等）上表现靠前，512 维，模型约 100MB | **好**：模型首次下载后缓存本地（`~/.cache/fastembed`），之后完全离线；onnxruntime 有官方 Windows wheel | 零（开源模型 + 本地推理） | 中（onnxruntime + tokenizers，无需 torch） | **真实语义路径的推荐** |
| **sentence-transformers 系**（BGE/SBERT，torch） | 好（同上，模型同源） | 好（模型可本地缓存） | 零 | **重**：连带安装 torch（数百 MB ~ 数 GB，Windows 安装慢、体积大） | 对教学项目过重，不选 |
| **在线 API**（OpenAI text-embedding-3、DeepSeek embedding 等） | 好（商业模型） | **差**：每次 embed 都需联网；测试必须替身 | **有持续成本**（按 token 计费）+ 需要 API Key 与网络环境 | 轻（仅 HTTP 客户端，已有 langchain-openai） | 适合云端部署，不适合本项目的离线约束；协议预留了替换点 |
| **自研「确定性哈希向量」**（crc32 字符特征哈希，本项目内置） | **有限**：只编码字符表面特征，不理解同义关系（「土豆」≠「马铃薯」） | **完全离线**：零依赖、零下载 | 零 | **零**（纯标准库） | **默认实现**：保证任何环境都能跑通向量检索链路（见第 4 节） |

### 推荐结论

- **真实语义效果的推荐路径**：`fastembed + bge-small-zh-v1.5`（中文好 +
  完全离线 + 比 torch 轻）。
- **项目默认实现**：内置 `HashEmbeddingProvider`（零依赖哈希向量）。
  理由：本任务落地时环境无 shell 无法实际安装验证 fastembed，且哈希方案
  天然满足「依赖锁定、Windows 零风险、测试确定性」三条硬约束；
  `FastEmbedProvider` 适配器已封装好（惰性导入），将来在任意环境
  `uv pip install fastembed` 后把 ingest 脚本里的一行 provider 换掉即可
  切换真实语义，**索引代码与测试零改动**——这就是协议可替换的价值。

  ⚠️ **更换 provider 需删除旧向量库后重建（I-1 防护）**：不同模型的
  向量**维度可能不同**（哈希替身 256 维，bge-small-zh-v1.5 为 512 维）。
  换 provider 后旧向量库与查询向量维度不一致，向量索引加载旧库时会直接
  报错（不会静默截断算错）。注意 `--force` 无法绕过：维度校验发生在
  打开旧库的构造期，早于 `--force` 清完成标记——请删除向量库文件
  （或改用新的 `--vector-db` 路径）后重新入库重建。
- 哈希向量是**降级方案**：语义能力有限，只证明「向量检索链路」本身；
  其真实语义差距的弥补方式见第 5 节。

---

## 3. 向量库对比（Chroma vs 自研）

| 候选 | 优点 | 缺点 | 结论 |
| --- | --- | --- | --- |
| **Chroma**（清单建议「Chroma 先行」） | 自带持久化、ANN 索引、元数据过滤、成熟生态 | **重依赖**：chromadb 会连带安装 onnxruntime、numpy、pydantic 等多个二进制包，Windows 偶有安装/版本兼容问题；锁文件显著膨胀；对本项目 1.5 万 chunk 规模是「杀鸡用牛刀」（ANN 索引在万级规模收益有限，暴力余弦扫描已够快） | 不选（记录在案，规模上量后再评估） |
| **自研：numpy 矩阵 + SQLite 存向量** | 依赖轻（numpy 有 Windows wheel）、持久化简单 | numpy 仍是新增依赖；矩阵与 SQLite 的同步逻辑要自己写 | 备选（比纯标准库多一点依赖） |
| **自研：纯标准库 + SQLite 存 BLOB**（本项目采用） | **零新增依赖**（math/struct/sqlite3 全是标准库）、Windows 零风险、锁文件零变化、代码完全可控可教学 | 纯 Python 点积比 numpy 慢一个数量级（1.5 万 × 256 维约零点几秒/次，教学系统可接受）；无 ANN，万级以上全扫描 | **推荐（本项目）** |

### 推荐结论

自研「纯标准库 + SQLite BLOB」：

- 向量以 float32 二进制存 SQLite BLOB（`struct` 打包，约 1KB/256 维），
  检索时整库载入内存矩阵（1.5 万 × 256 × 4B ≈ 15MB），SQLite 只做持久化
  与重载——SQLite 不适合向量距离计算（全表扫描 + 逐行解包比内存慢一个
  数量级），内存矩阵是正确分工；
- 数据文件放 `data/vector_knowledge.db`，随 `data/` 整体不进 git（现有
  `.gitignore` 已覆盖）；
- 升级路径：规模再大时把 `vector_index.py` 的 `_dot` 换成 numpy 矩阵乘法，
  或整体换 Chroma/FAISS——存储表结构与 `KnowledgeIndex` 协议都不用变。

---

## 4. 架构：两个协议、两套索引并存

```
EmbeddingProvider（embedding.py，可替换）
   ├── HashEmbeddingProvider    ← 默认：零依赖哈希向量（降级方案）
   └── FastEmbedProvider        ← 可选：fastembed + bge-small-zh-v1.5（真实语义）

KnowledgeIndex（index.py，可替换）
   ├── InMemoryKnowledgeIndex / SqliteKnowledgeIndex   ← 词法（S3-T1 起，默认路径）
   └── InMemoryVectorKnowledgeIndex / SqliteVectorKnowledgeIndex  ← 向量（S3-T4 新增）
         └── 内部依赖 EmbeddingProvider
```

- **并存而非替换**：词法索引（字符命中打分）确定、可解释、零依赖，是
  `search_knowledge` 工具的现状默认路径，行为不变；向量索引（余弦相似度）
  能命中同义表述，作为可选能力接入（ingest `--vector` 开关）。
- **KnowledgeService 零改动**：它只依赖 `KnowledgeIndex` 协议，构造时传入
  哪个索引就走哪条路（协议替换点）。`search_knowledge` 工具与 API 层
  （红线范围）完全不动。
- **metadata 过滤复用**：向量索引的 `search` 同样支持 S3-T3 的
  `metadata_filter`，直接复用词法索引的校验/匹配函数，两套索引过滤语义
  完全一致（先过滤 → 打分排序 → top_k 截断）。
- **S3-T5 预留**：混合检索（向量 + 词法融合）在服务层做，两个索引各自
  独立检索即可，本任务不涉及。

---

## 5. 真实语义效果的验证方式（替身之外的证明路径）

当前测试全部用替身（同义词归一化哈希向量）证明「检索链路正确」——
即：**给定一个能把同义表述映射到相近向量的 Embedding，向量索引就能命中
词法索引无法命中的表述**（用例：土豆/马铃薯、CNN/卷积神经网络）。

真实语义效果（哈希替身不具备的部分）建议按以下任一方式人工验证：

1. **fastembed 离线模型**（推荐）：安装 fastembed 后，用
   `FastEmbedProvider` 对真实语料（如 data/books 已入库文本）构建向量库，
   再用同义表述查询（「CNN 的原理」「土豆的营养」等）核对命中——模型
   首次下载后完全离线，不污染测试；
2. **在线 API 对照**：用 OpenAI/DeepSeek embedding 在开发环境跑同样的
   用例对照（成本极低，仅开发期用，测试不依赖）。

无论哪种方式，**代码路径与替身完全一致**（同一个 `EmbeddingProvider`
协议、同一个向量索引），验证的只是 Embedding 质量本身。

---

## 6. 依赖变更记录

- **运行依赖新增：0 个**（`pyproject.toml` 未改动，锁文件无需更新）。
  向量检索用 math / struct / sqlite3 / zlib（crc32 特征哈希）全部是
  Python 标准库；
- **更换 provider 的约束**：不同 provider 的向量维度可能不同（哈希替身
  256 维、bge-small-zh-v1.5 为 512 维），切换后需删除旧向量库文件（或
  改用新的 `--vector-db` 路径）再重新入库——`--force` 无法绕过维度
  守卫：维度校验发生在加载旧库的构造期（`_load_all`），早于 `--force`
  清完成标记；向量索引加载旧库时会给出明确报错，`_dot` 也有长度断言，
  不会静默截断计算（详见 vector_index.py 注释）。
- **可选依赖**：fastembed 未写入锁文件（避免重依赖与 Windows 安装风险），
  需要时 `uv pip install fastembed` 即可（`FastEmbedProvider` 惰性导入，
  未安装时给出明确安装指引，不影响项目其它功能）。
- 若后续决定把 fastembed 转正，建议加入 `pyproject.toml` 的
  optional-dependencies（如 `[project.optional-dependencies].embedding`）并
  `uv lock`，届时在 Windows 上实际安装验证一次。

---

## 7. 测试替身设计说明（给维护者的备忘）

- `test_knowledge_embedding.py`：哈希替身的确定性、维度契约、normalize
  注入点、fastembed 未安装报错（monkeypatch 模拟，不依赖网络）；
- `test_knowledge_vector_index.py`：
  - `_FixedVectorProvider`（文本 → 手工向量）：精确断言余弦排序/归一化/
    过滤，不依赖哈希细节；
  - `_synonym_provider`（normalize 同义词替换 + 哈希）：模拟语义模型的
    等价映射，证明「语义命中链路」；
  - 持久化重载、ingest 同步与增量补建用例均离线可跑。
- 维护点：若未来默认 provider 换成 FastEmbedProvider，这些测试不需要改
  （替身仍然有效）；只有「真实语义效果」的验证才需要真实模型（第 5 节）。
