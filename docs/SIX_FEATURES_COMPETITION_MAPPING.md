# 六大核心功能与赛题要求对照表

> 更新时间：2026-08-31（自 2026-08-26 起为范围权威；2026-08-31 赛前增强：学情洞察可视化 + AI 生成内容标识，见下文标注）
> 赛题：XH-202620 面向一流学科建设的学科垂类大模型与创新应用开发（docs/pdf_content.txt）
> 学科定位：人工智能学科（机器学习/深度学习）
> 实施方案来源：《六大教学功能实现计划》（经 pi agent 三轮审查修订）

## 一、功能 ↔ 赛题原文 ↔ 实现落点

| 功能 | 赛题原文要求 | 意图路由 | 核心实现 | 验收测试 |
|---|---|---|---|---|
| 1 智能备课 | 基于课程标准与教材等，自动生成教学设计、课件素材与课堂活动建议 | lesson_prep → teaching_assistant | 六段教学设计模板 + 课标对齐约定（prompts.py TEACHING_ASSISTANT 卡）；检索 source/difficulty 过滤（knowledge/tools.py P0-3）；课件走 officecli_edit（既有审批+下载回执链）；复杂备课 create_task_plan 拆分 | test_lesson_prep.py |
| 2 作业与试题批改 | 自动批阅客观题，辅助评阅主观题，提供评分依据与改进建议 | evaluation → evaluator | 附件 txt/pdf/OCR 三路提取进上下文（api/attachments.py，30K/100K 字符双护栏）；客观题确定性比对工具 grade_objective_answers（零 LLM，answer_source 披露答案来源）；submit_grading 结构化提交；grading 通道 + GRADING_COMPLETED 事件；_wrap 确定性逐题落库 learning_records（复合幂等键）；ChatResponse.grading + 消息元数据回放（刷新不丢）；前端 grading-card（标注"建议评分，教师复核"） | test_grading.py、test_chat_attachments.py、test_ocr.py |
| 3 学情精准诊断 | 分析学生作业、测验与学习行为数据，智能识别知识薄弱点，生成学情诊断或预警报告 | diagnosis → evaluator（新增意图） | learning_records SQL 聚合（预警规则 attempts≥2 且正确率<0.6，确定性可复现）；evaluator 诊断约定（聚合为准、LLM 只写叙述）；GET /learning/diagnosis/summary 端点（student_id 教师视角参数，产品边界见 api/learning.py docstring）；**2026-08-31 增强**：GET /learning/insights/summary（错题归因分布/正确率趋势/路径存档回显，有界窗口近 30 日/近 20 条）+ /stats 页四张洞察实卡（薄弱点预警/错题归因/趋势/路径回显，洞察拉取失败降级不击穿基础统计） | test_diagnosis_api.py、test_learning_store.py、test_learning_insights_api.py、stats-page.test.ts |
| 4 学习路径规划 | 智能诊断学生知识掌握水平与进度，动态规划并推送个性化学习路径与资源 | learning_path → learning_assistant（新增意图） | intent 感知动态提示词段 _PATH_PLANNING_GUIDANCE；读学习记录 → difficulty 过滤检索 → 结构化路径（阶段/知识点/资源引用/检验点/时长）→ path_plan 存档；动态调整=每轮重读记录库 | test_learning_path.py |
| 5 知识问答与讲解 | 针对学科专业问题提供准确、可追溯的答案解析与分步引导 | answer_question → learning_assistant | 既有 RAG + 引用校验链（可追溯）；分步引导约定（P1）；自适应检索接线（寒暄/纯计算免检索、相关性阈值、启发式精化） | test_leveling.py、test_search_knowledge_adaptive.py |
| 6 学习过程陪伴 | 智能体模拟导师或学伴，提供知识点巩固、错题归因分析、学习策略优化等支持辅导 | study_coaching → learning_assistant（新增意图） | intent 感知动态提示词段 _COACHING_GUIDANCE（导师/学伴语气、苏格拉底式引导）；错题归因四分类（概念不清/审题偏差/计算失误/方法选择）+ error_tag 落库反哺诊断/路径；巩固出题"最久未练优先" | test_study_coaching.py |

## 二、基础设施接线（P0 底座）

| 设施 | 环境变量 | 默认值 | 说明 |
|---|---|---|---|
| 上下文预算 | API_MAX_CONTEXT_TOKENS / API_MAX_CONTEXT_MESSAGES | 524288 / 200 | 背后模型 1M 窗口，512K 为护栏上限而非填充目标；内置保守估算器（中文 1 字符≈1 token） |
| 自适应检索 | API_RETRIEVAL_THRESHOLD | 0.01 | 启发式必要性策略 + 相关性阈值 + 零 LLM 启发式精化器（1 轮） |
| 检索增强（S5） | API_KNOWLEDGE_REWRITE / API_KNOWLEDGE_RERANK / API_RERANK_MODEL | auto / auto / bge-reranker-base | LLM 查询改写（多变体联合检索，未配置 key 自动跳过）+ Cross-Encoder 重排（fastembed 复用 embedding 依赖组，只改顺序不改分数，构造失败自动降级） |
| tool 模式任务计划（S5-A） | 无需配置（tool 模式默认可用） | — | Supervisor 可 `create_task_plan` 建立有序计划，核心层确定性门控逐步执行：乱序/跳步 ask_* 工具层拒绝、失败策略 abort/continue/retry（每计划重试预算 1 次）、ACTIVE 计划禁止覆盖；计划与结果经 ChatResponse/SessionProcess 既有字段可见（前端零改动点亮） | test_tool_mode_planning.py |
| 学习记录库 | API_LEARNING_DB_PATH | data/learning.db | SQLite WAL；复合唯一键 (source_tool_call_id, question_id) 防重放重复入库 |
| OCR | API_OCR_MODE / uv sync --extra ocr | auto | rapidocr-onnxruntime（PP-OCR 同源）；依赖缺失自动降级为友好提示 |
| AI 生成内容标识（2026-08-31，伦理合规硬性承诺的系统证据） | 无需配置 | 常驻 | 每条助手回答气泡内「内容由 AI 生成，仅供参考，重要信息请人工复核」（新旧两条渲染路径均覆盖）；助手附件（生成文件回执）附「文件由 AI 生成」标注；/stats 页脚全局声明；批改卡既有「建议评分，教师复核」口径不变（components/ai-content-notice.tsx） | ai-content-notice.test.ts、conversation-panel.test.ts |
| 附件提取护栏 | API_ATTACHMENT_MAX_CHARS / API_ATTACHMENTS_TOTAL_MAX_CHARS | 30000 / 100000 | 超限截断并附标注；PDF 表格结构化（API_PDF_TABLE_MODE，pdfplumber 可选组，表格转 GFM Markdown）与图片理解三级降级链（API_VISION_*，VLM → OCR → 友好提示） |
| 知识空间/命名空间/FTS预滤/知识树（f9628b3/33867a2） | API_KNOWLEDGE_DB_PATH 等 | — | 命名空间隔离 + FTS5 预滤 + 知识树/空间选择器（`frontend/app/knowledge/page.tsx` 空间选择器一致，`backend/src/core/knowledge/catalog.py` + `service.py`）；低选择性回退阈值 0.97x，见 `docs/perf-evidence/fts-benchmark-2026-08-23.md` |
| FTS 低选择性回退 | — | 0.97x 阈值 | `fts-benchmark-2026-08-23` 实测低选择性查询回退，基准见同名报告；向量/FTS 混合链路已落地 `vector_index.py`/`hybrid.py` 一致 |

## 三、剩余人工动作（不阻塞代码交付）

0. **赛前材料支撑脚本（2026-08-31 已就位）**：试用体验脚本 / 典型案例测试规程 / 3 分钟演示视频脚本见 `docs/competition/`，供《06—效果验证报告》与《07—其他材料》制作执行。
1. **课程标准 PDF 入库**：把课标 PDF 放入 data/books/ 并命名"人工智能学科课程标准.pdf"，删除 knowledge_manifest.json 中 cs-ai-curriculum 条目的 blocked 标记，重跑 `uv run python scripts/ingest_books.py`。
2. **OCR 启用**：演示/部署环境执行 `uv sync --extra ocr`（评委环境未装时自动降级，不影响启动）。
3. **真实模型冒烟**：配置 DEEPSEEK_API_KEY 后跑 `uv run python scripts/verify_deepseek_react.py`，并为六大功能各补一条真实模型端到端冒烟记录（演示证据）。
4. **效果验证报告素材**（赛题 06 材料）：≥2 名真实目标用户试用记录、前后对比数据——依赖真实用户，代码侧已就绪（feedback.jsonl + 诊断端点可出数据）。

## 四、质量门禁现状（2026-08-26，含审查修复轮）

- 后端：1074 pytest 全绿（+1 OCR 真实引擎用例在未装 ocr extra 的环境自动跳过）+ ruff 全绿 + mypy strict 全绿（54 源文件）
- 前端：327 测试全绿（含新增 grading-card 组件测试）+ typecheck 全绿（lint 仅 2 个既有 useVirtualizer 兼容性 warning）
- 契约：openapi.json 与 api.generated.ts 已重导同步（DiagnosisSummary / GradingResultDto / attachments maxItems 等新契约就位）

### 审查修复轮（2026-08-20）

按深度质量审查报告（三视角合并）修复全部 13 项：
- **Critical**：附件提取（磁盘 IO/PDF 解析/OCR 推理）三处调用移入 run_in_threadpool，消除事件循环阻塞
- **Warning**：多选集合比较误判面彻底移除（严格相等语义，异序词不再污染学情库）；attachments 契约 maxItems=10 + 组装层防御截断；刷新恢复正向用例 + SqliteSaver checkpoint 往返测试；前端 grading-card 组件测试；HeuristicQueryRefiner 规则单测 + 集成；student_id 加 Query(max_length=64) 约束 + 教师视角审计日志；批改卡包装 div 条件渲染（消除消息布局漂移）；OCR 真实引擎 importorskip 跳过用例
- **Suggestion**：PDF 逐页累计超限即停；OCR dict 形态按字段名取文本；feedback/overall_comment 统一为 reason 先例的单口径截断；API_RETRIEVAL_THRESHOLD 护栏解析（拒绝 nan/inf/拼写错误）；/healthz ocr 诊断、env 护栏解析、批改工具角色守卫三处测试补齐
