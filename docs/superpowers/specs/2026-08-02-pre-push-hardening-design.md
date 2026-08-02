# 推送前安全加固与关键注释设计

## 背景

阶段 0–3 已形成四个本地提交。推送前的三路独立复审确认：整体架构符合
统一 ReAct、SQLite 会话持久化和轻量知识检索目标，但仍存在 checkpoint 恢复、
权限缺省、异常脱敏、引用一致性等边界问题。本轮只修复已确认的问题，并给不直观
的不变量补充简短中文注释。

## 目标

- 保证待执行 checkpoint 不会被新一轮输入覆盖，并提供明确的恢复入口。
- 让业务工具权限 fail-closed，防止漏配时 Supervisor 获得业务工具。
- 阻止模型、工具异常和未知工具名把敏感文本写入 Observation、事件或 checkpoint。
- 保证模型上下文中的 Tool Call/ToolMessage 组完整且有明确硬边界。
- 使 SQLite 会话组件在同一实例跨线程调用时保持正确。
- 保证知识来源可公开、Citation 与命中 chunk 一致、分块 ID 不静默碰撞。
- 只在关键逻辑处解释“为什么”，不增加逐行复述式注释。

## 非目标

- 不改为异步 Graph，不提供跨进程分布式锁。
- 不引入日志平台、向量数据库、Embedding、reranker 或文档版本系统。
- 不翻译全部英文 docstring，不重构无关代码。
- 不修改或提交用户已有的 `.gitignore` 变更。

## 设计

### 1. Graph 与工具安全边界

`CollaborativeAgentGraph` 收到业务工具时，要求 `tool_permissions` 为每个工具
显式给出角色集合；未知、缺失或多余映射都在构造时失败。`handoff` 仍只允许
Supervisor。

ToolExecutor 只把注册表中的规范工具名写入状态；未知名称统一记为固定哨兵。
模型和工具异常对外只返回稳定的错误分类与脱敏文本，不保存原始异常字符串。
Prompt 测试改为按角色逐项比较，防止角色 Prompt 互换后仍通过。

### 2. Checkpoint 恢复与同步调用

配置 checkpointer 后，`run()` 在写入新用户消息前检查 `StateSnapshot.next`：

- 有待执行节点时拒绝启动新一轮，并提示调用 `resume()`。
- `resume(session_id, user_id)` 使用 `invoke(None, config)` 从待执行节点继续。
- 无待执行节点或无会话时，`resume()` 返回明确错误。

Graph 实例使用一个轻量同步锁包住“读取 snapshot → invoke”的完整区间，避免同一
实例的并发调用从同一 checkpoint 分叉。该锁刻意保持简单，会串行化不同会话；
跨实例或跨进程并发仍由部署层协调。

`task_context` 和 `extra` 明确定义为会话持久字段；每轮只重置路由目标、运行错误、
handoff 次数和 Agent 切换次数。

### 3. 上下文完整性

裁剪器继续把 `max_messages` 视为历史窗口目标，但只允许“最新用户消息”额外占用
一个位置。完整 Tool Call 组只有在这个硬边界内才整体保留；缺少任一结果或组过大
时整体丢弃，绝不向模型提供孤立 ToolMessage 或未完成的 AI tool-call。

### 4. SQLite 会话元数据

`SessionStore` 使用 `check_same_thread=False` 和实例锁保护连接、事务、查询与关闭，
支持同一实例在线程池中复用。重复主键/唯一键才转换为“会话已存在”；触发器及未来
其他约束的 `IntegrityError` 原样传播。事务失败仍由连接上下文统一回滚。

SQLite checkpointer 保留现有生命周期封装，并用注释说明关闭线程检查的前提是连接
只交给带内部锁的 saver 使用。

### 5. 知识来源与引用一致性

`load_text()`、`load_pdf()` 新增可选 `source_label`。默认 Citation 来源仅使用文件名，
不暴露服务器绝对路径；调用方可传公开 URL、课程名或受控相对标签。私有路径只用于
派生稳定 document ID，不进入工具返回。

`SearchHit` 在模型校验阶段确认 Citation 的 document、source、page、chunk_id 与
chunk 完全一致。`KnowledgeService.add_documents()` 拒绝同批重复
`(document_id, page)`，避免返回多个 chunk 但索引静默覆盖。无分页文档使用 page=0
构造 chunk ID，并明确说明真实页码从 1 开始。

### 6. 依赖来源

项目显式声明官方 PyPI 为 uv 默认索引，并重新生成锁文件，消除对开发机全局清华
镜像配置的隐式依赖。依赖版本本身不做无关升级。

### 7. 注释策略

新增中文注释仅覆盖以下不变量：

- LangGraph reducer 的追加/覆盖语义。
- checkpoint pending/resume 与同步锁的原因。
- thread ID 长度前缀、匿名标记和租户隔离。
- Tool Call 成组裁剪及硬边界。
- SQLite 线程与事务约束。
- page=0 哨兵、整文档替换和 Citation 一致性。

删除 `graph_builder.py` 中“注册节点”“返回缓存”等仅复述代码的注释。保留现有英文
docstring，避免无关翻译和大面积 diff。

## 测试与验收

所有行为修改遵循 RED → GREEN：

- pending checkpoint 拒绝新输入并可通过 `resume()` 恢复。
- 并发持久化调用不会从同一 checkpoint 分叉。
- 漏配权限、异常脱敏和未知工具名均有回归测试。
- 不完整及超大 Tool Call 组不会进入模型上下文。
- SessionStore 跨线程调用、约束错误分类和事务释放均有真实 SQLite 测试。
- 默认 source 不含父目录，Citation 不一致及重复文档页被拒绝。
- Prompt 逐角色映射、锁文件来源和现有 132 项测试继续通过。

完成实现后由多个独立 reviewer 再次审查，并运行 pytest、Ruff、mypy、
`uv lock --check`、`git diff --check` 与真实 DeepSeek ReAct 冒烟验证。只有全部通过
才创建加固提交并推送 `soldier`。

## 兼容性说明

以下变化是有意的 fail-closed 行为：

- Graph 使用业务工具时必须显式配置该工具权限。
- loader 默认 `source` 从本地路径变为文件名；需要其他公开来源时传 `source_label`。
- 同一批次重复文档页从静默覆盖改为 `ValueError`。
- 原始模型/工具异常不再出现在公开状态中。
