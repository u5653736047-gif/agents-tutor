# 挑战杯项目任务分解清单
## 多智能体助教助学系统

> 更新时间：2026-08-02
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

### 进度统计口径

- `[x]`：三级编号任务已经完整达到当前清单定义的目标。
- `[ ] **（部分完成）**`：已有可验证实现，但仍有明确子项未完成。
- `[ ]`：尚未开始，或没有足够证据证明已经完成。
- 只统计 `1.1.1`、`2.3.2` 这类三级编号任务；更细子项用于解释完成边界，
  不重复计入总数。

| 状态 | 数量 |
|------|-----:|
| 已完成 | 3 |
| 部分完成 | 14 |
| 尚未开始 | 30 |
| **未完整完成（部分完成 + 尚未开始）** | **44** |
| **三级编号任务总数** | **47** |

### 当前冲刺：推送前安全加固与关键注释

- [x] 三个独立子代理复审运行时、持久化和知识层。
- [x] 形成并提交加固设计文档（`985bd51`）。
- [x] 审阅设计文档并形成逐步 TDD 实施计划（`633c94a`）。
- [x] 修复 checkpoint 恢复、权限缺省、异常脱敏和并发边界
  （`b3437fd`、`c861419`、`c8039b7`、`fd9ba96`）。
- [x] 修复知识来源、Citation 一致性和重复文档坐标问题。
  - [x] 知识模型、Citation 与检索 Observation 仅暴露逻辑 source（S0-T1）。
  - [x] 重复写入替换与 chunk 坐标一致性已覆盖（S0-T2）。
- [x] 为状态 reducer、恢复流程、上下文裁剪和知识替换语义补充关键注释。
- [x] 再次执行多代理 review、全量测试、静态检查和真实 DeepSeek
  冒烟验证。
  - [x] 真实 DeepSeek 两轮 ReAct 冒烟通过并记录脱敏日志（S0-T3）。
  - [x] Sprint 0 独立代码 review 与最终三项门禁复核通过（S0-T4）。
- [x] 创建加固提交并推送 `soldier`，核对远端提交哈希
  （2026-08-02 已推送，远端 HEAD 与本地一致）。

### 当前冲刺之后的建议顺序

1. **收口 M1 运行时能力**：完成 1.1.3 的任务分解/聚合、1.1.4 人机断点和
   1.2.2 工具超时，形成稳定的 Agent 框架基线。
2. **形成最小教学闭环**：优先实现 2.1 的意图识别、分层讲解、评价规则，以及
   2.3.3 最终回答 Citation，先覆盖“答疑”场景。
3. **进入阶段三 API**：从 3.1.1 RESTful 会话/聊天接口开始，再实现 WebSocket
   流式事件，最后启动 Next.js 前端。

### 阶段一：智能体框架搭建（Python 端）

> 优先级：P0（关键路径，所有后续任务的基础）

#### 1.1 LangGraph 多智能体基础架构

**目标**：基于 LangGraph 的 StateGraph 构建可扩展的多智能体运行时。

- [x] 1.1.1 设计全局状态 Schema（`AgentState`）
  - [x] 消息历史、当前 Agent、任务上下文、工具结果和会话字段已纳入状态。
  - [x] 顶层使用 TypedDict，嵌套结构使用 Pydantic，并定义 reducer 语义。
- [x] 1.1.2 实现 Agent 节点（Node）抽象
  - [x] 四个角色由同一个 `ReActAgentNode` 和 factory 创建，仅角色与 Prompt 不同。
  - [x] 节点实现“模型决策 → 工具执行 → ToolMessage 观察”的 ReAct 循环。
  - [x] 节点通过 `AgentState` 和 conditional edge 路由。
- [ ] **（部分完成）** 1.1.3 实现协调者（Supervisor）模式
  - [x] Supervisor 可通过 `handoff` 工具分派到三个 Worker，并在完成后回收控制权。
  - [x] 已实现基于模型 Tool Call 的 conditional edge 动态路由。
  - [ ] 复杂请求的显式任务分解和结果聚合策略尚未实现。
  - [ ] LangGraph Send API / fan-out 子代理并行尚未实现。
- [ ] 1.1.4 实现人机交互断点（Human-in-the-loop）
  - 关键决策点暂停等待用户确认
  - 支持用户中途修正 Agent 行为

#### 1.2 工具系统

**目标**：实现可插拔的工具注册与执行机制。

- [ ] **（部分完成）** 1.2.1 工具注册表
  - [x] 使用 LangChain `@tool` / BaseTool 标准工具定义。
  - [x] 已实现名称唯一校验、参数 Schema 和按 Agent 角色授权。
  - [ ] 动态卸载及运行时按角色加载不同工具集尚未实现。
- [x] **1.2.2 工具执行器**
  - [x] 使用 Pydantic Schema 进行参数校验。
  - [x] 工具异常会转换为结构化错误和 ReAct Observation。
  - [x] 工具结果、耗时和安全事件可供评价 Agent 审计。
  - [x] 工具执行支持全局默认与按工具覆盖，超时结果可安全审计（S1-T1）。
- [ ] **（部分完成）** 1.2.3 领域专用工具
  - [x] 已封装 `search_knowledge` 知识检索工具。
  - [ ] Python 代码执行沙箱尚未实现。
  - [ ] LaTeX 公式渲染工具尚未实现。
  - [ ] 学习记录读写工具尚未实现。

#### 1.3 会话与上下文管理

**目标**：支持多轮对话、多会话隔离、上下文窗口管理。

- [ ] **（部分完成）** 1.3.1 会话持久化
  - [x] 已接入 LangGraph SQLite Checkpointer，并验证进程重建后的历史恢复。
  - [x] 每个 `user_id + session_id` 拥有独立状态快照。
  - [ ] pending checkpoint 的显式 `resume()` 与并发写保护列入当前加固冲刺。
  - [ ] PostgreSQL checkpointer 尚未实现。
- [ ] **（部分完成）** 1.3.2 上下文窗口管理
  - [x] 已实现按消息数量裁剪，并保留完整 Tool Call/ToolMessage 关系。
  - [x] 已实现 Token 级计数和预算控制，可与消息数量限制叠加（S1-T2）。
  - [ ] 长对话摘要压缩尚未实现。
- [x] 1.3.3 多会话隔离
  - [x] 按 `user_id + session_id` 生成无碰撞 thread key。
  - [x] 已实现会话创建、列表、归档、状态读取和历史回溯。

---

### 阶段二：核心智能体与知识库（Python 端）

> 优先级：P0-P1（核心功能，与阶段一部分并行）

#### 2.1 角色化智能体开发

**目标**：实现各教学角色的专用 Agent。

- [ ] **（部分完成）** 2.1.1 协调 Agent（Supervisor）
  - [x] 已建立极简 Supervisor Prompt、handoff 工具和安全路由边界。
  - [ ] 教学场景意图识别尚未实现。
  - [ ] 复杂任务分解与多结果聚合尚未实现。
- [ ] **（部分完成）** 2.1.2 助教 Agent（Teaching Assistant）
  - [x] 已接入同构 ReAct 节点，并可按权限调用知识检索工具。
  - [ ] 教案/例题生成的结构化工作流尚未实现。
  - [ ] 深度知识讲解和作业评分策略尚未实现。
- [ ] **（部分完成）** 2.1.3 助学 Agent（Learning Assistant）
  - [x] 已接入同构 ReAct 节点，并可按权限调用知识检索工具。
  - [ ] 学生水平建模和分层答疑尚未实现。
  - [ ] 错题分析与知识图谱学习路径规划尚未实现。
- [ ] **（部分完成）** 2.1.4 评价 Agent（Evaluator）
  - [x] 已接入同构 ReAct 节点、运行事件和工具调用结果。
  - [ ] 事实准确性与引用完整性的评价规则尚未实现。
  - [ ] 学习进度分析和自动合规审计策略尚未实现。

#### 2.2 领域知识库构建

**目标**：构建 AI 学科的高质量知识源。

- [ ] 2.2.1 知识源采集与整理
  - 权威教材：《机器学习》(周志华)、《深度学习》(Goodfellow) 等
  - 顶会论文：NeurIPS / ICML / ACL / CVPR 近年高引论文
  - 课程资源：斯坦福 CS229/CS231n/CS224n 公开课件
- [ ] **（部分完成）** 2.2.2 文档解析与分块
  - [x] 已使用 pypdf 实现逐页 PDF 解析，并支持 UTF-8 文本加载。
  - [x] 已实现确定性重叠字符分块及来源、页码、自定义 metadata 保留。
  - [ ] 按章节、段落、公式和代码块进行语义分块尚未实现。
  - [ ] 难度级别、关联概念等领域元数据尚未定义。
- [ ] **（部分完成）** 2.2.3 向量化与索引
  - [x] 已定义可替换的 `KnowledgeIndex` 协议和纯内存中英文词法索引。
  - [ ] Embedding 模型尚未选型和接入。
  - [ ] Chroma / Milvus / pgvector 尚未接入。
  - [ ] 向量、BM25 与元数据过滤的混合检索尚未实现。

#### 2.3 RAG 检索增强系统

**目标**：实现高质量的检索增强生成管线。

- [ ] **（部分完成）** 2.3.1 基础 RAG 管线
  - [x] 已实现文档写入、整文档替换、删除、Top-K 词法检索和 Agent 工具封装。
  - [x] 零命中返回明确空结果，不生成虚假 Citation。
  - [ ] Query 改写、多路联合检索和 Cross-Encoder 重排序尚未实现。
- [ ] 2.3.2 自适应 RAG 策略
  - 判断是否需要检索（简单问题直接回答）
  - 检索结果质量评估（相关性打分，低于阈值则不注入）
  - 多轮检索：首次检索不足时自动 refine query
- [ ] **（部分完成）** 2.3.3 引用溯源
  - [x] 已定义 document/source/page/chunk 结构化 Citation，并随检索结果返回。
  - [ ] 最终回答中的引用插入与真实性校验尚未实现。
  - [ ] 点击查看原文和引用格式规范化尚未实现。

---

### 阶段三：API 服务与前后端桥接

> 优先级：P1（依赖阶段一基本完成）

#### 3.1 Python 后端 API（FastAPI）

**目标**：暴露智能体系统能力为标准化 API。

- [ ] 3.1.1 RESTful API 设计
  - `POST /chat`：发送消息（返回流式响应）
  - `GET /sessions`：会话列表
  - `POST /sessions`：创建新会话
  - `GET /sessions/{id}/history`：历史消息
  - `POST /feedback`：用户反馈收集
- [ ] 3.1.2 WebSocket 流式通信
  - Agent 思考过程实时推送（thinking → tool_call → response）
  - 多 Agent 协作进度可视化事件
  - 断线重连 + 消息补发机制
- [ ] 3.1.3 认证与限流
  - JWT Token 认证
  - 按用户限流（防止滥用）
  - 请求队列管理

#### 3.2 TypeScript 前端（Next.js）

**目标**：构建流畅的用户交互界面。

- [ ] 3.2.1 对话界面
  - 流式消息渲染（SSE / WebSocket 接收）
  - Markdown + LaTeX（KaTeX）+ 代码高亮渲染
  - 多 Agent 回复的视觉区分（角色头像/标签）
- [ ] 3.2.2 会话管理界面
  - 会话列表 + 搜索 + 归档
  - 历史对话回溯
  - 学习进度仪表盘
- [ ] 3.2.3 知识库管理界面（教师端）
  - 文档上传 + 解析状态展示
  - 知识条目浏览与编辑
  - 检索效果测试工具
- [ ] 3.2.4 Agent 协作可视化
  - 实时展示当前活跃的 Agent 及其任务
  - 工具调用过程透明化（可展开/折叠）
  - 子代理并行执行进度条

#### 3.3 前后端集成

- [ ] 3.3.1 API 客户端封装（TypeScript SDK）
  - 类型安全的请求/响应定义（zod schema）
  - 自动重连 + 错误处理
- [ ] 3.3.2 流式渲染管线
  - WebSocket 消息 → 状态更新 → 增量 DOM 渲染
  - Agent 状态机事件驱动 UI 变化
- [ ] 3.3.3 文件上传与多模态输入
  - 图片上传（手写公式识别）
  - PDF 上传（学生作业）
  - 语音输入（可选）

---

### 阶段四：模型优化与高级功能

> 优先级：P2（核心功能稳定后）

#### 4.1 模型微调（Python 端）

- [ ] 4.1.1 训练数据构建
  - 从教材/论文中构造 QA 对
  - 教师标注的高质量回答作为 SFT 数据
  - 学生真实问题 + 教师修正 → 偏好对齐数据
- [ ] 4.1.2 领域适配微调
  - 基座模型选型（Qwen2.5 / GLM-4 / LLaMA-3）
  - LoRA / QLoRA 高效微调
  - 评估：领域 QA 准确率 + 通用能力保持率
- [ ] 4.1.3 部署与推理优化
  - vLLM 部署（高吞吐推理）
  - 量化（AWQ / GPTQ）降低显存需求
  - API 兼容层（OpenAI 格式接口）

#### 4.2 高级 RAG 与知识图谱

- [ ] 4.2.1 知识图谱构建（可选加分项）
  - AI 学科概念图谱（概念 → 前置依赖 → 应用场景）
  - 基于图谱的学习路径推荐
  - 图谱辅助检索（概念关联扩展）
- [ ] 4.2.2 多模态 RAG
  - 公式理解（LaTeX 语义解析）
  - 图表解读（模型架构图、实验结果图）
  - 代码片段语义检索

#### 4.3 工作流编排高级功能

- [ ] 4.3.1 教学场景工作流模板
  - "备课流程"：大纲分析 → 知识点提取 → 例题生成 → 教案输出
  - "答疑流程"：问题理解 → 知识检索 → 分层讲解 → 追问引导
  - "批改流程"：答案解析 → 错误定位 → 评分 → 改进建议
- [ ] 4.3.2 自适应难度调节
  - 根据学生历史表现动态调整回答深度
  - 苏格拉底式引导（逐步提示而非直接给答案）
- [ ] 4.3.3 多 Agent 协作优化
  - 子代理并行执行（LangGraph fan-out/fan-in）
  - Agent 间辩论机制（多视角验证答案正确性）
  - 动态工具注册（运行时按需加载新工具）

---

### 阶段五：系统集成与打磨

> 优先级：P1-P2（贯穿后期）

#### 5.1 系统稳定性

- [ ] 5.1.1 异常处理与恢复
  - Agent 执行超时 → 优雅降级
  - LLM 调用失败 → 自动重试 + 备用模型
  - 会话状态异常 → 从 Checkpoint 恢复
- [ ] 5.1.2 性能优化
  - 检索缓存（热门问题命中缓存）
  - 流式输出首 Token 延迟优化
  - 并发会话压力测试
- [ ] 5.1.3 日志与可观测性
  - LangSmith / LangFuse 追踪 Agent 执行链路
  - 工具调用耗时统计
  - 用户满意度埋点

#### 5.2 用户体验打磨

- [ ] 5.2.1 响应速度感知优化
  - 思考过程实时展示（减少等待焦虑）
  - 骨架屏 + 渐进式内容加载
- [ ] 5.2.2 交互细节
  - 快捷指令（/explain /quiz /path）
  - 回答反馈（点赞/点踩/纠错）
  - 深色模式 + 移动端适配
- [ ] 5.2.3 引导与帮助
  - 首次使用引导
  - 功能提示与使用示例
  - FAQ 与帮助文档

#### 5.3 部署与演示准备

- [ ] 5.3.1 容器化部署
  - Docker Compose 编排（API + 前端 + 向量库 + 模型服务）
  - 环境变量配置化
  - 健康检查 + 自动重启
- [ ] 5.3.2 演示数据准备
  - 预置知识库（AI 学科核心教材）
  - 演示用对话脚本
  - 性能基准测试报告
- [ ] 5.3.3 文档与答辩材料
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
