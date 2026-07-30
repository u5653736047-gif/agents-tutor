# 挑战杯项目任务分解清单
## 多智能体助教助学系统

> 更新时间：2026-07-30
> 技术栈：Python (LangGraph) + TypeScript (Next.js)

---

## 项目总览

### 核心定位
- **学科垂类**: 人工智能学科领域（机器学习、深度学习、NLP、CV 等）
- **多智能体**: 助教、助学、评价、协调等角色化智能体协同工作
- **场景**: 教师备课辅助 + 学生个性化答疑 + 错题分析 + 学习路径规划
- **技术栈**: Python (LangGraph / LangChain) + TypeScript (Next.js / React)

### 架构分工原则

| 层级 | 技术选型 | 职责 |
|------|----------|------|
| **智能体编排层** | Python + LangGraph | 多 Agent 工作流编排、状态机管理、工具调度、子代理委派 |
| **知识与检索层** | Python + LangChain | RAG 管线、向量检索、知识库管理、文档解析 |
| **模型服务层** | Python + vLLM / API | 大模型推理、微调模型部署、Prompt 管理 |
| **API 网关层** | Python (FastAPI) | RESTful + WebSocket 接口、认证、限流、会话路由 |
| **前端交互层** | TypeScript + Next.js | 用户界面、实时消息渲染、Markdown/LaTeX 展示、文件上传 |
| **实时通信层** | WebSocket (双端) | 流式输出推送、Agent 状态同步、协作事件广播 |

### 赛题评分维度对照表
| 评分项 | 分值 | 我们的策略 |
|--------|------|-----------|
| 创意实用度 | 20 | 专注 AI 学科教学痛点，角色化多智能体协作解决真实教学问题 |
| 技术实现度 | 20 | LangGraph 工作流编排 + RAG + 微调 + 多模态（公式/代码/图表） |
| 内容质量度 | 20 | 权威教材 + 顶会论文 + 引用溯源 + 知识图谱辅助 |
| 作品完成度 | 10 | 闭环运行 + 异常恢复 + 会话持久化 |
| 技术先进性 | 10 | LangGraph 状态图 + 子代理并行 + 动态工具注册 + 自适应 RAG |
| 商业化潜力 | 10 | 学科配置化，可复制到其他学科领域 |
| 用户认可度 | 10 | 真实教师/学生试用反馈 + 交互体验打磨 |

---

## 核心任务拆解

### 阶段一：智能体框架搭建（Python 端）

> 优先级：P0（关键路径，所有后续任务的基础）

#### 1.1 LangGraph 多智能体基础架构

**目标**：基于 LangGraph 的 StateGraph 构建可扩展的多智能体运行时。

- 1.1.1 设计全局状态 Schema（`AgentState`）
  - 消息历史（messages）、当前活跃 Agent、任务上下文、工具调用结果
  - 使用 TypedDict / Pydantic 定义强类型状态
- 1.1.2 实现 Agent 节点（Node）抽象
  - 每个 Agent 角色（助教/助学/评价/协调）作为一个 Graph Node
  - 节点内部遵循"思考 → 决策 → 执行 → 观察"循环
  - 节点间通过 State 传递上下文，由 Edge 条件路由
- 1.1.3 实现协调者（Supervisor）模式
  - 中心协调 Agent 负责任务分解与子任务分派
  - 基于 LLM 决策的动态路由（conditional_edge）
  - 支持子代理并行执行（LangGraph 的 Send API / fan-out）
- 1.1.4 实现人机交互断点（Human-in-the-loop）
  - 关键决策点暂停等待用户确认
  - 支持用户中途修正 Agent 行为

#### 1.2 工具系统

**目标**：实现可插拔的工具注册与执行机制。

- 1.2.1 工具注册表
  - 基于 LangChain `@tool` 装饰器的标准工具定义
  - 支持动态注册/卸载（运行时按 Agent 角色加载不同工具集）
  - 工具元数据：名称、描述、参数 Schema、权限级别
- 1.2.2 工具执行器
  - 参数校验（Pydantic Schema 验证）
  - 超时控制 + 异常捕获 → 结构化错误返回
  - 执行日志记录（供评价 Agent 审计）
- 1.2.3 领域专用工具
  - 知识库检索工具（对接 RAG 管线）
  - 代码执行沙箱（Python 代码片段运行）
  - 公式渲染工具（LaTeX → 图片）
  - 学习记录读写工具

#### 1.3 会话与上下文管理

**目标**：支持多轮对话、多会话隔离、上下文窗口管理。

- 1.3.1 会话持久化
  - LangGraph Checkpointer（SQLite / PostgreSQL 后端）
  - 每个会话独立的状态快照，支持断点恢复
- 1.3.2 上下文窗口管理
  - Token 计数 + 滑动窗口裁剪
  - 关键信息摘要压缩（长对话自动 summarize）
- 1.3.3 多会话隔离
  - 按 user_id + session_id 隔离状态
  - 支持会话列表、切换、历史回溯

---

### 阶段二：核心智能体与知识库（Python 端）

> 优先级：P0-P1（核心功能，与阶段一部分并行）

#### 2.1 角色化智能体开发

**目标**：实现各教学角色的专用 Agent。

- 2.1.1 协调 Agent（Supervisor）
  - 意图识别：判断用户请求属于哪个教学场景
  - 任务分解：将复杂请求拆分为子任务
  - 结果聚合：汇总各子 Agent 输出，生成最终回复
- 2.1.2 助教 Agent（Teaching Assistant）
  - 备课辅助：根据教学大纲生成教案、例题
  - 知识讲解：结合 RAG 检索进行深度讲解
  - 作业批改辅助：分析学生答案，给出评分建议
- 2.1.3 助学 Agent（Learning Assistant）
  - 个性化答疑：根据学生水平调整解释深度
  - 错题分析：定位知识薄弱点，推荐补充材料
  - 学习路径规划：基于知识图谱推荐下一步学习内容
- 2.1.4 评价 Agent（Evaluator）
  - 回答质量评估：检查事实准确性、引用完整性
  - 学习进度追踪：记录并分析学生交互数据
  - 工具调用审计：监控其他 Agent 的行为合规性

#### 2.2 领域知识库构建

**目标**：构建 AI 学科的高质量知识源。

- 2.2.1 知识源采集与整理
  - 权威教材：《机器学习》(周志华)、《深度学习》(Goodfellow) 等
  - 顶会论文：NeurIPS / ICML / ACL / CVPR 近年高引论文
  - 课程资源：斯坦福 CS229/CS231n/CS224n 公开课件
- 2.2.2 文档解析与分块
  - PDF 解析（PyMuPDF / Unstructured）
  - 智能分块策略：按章节/段落/公式/代码块语义切分
  - 元数据标注：来源、章节、难度级别、关联概念
- 2.2.3 向量化与索引
  - Embedding 模型选型（BGE / text-embedding-3）
  - 向量数据库（Chroma / Milvus / pgvector）
  - 混合检索：向量相似度 + BM25 关键词 + 元数据过滤

#### 2.3 RAG 检索增强系统

**目标**：实现高质量的检索增强生成管线。

- 2.3.1 基础 RAG 管线
  - Query 改写：扩展/分解用户问题以提高召回率
  - 多路检索：向量 + 关键词 + 知识图谱联合检索
  - 重排序（Reranker）：Cross-Encoder 精排
- 2.3.2 自适应 RAG 策略
  - 判断是否需要检索（简单问题直接回答）
  - 检索结果质量评估（相关性打分，低于阈值则不注入）
  - 多轮检索：首次检索不足时自动 refine query
- 2.3.3 引用溯源
  - 回答中标注知识来源（教材页码 / 论文 DOI）
  - 支持用户点击查看原文出处
  - 引用格式规范化

---

### 阶段三：API 服务与前后端桥接

> 优先级：P1（依赖阶段一基本完成）

#### 3.1 Python 后端 API（FastAPI）

**目标**：暴露智能体系统能力为标准化 API。

- 3.1.1 RESTful API 设计
  - `POST /chat`：发送消息（返回流式响应）
  - `GET /sessions`：会话列表
  - `POST /sessions`：创建新会话
  - `GET /sessions/{id}/history`：历史消息
  - `POST /feedback`：用户反馈收集
- 3.1.2 WebSocket 流式通信
  - Agent 思考过程实时推送（thinking → tool_call → response）
  - 多 Agent 协作进度可视化事件
  - 断线重连 + 消息补发机制
- 3.1.3 认证与限流
  - JWT Token 认证
  - 按用户限流（防止滥用）
  - 请求队列管理

#### 3.2 TypeScript 前端（Next.js）

**目标**：构建流畅的用户交互界面。

- 3.2.1 对话界面
  - 流式消息渲染（SSE / WebSocket 接收）
  - Markdown + LaTeX（KaTeX）+ 代码高亮渲染
  - 多 Agent 回复的视觉区分（角色头像/标签）
- 3.2.2 会话管理界面
  - 会话列表 + 搜索 + 归档
  - 历史对话回溯
  - 学习进度仪表盘
- 3.2.3 知识库管理界面（教师端）
  - 文档上传 + 解析状态展示
  - 知识条目浏览与编辑
  - 检索效果测试工具
- 3.2.4 Agent 协作可视化
  - 实时展示当前活跃的 Agent 及其任务
  - 工具调用过程透明化（可展开/折叠）
  - 子代理并行执行进度条

#### 3.3 前后端集成

- 3.3.1 API 客户端封装（TypeScript SDK）
  - 类型安全的请求/响应定义（zod schema）
  - 自动重连 + 错误处理
- 3.3.2 流式渲染管线
  - WebSocket 消息 → 状态更新 → 增量 DOM 渲染
  - Agent 状态机事件驱动 UI 变化
- 3.3.3 文件上传与多模态输入
  - 图片上传（手写公式识别）
  - PDF 上传（学生作业）
  - 语音输入（可选）

---

### 阶段四：模型优化与高级功能

> 优先级：P2（核心功能稳定后）

#### 4.1 模型微调（Python 端）

- 4.1.1 训练数据构建
  - 从教材/论文中构造 QA 对
  - 教师标注的高质量回答作为 SFT 数据
  - 学生真实问题 + 教师修正 → 偏好对齐数据
- 4.1.2 领域适配微调
  - 基座模型选型（Qwen2.5 / GLM-4 / LLaMA-3）
  - LoRA / QLoRA 高效微调
  - 评估：领域 QA 准确率 + 通用能力保持率
- 4.1.3 部署与推理优化
  - vLLM 部署（高吞吐推理）
  - 量化（AWQ / GPTQ）降低显存需求
  - API 兼容层（OpenAI 格式接口）

#### 4.2 高级 RAG 与知识图谱

- 4.2.1 知识图谱构建（可选加分项）
  - AI 学科概念图谱（概念 → 前置依赖 → 应用场景）
  - 基于图谱的学习路径推荐
  - 图谱辅助检索（概念关联扩展）
- 4.2.2 多模态 RAG
  - 公式理解（LaTeX 语义解析）
  - 图表解读（模型架构图、实验结果图）
  - 代码片段语义检索

#### 4.3 工作流编排高级功能

- 4.3.1 教学场景工作流模板
  - "备课流程"：大纲分析 → 知识点提取 → 例题生成 → 教案输出
  - "答疑流程"：问题理解 → 知识检索 → 分层讲解 → 追问引导
  - "批改流程"：答案解析 → 错误定位 → 评分 → 改进建议
- 4.3.2 自适应难度调节
  - 根据学生历史表现动态调整回答深度
  - 苏格拉底式引导（逐步提示而非直接给答案）
- 4.3.3 多 Agent 协作优化
  - 子代理并行执行（LangGraph fan-out/fan-in）
  - Agent 间辩论机制（多视角验证答案正确性）
  - 动态工具注册（运行时按需加载新工具）

---

### 阶段五：系统集成与打磨

> 优先级：P1-P2（贯穿后期）

#### 5.1 系统稳定性

- 5.1.1 异常处理与恢复
  - Agent 执行超时 → 优雅降级
  - LLM 调用失败 → 自动重试 + 备用模型
  - 会话状态异常 → 从 Checkpoint 恢复
- 5.1.2 性能优化
  - 检索缓存（热门问题命中缓存）
  - 流式输出首 Token 延迟优化
  - 并发会话压力测试
- 5.1.3 日志与可观测性
  - LangSmith / LangFuse 追踪 Agent 执行链路
  - 工具调用耗时统计
  - 用户满意度埋点

#### 5.2 用户体验打磨

- 5.2.1 响应速度感知优化
  - 思考过程实时展示（减少等待焦虑）
  - 骨架屏 + 渐进式内容加载
- 5.2.2 交互细节
  - 快捷指令（/explain /quiz /path）
  - 回答反馈（点赞/点踩/纠错）
  - 深色模式 + 移动端适配
- 5.2.3 引导与帮助
  - 首次使用引导
  - 功能提示与使用示例
  - FAQ 与帮助文档

#### 5.3 部署与演示准备

- 5.3.1 容器化部署
  - Docker Compose 编排（API + 前端 + 向量库 + 模型服务）
  - 环境变量配置化
  - 健康检查 + 自动重启
- 5.3.2 演示数据准备
  - 预置知识库（AI 学科核心教材）
  - 演示用对话脚本
  - 性能基准测试报告
- 5.3.3 文档与答辩材料
  - 系统架构文档
  - API 接口文档
  - 技术亮点总结 PPT
  - 演示视频录制

---

## 技术关键路径

```
阶段一（1.1 → 1.2 → 1.3）──→ 阶段三（3.1 → 3.3）──→ 阶段五
       │                              ↑
       ↓                              │
阶段二（2.1 → 2.2 → 2.3）────────────┘
       │
       ↓
阶段四（4.1 / 4.2 / 4.3 可并行）
```

**关键依赖链**：
1. LangGraph 基础架构 (1.1) → 角色 Agent 开发 (2.1) → API 暴露 (3.1) → 前端对接 (3.2)
2. 知识库构建 (2.2) → RAG 管线 (2.3) → 工具注册 (1.2.3) → Agent 集成
3. 前后端桥接 (3.3) 依赖 API 层 (3.1) 和前端框架 (3.2) 基本就绪

---

## Python / TypeScript 集成方案

### 通信架构

```
┌─────────────────────────────────────────────────────────┐
│  TypeScript 前端 (Next.js)                               │
│  • React 组件 + 状态管理                                 │
│  • WebSocket Client（接收流式事件）                       │
│  • REST Client（CRUD 操作）                              │
└────────────────────────┬────────────────────────────────┘
                         │ HTTP / WebSocket
                         ▼
┌─────────────────────────────────────────────────────────┐
│  Python 后端 (FastAPI)                                   │
│  • REST API + WebSocket Endpoint                         │
│  • 请求路由 → LangGraph 工作流                           │
│  • 流式事件序列化 → WS 推送                              │
├─────────────────────────────────────────────────────────┤
│  LangGraph 编排层                                        │
│  • StateGraph 定义多 Agent 工作流                        │
│  • Checkpointer 管理会话状态                             │
│  • Tool Executor 调度工具执行                            │
├─────────────────────────────────────────────────────────┤
│  模型 & 检索层                                           │
│  • LLM API / 本地微调模型 (vLLM)                        │
│  • 向量数据库 (Chroma/pgvector)                          │
│  • Embedding + Reranker                                  │
└─────────────────────────────────────────────────────────┘
```

### 流式事件协议（Python → TypeScript）

```typescript
// TypeScript 端事件类型定义
type AgentEvent =
  | { type: "thinking"; agent: string; content: string }
  | { type: "tool_call"; agent: string; tool: string; args: Record<string, unknown> }
  | { type: "tool_result"; agent: string; tool: string; result: string }
  | { type: "message_delta"; agent: string; delta: string }
  | { type: "message_end"; agent: string; message: string; citations: Citation[] }
  | { type: "agent_switch"; from: string; to: string; reason: string }
  | { type: "error"; code: string; message: string }
  | { type: "done"; usage: UsageStats };
```

### 数据模型共享

- Python 端：Pydantic Schema 定义 → 自动生成 OpenAPI Spec
- TypeScript 端：从 OpenAPI Spec 自动生成类型（`openapi-typescript`）
- 确保双端数据结构始终同步

---

## 多智能体协作机制（LangGraph 实现）

### 协调模式：Supervisor + Workers

```python
# 概念性伪代码
from langgraph.graph import StateGraph, END

graph = StateGraph(AgentState)

# 注册 Agent 节点
graph.add_node("supervisor", supervisor_agent)
graph.add_node("teaching_assistant", ta_agent)
graph.add_node("learning_assistant", la_agent)
graph.add_node("evaluator", eval_agent)

# 协调者路由：根据意图分派到不同 Agent
graph.add_conditional_edges("supervisor", route_by_intent, {
    "teach": "teaching_assistant",
    "learn": "learning_assistant",
    "evaluate": "evaluator",
    "done": END,
})

# 各 Agent 完成后回到协调者汇总
graph.add_edge("teaching_assistant", "supervisor")
graph.add_edge("learning_assistant", "supervisor")
graph.add_edge("evaluator", "supervisor")
```

### 子代理并行执行

```python
# LangGraph Send API 实现 fan-out
from langgraph.constants import Send

def supervisor_parallel_dispatch(state):
    """将独立子任务并行分派给多个 Agent"""
    tasks = decompose_task(state["current_request"])
    return [Send("worker", {"task": t}) for t in tasks]
```

---

## 里程碑与交付物

| 里程碑 | 交付物 | 验收标准 |
|--------|--------|----------|
| M1: 框架就绪 | LangGraph 多 Agent 骨架 + 工具系统 | 协调者能分派任务给 2 个子 Agent 并汇总 |
| M2: 知识闭环 | RAG 管线 + 知识库 | 能基于教材内容回答 AI 学科问题并标注来源 |
| M3: 前后端联通 | API + 前端对话界面 | 用户可通过 Web 界面与多 Agent 流式对话 |
| M4: 功能完整 | 全部角色 Agent + 高级 RAG | 覆盖备课/答疑/批改/路径规划四大场景 |
| M5: 演示就绪 | 部署 + 文档 + 演示数据 | 可稳定运行 30 分钟完整演示 |
