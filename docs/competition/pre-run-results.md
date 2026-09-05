# 典型案例预跑结果（ScriptedModel 确定性验证）

> 生成方式：`backend/scripts/prerun_cases.py`（确定性替身模型 + 真实链路组件）。
> 用途：正式《典型案例测试报告》的机制层证据；真实模型冒烟另行记录。
> 环境说明：本机无 DEEPSEEK_API_KEY，真实模型冒烟待配额就绪后按 `docs/competition/typical-case-protocol.md` 执行；本轮全部用例采用确定性替身模型验证系统机制，验收判据均为可复现断言。

## 汇总

| 用例 | 场景 | 结果 | 失败归因 |
| --- | --- | --- | --- |
| C1 | 智能备课 · 教案工作流 | PASS | — |
| C2 | 智能备课 · PPT 课件工作流 | PASS | — |
| C3 | 作业批改 · 逐题评分与学情落库 | PASS | — |
| C4 | 学情诊断与学习路径推荐 | PASS | — |
| C5 | 知识问答 · 检索作答与引用真实性校验 | PASS | — |
| C6 | 多轮对话 · 上下文保持 | PASS | — |

## C1 智能备课 · 教案工作流

- 用户输入：帮我准备《反向传播》的教案，对象是本科二年级
- 验收判据：
  - 工作流四步（collect/draft/generate/review）全部 COMPLETED
  - 步骤产出按 step_id 暂存（step_outputs 含 collect 与 draft）
  - 事件流含 WORKFLOW_STARTED/STEP×4/WORKFLOW_COMPLETED
  - Supervisor 收口说明进入共享历史
- 结果：**PASS**
- 证据：
  - [OK] workflow.status == COMPLETED
  - [OK] 四步全部 COMPLETED
  - [OK] step_outputs 暂存：['collect', 'draft', 'generate', 'review']
  - [OK] 事件链完整（STARTED + STEP×4 + COMPLETED）
  - [OK] 跨步骤上下文累积且收口回答在历史中

## C2 智能备课 · PPT 课件工作流

- 用户输入：为《反向传播》做一份教学 PPT
- 验收判据：
  - 大纲 JSON 通过结构门禁（≥10 页、标题非空）并暂存
  - generate 步落盘闸：产物区出现非空 课件-反向传播.pptx
  - 四步全部 COMPLETED，workflow.artifacts 登记产物
  - review 判 pass，工作流收口
- 结果：**PASS**
- 证据：
  - [OK] workflow.status == COMPLETED
  - [OK] 大纲 JSON 已过结构门禁并暂存
  - [OK] 落盘闸通过：课件-反向传播.pptx（29B）
  - [OK] 产物登记：['课件-反向传播.pptx']

## C3 作业批改 · 逐题评分与学情落库

- 用户输入：请批改我的作业（3 题，含知识点与错因标注）
- 验收判据：
  - grading 通道返回逐题结论，总分由核心侧确定性汇总
  - 逐题记录落库 learning_records（复合幂等键，3 题不丢）
  - 错因标签（概念不清）与知识点随记录落库
- 结果：**PASS**
- 证据：
  - [OK] grading 通道：3 题，总分 15/30（核心侧汇总）
  - [OK] learning_records 落库 3 条（复合幂等键下多题不丢）
  - [OK] 知识点聚合正确：梯度下降 2 次作答，加权正确率 0.5
  - [OK] 错因标签落库：概念不清 ×1

## C4 学情诊断与学习路径推荐

- 用户输入：诊断我的学习情况；并根据薄弱点规划学习路径
- 验收判据：
  - 诊断端点输出薄弱知识点（确定性规则：作答≥2 且正确率<0.6）
  - 学习路径规划落库 path_plan 记录（模型经工具显式存档）
  - 洞察端点回显路径记录与错题归因（新功能端到端）
- 结果：**PASS**
- 证据：
  - [OK] 诊断端点：薄弱点 ['梯度下降']
  - [OK] path_plan 存档成功（record_learning_outcome → learning_records）
  - [OK] 洞察端点端到端：路径回显 + 错因分布 + 正确率趋势（1 天）

## C5 知识问答 · 检索作答与引用真实性校验

- 用户输入：请用知识库解释链式法则在反向传播中的作用
- 验收判据：
  - 检索作答的最终回答携带结构化引用，与真实命中一一对应
  - 引用真实性校验：伪造引用被剔除、真实引用通过（纯函数探针）
  - 真实语料抽查：新入库英文教材可被检索命中（探测项）
- 结果：**PASS**
- 证据：
  - [OK] 检索作答携带 1 条引用，与真实命中一致
  - [OK] 引用校验结论：verified=1 removed=0
  - [OK] 伪造引用探针：越界引用被确定性剔除
  - [OK] 真实语料抽查命中新教材：['rl-sutton', 'rl-sutton', 'rl-sutton']

## C6 多轮对话 · 上下文保持

- 用户输入：（第 2 轮）我刚才说我叫什么名字？
- 验收判据：
  - 第二轮模型输入包含第一轮用户消息（checkpoint 历史注入）
  - get_history 恢复两轮完整对话
- 结果：**PASS**
- 证据：
  - [OK] 第二轮模型输入包含第一轮完整上下文（小明/考研均在）
  - [OK] get_history 恢复 2 轮用户输入

## 附录：知识库数据充实与检索质量（任务一，2026-08-31）

### 新增书目（均为官方免费渠道，版权合规）

| source | 书名 | 来源 | 页数 | 分块数 |
| --- | --- | --- | --- | --- |
| ml-islr | An Introduction to Statistical Learning | statlearning.com（作者官网） | 434 | 1304 |
| ml-uml | Understanding Machine Learning | cs.huji.ac.il（作者官网） | 439 | 1159 |
| rl-sutton | Reinforcement Learning: An Introduction (2nd) | incompleteideas.net（作者官网） | 539 | 1989 |
| nlp-jurafsky | Speech and Language Processing (3rd draft) · N-gram 章 | web.stanford.edu（官方章节） | 26 | 97 |
| ml-esl（ESL） | 未获取：官方已撤下服务器 PDF，仅剩不可达的 Google Drive 入口，按版权要求放弃 | — | — | — |

入库配置：`ingest_books.py --vector --provider fastembed`（bge-small-zh-v1.5，512 维，与既有向量库同维增量合流）；模型下载经 `HF_ENDPOINT=hf-mirror.com + HF_HUB_DISABLE_XET=1` 解决（HF 官方域名与 xet 协议在本环境不可达）。

### 检索质量（`evaluate_retrieval.py`，42 用例，报告见 `docs/perf-evidence/retrieval-eval-20260831-*.md`）

| 配置 | Recall@1 | Recall@5 | MRR |
| --- | --- | --- | --- |
| lexical | 0.643 | 0.929 | 0.747 |
| hybrid（词法+向量） | **0.833** | **1.000** | **0.904** |
| hybrid+rerank | 0.810 | 1.000 | 0.884 |

混合检索相比纯词法 Recall@5 由 0.929 提升至 1.000；`ingest_books.py --verify` 16/16 用例通过（含 4 本新书 8 条英文用例）。

### 过程中发现并修复的入库流程缺陷（均已补测试）

1. blocked 占位条目（课标）缺文件时 `load_manifest` 直接报错，清单无法加载——放宽为仅未阻塞条目强校验文件存在性（`test_load_manifest_accepts_blocked_entry_with_missing_file`）；
2. blocked 占位条目作者未知（空 authors）同样阻断清单加载——同一语义放宽（同一用例覆盖）；
3. 新书 verify 查询词「temporal difference learning SARSA」区分度不足（AIMA 也覆盖该主题）——更换为高区分度查询「SARSA on-policy control algorithm」后通过；启示：verify 用例选题应避开跨书重叠主题。
