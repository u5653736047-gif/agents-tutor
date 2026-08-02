# 推送前安全加固 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复推送前复审发现的运行时、checkpoint、SQLite、知识引用和依赖来源问题，补齐关键“为什么”注释，并在多代理复审与真实 DeepSeek 验证通过后推送 `soldier`。

**Architecture:** 保持四个角色共享 `ReActAgentNode` 的现有架构，只在各层入口收紧边界：Graph 负责权限和 checkpoint 生命周期，ToolExecutor 负责安全 Observation，上下文裁剪器负责消息组完整性，SessionStore 负责单实例线程安全，知识模型负责引用一致性。所有行为修改采用 RED → GREEN，小步本地提交；用户已有的 `.gitignore` 变更始终不纳入暂存区。

**Tech Stack:** Python 3.11、LangGraph、LangChain Core、Pydantic 2、SQLite、pytest、Ruff、mypy、uv、DeepSeek OpenAI-compatible API。

---

## 文件映射

- `backend/src/core/graph_builder.py`：业务工具权限、持久化运行锁、pending 检测和 `resume()`。
- `backend/src/core/nodes/react_agent.py`：模型异常脱敏和安全工具名事件。
- `backend/src/core/tools/executor.py`：工具名规范化、参数/运行异常脱敏。
- `backend/src/core/context.py`：完整 Tool Call 组和硬消息边界。
- `backend/src/core/state.py`：reducer 及持久/瞬态字段语义注释。
- `backend/src/core/persistence.py`：SQLite checkpointer 的线程使用前提注释。
- `backend/src/core/sessions.py`：SessionStore 跨线程保护及 IntegrityError 分类。
- `backend/src/core/knowledge/{loaders,models,service,chunking}.py`：公开 source、Citation 一致性、重复页拒绝和 page=0 哨兵。
- `backend/pyproject.toml`、`backend/uv.lock`：显式使用官方 PyPI。
- 对应 `backend/tests/test_*.py`：所有边界的回归测试。
- `docs/TASK_BREAKDOWN_v2.md`：完成后更新当前加固冲刺状态。

### Task 1: 收紧 Graph、ReAct 与工具执行安全边界

**Files:**
- Modify: `backend/tests/test_graph_builder.py`
- Modify: `backend/tests/test_agent_factory.py`
- Modify: `backend/tests/test_react_agent.py`
- Modify: `backend/tests/test_tool_executor.py`
- Modify: `backend/src/core/graph_builder.py:48-67`
- Modify: `backend/src/core/nodes/react_agent.py:108-160`
- Modify: `backend/src/core/tools/executor.py:16-92`

- [ ] **Step 1: 写入 fail-closed 权限、角色 Prompt 映射和脱敏失败测试**

```python
# test_graph_builder.py
def test_graph_requires_explicit_permissions_for_every_business_tool() -> None:
    with pytest.raises(ValueError, match="缺少.*double"):
        CollaborativeAgentGraph(model=ScriptedModel([]), tools=[double])


# test_agent_factory.py；替换 set 对比
for role, agent in agents.items():
    assert agent.system_prompt == ROLE_PROMPTS[role]


# test_react_agent.py
def test_react_agent_does_not_expose_model_exception_text() -> None:
    agent = ReActAgentNode(
        role=AgentRole.EVALUATOR,
        system_prompt="你是评价助手。",
        model=FailingModel(),
    )

    result = agent.run(create_initial_state())

    assert result.error == RunError(
        error_code=ErrorCode.MODEL_CALL_FAILED,
        message="模型调用失败",
        agent="evaluator",
    )
    assert "模型不可用" not in result.error.model_dump_json()


def test_react_agent_replaces_unknown_tool_name_in_events() -> None:
    secret_name = "missing-/srv/private/key"
    model = ScriptedModel(
        [
            AIMessage(content="", tool_calls=[tool_call(secret_name)]),
            AIMessage(content="fallback"),
        ]
    )
    agent = ReActAgentNode(
        role=AgentRole.EVALUATOR,
        system_prompt="system",
        model=model,
    )

    result = agent.run(create_initial_state())

    serialized = str(result.updates)
    assert secret_name not in serialized
    assert result.updates["tool_results"][0].tool_name == UNKNOWN_TOOL_NAME
```

```python
# test_tool_executor.py
@tool
def leaking_tool() -> str:
    """模拟包含私有信息的工具异常。"""
    raise RuntimeError("secret=/srv/private/token")


def test_tool_executor_redacts_runtime_exception() -> None:
    execution = ToolExecutor([leaking_tool]).execute(
        tool_call("leaking_tool"), AgentRole.EVALUATOR
    )

    assert execution.result.error == "工具执行失败"
    assert execution.message.content == "错误：工具执行失败"
    assert "private/token" not in execution.result.model_dump_json()


def test_tool_executor_redacts_unknown_requested_name() -> None:
    secret_name = "missing-/srv/private/key"
    execution = ToolExecutor().execute(tool_call(secret_name), AgentRole.EVALUATOR)

    assert execution.result.tool_name == UNKNOWN_TOOL_NAME
    assert execution.message.name == UNKNOWN_TOOL_NAME
    assert secret_name not in execution.result.model_dump_json()
```

- [ ] **Step 2: 运行定向测试并确认 RED**

Run:

```powershell
$env:PYTHONPATH='D:\CODE\Agents\backend\src'
& 'D:\CODE\Agents\backend\.venv\Scripts\python.exe' -m pytest `
  'D:\CODE\Agents\backend\tests\test_graph_builder.py' `
  'D:\CODE\Agents\backend\tests\test_agent_factory.py' `
  'D:\CODE\Agents\backend\tests\test_react_agent.py' `
  'D:\CODE\Agents\backend\tests\test_tool_executor.py' -q
```

Expected: FAIL；缺失权限仍默认放行，模型/工具原始异常与未知工具名仍可见。

- [ ] **Step 3: 实现最小 fail-closed 与脱敏逻辑**

```python
# graph_builder.py；替换现有权限集合检查
permissions = tool_permissions or {}
business_tool_names = {business_tool.name for business_tool in tools}
permission_names = set(permissions)
missing_permissions = business_tool_names - permission_names
unknown_permissions = permission_names - business_tool_names
if missing_permissions:
    names = ", ".join(sorted(missing_permissions))
    raise ValueError(f"tool_permissions 缺少业务工具：{names}")
if unknown_permissions:
    names = ", ".join(sorted(unknown_permissions))
    raise ValueError(f"tool_permissions 包含非业务工具：{names}")
```

```python
# executor.py
UNKNOWN_TOOL_NAME = "unknown_tool"

_SAFE_ERRORS = {
    ErrorCode.TOOL_UNKNOWN: "未注册工具",
    ErrorCode.TOOL_UNAUTHORIZED: "当前角色无权调用该工具",
    ErrorCode.TOOL_INVALID_ARGUMENTS: "工具参数无效",
    ErrorCode.TOOL_EXECUTION_FAILED: "工具执行失败",
}


def public_tool_name(self, tool_call: Mapping[str, Any]) -> str:
    """只公开注册表中的规范名称，避免模型生成名称进入持久状态。"""
    requested_name = str(tool_call.get("name") or "")
    tool = self.registry.get(requested_name)
    return tool.name if tool is not None else UNKNOWN_TOOL_NAME


def execute(
    self,
    tool_call: Mapping[str, Any],
    agent_role: AgentRole,
) -> ToolExecution:
    call_id = str(tool_call.get("id") or "unknown")
    requested_name = str(tool_call.get("name") or "")
    tool_name = self.public_tool_name(tool_call)
    args = tool_call.get("args", {})
    started_at = perf_counter()
    tool = self.registry.get(requested_name)
    success = False
    output = ""
    error_code: ErrorCode | None = None

    if tool is None:
        error_code = ErrorCode.TOOL_UNKNOWN
    elif not self.registry.is_authorized(requested_name, agent_role):
        error_code = ErrorCode.TOOL_UNAUTHORIZED
    elif not isinstance(args, Mapping):
        error_code = ErrorCode.TOOL_INVALID_ARGUMENTS
    else:
        try:
            input_schema = cast(type[BaseModel], tool.get_input_schema())
            input_schema.model_validate(dict(args))
        except ValidationError:
            error_code = ErrorCode.TOOL_INVALID_ARGUMENTS
        except Exception:  # noqa: BLE001 - Schema 边界只公开稳定错误分类
            error_code = ErrorCode.TOOL_EXECUTION_FAILED
        else:
            try:
                output = _to_text(tool.invoke(dict(args)))
                success = True
            except Exception:  # noqa: BLE001 - 工具边界只公开稳定错误分类
                error_code = ErrorCode.TOOL_EXECUTION_FAILED

    error = None if error_code is None else _SAFE_ERRORS[error_code]
    duration_ms = (perf_counter() - started_at) * 1000
    content = output if success else f"错误：{error}"
    result = ToolResult(
        tool_call_id=call_id,
        tool_name=tool_name,
        agent_role=agent_role,
        success=success,
        output=output,
        error=error,
        error_code=error_code,
        duration_ms=duration_ms,
    )
    return ToolExecution(
        message=ToolMessage(content=content, tool_call_id=call_id, name=tool_name),
        result=result,
    )
```

`requested_name` 仅用于注册表查找；`ValidationError`、Schema 异常和工具运行异常只决定
`ErrorCode`，不再调用 `str(exc)`。

```python
# react_agent.py；模型边界和工具事件
except Exception:  # noqa: BLE001 - 模型边界只暴露稳定错误分类
    error = RunError(
        error_code=ErrorCode.MODEL_CALL_FAILED,
        message="模型调用失败",
        agent=self.role.value,
    )

for tool_call in response.tool_calls:
    public_name = self.tool_executor.public_tool_name(tool_call)
    emit(EventType.TOOL_STARTED, tool_name=public_name)
    execution = self.tool_executor.execute(tool_call, self.role)
```

- [ ] **Step 4: 运行定向测试并确认 GREEN**

Run: Step 2 的 pytest 命令。

Expected: PASS；Prompt 逐角色对应，所有公开失败字段均为稳定文本。

- [ ] **Step 5: 本地提交运行时安全边界**

```powershell
git add backend/src/core/graph_builder.py backend/src/core/nodes/react_agent.py `
  backend/src/core/tools/executor.py backend/tests/test_graph_builder.py `
  backend/tests/test_agent_factory.py backend/tests/test_react_agent.py `
  backend/tests/test_tool_executor.py
git commit -m "fix: 收紧 ReAct 运行时安全边界" `
  -m "业务工具改为显式授权，并阻止模型或工具异常及未知工具名进入公开状态。"
```

### Task 2: 为 Tool Call 上下文组建立硬边界

**Files:**
- Modify: `backend/tests/test_context.py`
- Modify: `backend/src/core/context.py:19-83`

- [ ] **Step 1: 写入不完整组、超大组和硬上限测试**

```python
def test_trim_drops_incomplete_tool_call_group() -> None:
    request = AIMessage(
        content="",
        tool_calls=[_tool_call("call-1"), _tool_call("call-2")],
    )
    first_result = ToolMessage(content="one", tool_call_id="call-1")
    answer = AIMessage(content="fallback")
    history = [HumanMessage(content="latest"), request, first_result, answer]

    window = trim_message_history(history, max_messages=3)

    assert window.messages == (history[0], answer)
    assert len(window.messages) <= 4


def test_trim_drops_complete_tool_group_that_exceeds_hard_boundary() -> None:
    request = AIMessage(
        content="",
        tool_calls=[_tool_call(f"call-{index}") for index in range(4)],
    )
    results = [
        ToolMessage(content=str(index), tool_call_id=f"call-{index}")
        for index in range(4)
    ]
    latest = HumanMessage(content="latest")
    answer = AIMessage(content="fallback")
    history = [request, *results, latest, answer]

    window = trim_message_history(history, max_messages=3)

    assert window.messages == (latest, answer)
    assert len(window.messages) <= 4
```

把现有 `test_trim_keeps_complete_multi_tool_call_group` 的 `max_messages` 改为 `4`，
使“最新用户 + 三条完整工具组 + 最终回答”恰好处于 `max_messages + 1` 的硬边界内。

- [ ] **Step 2: 运行上下文测试并确认 RED**

Run:

```powershell
$env:PYTHONPATH='D:\CODE\Agents\backend\src'
& 'D:\CODE\Agents\backend\.venv\Scripts\python.exe' -m pytest `
  'D:\CODE\Agents\backend\tests\test_context.py' -q
```

Expected: FAIL；当前实现会扩张工具组，超过硬边界，并保留缺少结果的调用组。

- [ ] **Step 3: 用完整组集合替换单向父子扩张**

```python
def trim_message_history(
    messages: Sequence[BaseMessage],
    max_messages: int,
) -> ContextWindow:
    """保留最近历史，同时只注入完整且未越界的工具调用组。"""
    if max_messages < 3:
        raise ValueError("max_messages must be at least 3")

    history = list(messages)
    if not history:
        return ContextWindow(messages=(), trimmed_count=0)

    selected = set(range(max(0, len(history) - max_messages), len(history)))
    hard_limit = max_messages
    latest_human = next(
        (
            index
            for index in range(len(history) - 1, -1, -1)
            if isinstance(history[index], HumanMessage)
        ),
        None,
    )
    if latest_human is not None and latest_human not in selected:
        selected.add(latest_human)
        hard_limit += 1

    groups, incomplete_parents, orphan_results = _tool_groups(history)
    selected.difference_update(orphan_results)
    # Tool Call 必须整组进入上下文；不完整或扩张后越界时整组删除。
    for parent, group in groups.items():
        if selected.isdisjoint(group):
            continue
        expanded = selected | group
        if parent in incomplete_parents or len(expanded) > hard_limit:
            selected.difference_update(group)
        else:
            selected = expanded

    kept = tuple(history[index] for index in sorted(selected))
    return ContextWindow(messages=kept, trimmed_count=len(history) - len(kept))


def _tool_groups(
    messages: Sequence[BaseMessage],
) -> tuple[dict[int, frozenset[int]], set[int], set[int]]:
    """返回工具组、结果不完整的父消息和孤立结果。"""
    call_parents: dict[str, int] = {}
    expected_by_parent: dict[int, set[str]] = {}
    observed_by_parent: dict[int, set[str]] = {}
    children_by_parent: dict[int, set[int]] = {}
    orphan_results: set[int] = set()

    for index, message in enumerate(messages):
        if isinstance(message, AIMessage) and message.tool_calls:
            expected = {
                str(tool_call["id"])
                for tool_call in message.tool_calls
                if tool_call.get("id")
            }
            expected_by_parent[index] = expected
            for call_id in expected:
                call_parents[call_id] = index
            continue
        if not isinstance(message, ToolMessage):
            continue
        call_id = str(message.tool_call_id)
        parent = call_parents.get(call_id)
        if parent is None:
            orphan_results.add(index)
            continue
        observed_by_parent.setdefault(parent, set()).add(call_id)
        children_by_parent.setdefault(parent, set()).add(index)

    groups = {
        parent: frozenset({parent, *children_by_parent.get(parent, set())})
        for parent in expected_by_parent
    }
    incomplete_parents = {
        parent
        for parent, expected in expected_by_parent.items()
        if not expected.issubset(observed_by_parent.get(parent, set()))
    }
    return groups, incomplete_parents, orphan_results
```

实际结果 ID 集合必须覆盖期望 ID 集合才算完整；无法关联的 ToolMessage 始终删除。

- [ ] **Step 4: 运行上下文与 ReAct 测试并确认 GREEN**

Run:

```powershell
$env:PYTHONPATH='D:\CODE\Agents\backend\src'
& 'D:\CODE\Agents\backend\.venv\Scripts\python.exe' -m pytest `
  'D:\CODE\Agents\backend\tests\test_context.py' `
  'D:\CODE\Agents\backend\tests\test_react_agent.py' -q
```

Expected: PASS；窗口长度不超过 `max_messages + 1`，且无孤立工具消息。

- [ ] **Step 5: 本地提交上下文边界**

```powershell
git add backend/src/core/context.py backend/tests/test_context.py
git commit -m "fix: 限制 ReAct 工具上下文边界"
```

### Task 3: 增加 checkpoint pending 检测、恢复入口和单实例同步

**Files:**
- Modify: `backend/tests/test_graph_persistence.py`
- Modify: `backend/src/core/graph_builder.py:32-341`
- Modify: `backend/src/core/state.py:99-151`

- [ ] **Step 1: 写入 pending、resume、持久字段和并发回归测试**

```python
def test_run_rejects_pending_checkpoint_and_resume_continues_it() -> None:
    model = ScriptedModel([AIMessage(content="resumed answer")])
    graph = CollaborativeAgentGraph(model=model, checkpointer=InMemorySaver())
    app = graph.build()
    config = graph._thread_config("pending", "user-1")
    state = create_initial_state(session_id="pending", user_id="user-1")
    app.update_state(config, state, as_node="__start__")

    with pytest.raises(RuntimeError, match="resume"):
        graph.run("must not be appended", "pending", "user-1")

    result = graph.resume("pending", "user-1")
    assert result["messages"][-1].content == "resumed answer"
    assert _human_contents(result["messages"]) == []


@pytest.mark.parametrize("prepare", ["missing", "completed"])
def test_resume_requires_pending_work(prepare: str) -> None:
    responses = [AIMessage(content="done")] if prepare == "completed" else []
    graph = CollaborativeAgentGraph(
        model=ScriptedModel(responses), checkpointer=InMemorySaver()
    )
    if prepare == "completed":
        graph.run("question", "session-1", "user-1")

    with pytest.raises(ValueError, match="待恢复"):
        graph.resume("session-1", "user-1")


def test_task_context_and_extra_persist_across_turns() -> None:
    graph = CollaborativeAgentGraph(
        model=ScriptedModel([AIMessage(content="first"), AIMessage(content="second")]),
        checkpointer=InMemorySaver(),
    )
    graph.run("first", "session-1", "user-1")
    graph.build().update_state(
        graph._thread_config("session-1", "user-1"),
        {"task_context": TaskContext(intent="teach"), "extra": {"course": "ml"}},
    )

    result = graph.run("second", "session-1", "user-1")

    assert result["task_context"].intent == "teach"
    assert result["extra"]["course"] == "ml"


class BlockingModel:
    """阻塞首次调用，用于检测同一 Graph 实例是否重叠执行。"""

    def __init__(self) -> None:
        self.first_entered = Event()
        self.second_entered = Event()
        self.release = Event()
        self._counter_lock = Lock()
        self._calls = 0

    def bind_tools(self, tools: Sequence[object]) -> BlockingModel:
        return self

    def invoke(self, messages: list[BaseMessage]) -> AIMessage:
        with self._counter_lock:
            self._calls += 1
            call_number = self._calls
        if call_number == 1:
            self.first_entered.set()
        else:
            self.second_entered.set()
        if not self.release.wait(timeout=2):
            raise RuntimeError("test release timed out")
        return AIMessage(content=f"answer-{call_number}")


def test_persisted_runs_are_serialized_within_one_graph_instance() -> None:
    model = BlockingModel()
    graph = CollaborativeAgentGraph(model=model, checkpointer=InMemorySaver())

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(graph.run, "first", "session-1", "user-1")
        assert model.first_entered.wait(timeout=1)
        second = pool.submit(graph.run, "second", "session-1", "user-1")
        overlapped = model.second_entered.wait(timeout=0.1)
        model.release.set()
        first.result(timeout=2)
        second.result(timeout=2)

    assert overlapped is False
```

测试文件同时导入 `ThreadPoolExecutor`、`Event`、`Lock` 和 `TaskContext`。该测试在无
Graph 锁时确定性观察到重叠，在加锁后不依赖模型响应顺序。

- [ ] **Step 2: 运行持久化测试并确认 RED**

Run:

```powershell
$env:PYTHONPATH='D:\CODE\Agents\backend\src'
& 'D:\CODE\Agents\backend\.venv\Scripts\python.exe' -m pytest `
  'D:\CODE\Agents\backend\tests\test_graph_persistence.py' -q
```

Expected: FAIL；`run()` 会覆盖 pending 状态，且尚无 `resume()` 与同步区间。

- [ ] **Step 3: 实现单实例锁、pending 检测和恢复**

```python
# graph_builder.py imports / __init__
from threading import RLock

self._run_lock = RLock()
```

```python
def run(
    self,
    user_input: str,
    session_id: str = "demo",
    user_id: str | None = None,
) -> AgentState:
    """从一条用户消息启动协作图。"""
    self._user_key(user_id)
    self._session_key(session_id)
    app = self.build()
    if self.checkpointer is None:
        state = create_initial_state(session_id=session_id, user_id=user_id)
        state["messages"] = [HumanMessage(content=user_input)]
        return cast(AgentState, app.invoke(state))

    config = self._thread_config(session_id, user_id)
    # 同一实例串行化 snapshot→invoke，避免两个请求从同一 checkpoint 分叉。
    with self._run_lock:
        snapshot = app.get_state(config)
        if snapshot.next:
            raise RuntimeError("会话存在待执行节点，请调用 resume()")
        if snapshot.values:
            state = cast(
                AgentState,
                {
                    "messages": [HumanMessage(content=user_input)],
                    "next_agent": None,
                    "run_error": None,
                    "handoff_count": 0,
                    "agent_switch_count": 0,
                },
            )
        else:
            state = create_initial_state(session_id=session_id, user_id=user_id)
            state["messages"] = [HumanMessage(content=user_input)]
        return cast(AgentState, app.invoke(state, config=config))


def resume(
    self,
    session_id: str,
    user_id: str | None = None,
) -> AgentState:
    """从持久化 checkpoint 的待执行节点继续。"""
    if self.checkpointer is None:
        raise ValueError("resume requires a configured checkpointer")
    config = self._thread_config(session_id, user_id)
    app = self.build()
    with self._run_lock:
        snapshot = app.get_state(config)
        if not snapshot.values or not snapshot.next:
            raise ValueError("会话没有待恢复的执行")
        return cast(AgentState, app.invoke(None, config=config))
```

在 `_thread_config()` 前补一条中文注释，说明长度前缀避免分隔符碰撞，`none` 明确表示
匿名租户；删除 `build()` 中“注册节点”“返回缓存”等仅复述代码的注释。

在 `AgentState` docstring 中把追加字段明确写为 `messages`、`tool_results`、`events`；
在 `task_context`、`extra` 注释中说明二者跨轮持久，瞬态字段只有 `next_agent`、
`run_error`、`handoff_count` 和 `agent_switch_count`。

- [ ] **Step 4: 运行 Graph、状态与持久化测试并确认 GREEN**

Run:

```powershell
$env:PYTHONPATH='D:\CODE\Agents\backend\src'
& 'D:\CODE\Agents\backend\.venv\Scripts\python.exe' -m pytest `
  'D:\CODE\Agents\backend\tests\test_graph_builder.py' `
  'D:\CODE\Agents\backend\tests\test_graph_persistence.py' `
  'D:\CODE\Agents\backend\tests\test_state_integration.py' -q
```

Expected: PASS；pending 状态只可恢复，不接收新输入；同一 Graph 实例不出现并发分叉。

- [ ] **Step 5: 本地提交 checkpoint 生命周期**

```powershell
git add backend/src/core/graph_builder.py backend/src/core/state.py `
  backend/tests/test_graph_persistence.py
git commit -m "fix: 完善 checkpoint 恢复与并发保护" `
  -m "拒绝覆盖待执行 checkpoint，并用单实例锁保护读取到写回的完整区间。"
```

### Task 4: 使 SessionStore 支持同实例跨线程使用

**Files:**
- Modify: `backend/tests/test_sessions.py`
- Modify: `backend/src/core/sessions.py:23-139`
- Modify: `backend/src/core/persistence.py:14-23`

- [ ] **Step 1: 写入真实 SQLite 跨线程与错误分类测试**

```python
def test_session_store_supports_one_instance_across_threads(tmp_path: Path) -> None:
    with SessionStore(tmp_path / "sessions.sqlite") as store:
        with ThreadPoolExecutor(max_workers=4) as pool:
            records = list(
                pool.map(
                    lambda number: store.create_session(
                        f"session-{number}", user_id="user-1"
                    ),
                    range(8),
                )
            )

        assert store.list_sessions("user-1") == sorted(
            records, key=lambda record: (record.created_at, record.session_id)
        )


def test_create_propagates_non_unique_integrity_error(tmp_path: Path) -> None:
    database_path = tmp_path / "sessions.sqlite"
    with SessionStore(database_path) as store:
        with sqlite3.connect(database_path) as setup:
            setup.execute(
                """
                CREATE TRIGGER reject_insert
                BEFORE INSERT ON sessions
                BEGIN
                    SELECT RAISE(ABORT, 'forced create failure');
                END
                """
            )

        with pytest.raises(sqlite3.IntegrityError, match="forced create failure"):
            store.create_session("blocked", "user-1")
```

- [ ] **Step 2: 运行 SessionStore 测试并确认 RED**

Run:

```powershell
$env:PYTHONPATH='D:\CODE\Agents\backend\src'
& 'D:\CODE\Agents\backend\.venv\Scripts\python.exe' -m pytest `
  'D:\CODE\Agents\backend\tests\test_sessions.py' -q
```

Expected: FAIL；默认 SQLite 连接拒绝跨线程，触发器异常被错误转换为重复会话。

- [ ] **Step 3: 用一个 RLock 包住 SessionStore 连接操作**

```python
from threading import RLock

self._lock = RLock()
# 关闭 SQLite 线程检查的前提是所有连接访问都由实例锁串行化。
connection = sqlite3.connect(database_path, check_same_thread=False)
```

写操作使用以下嵌套顺序；`list_sessions()` 只把 execute/fetchall 放在实例锁内，
`close()` 直接在实例锁内关闭连接：

```python
with self._lock:
    try:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO sessions
                    (user_key, user_id, session_id, created_at, archived)
                VALUES (?, ?, ?, ?, 0)
                """,
                (
                    self._user_key(user_id),
                    user_id,
                    session_id,
                    record.created_at.isoformat(),
                ),
            )
    except sqlite3.IntegrityError as exc:
        duplicate_codes = {
            sqlite3.SQLITE_CONSTRAINT_PRIMARYKEY,
            sqlite3.SQLITE_CONSTRAINT_UNIQUE,
        }
        if exc.sqlite_errorcode not in duplicate_codes:
            raise
        raise ValueError(
            f"session already exists for user: {session_id}"
        ) from exc

with self._lock:
    rows = self._connection.execute(query, parameters).fetchall()

with self._lock:
    with self._connection:
        cursor = self._connection.execute(
            """
            UPDATE sessions
            SET archived = 1
            WHERE user_key = ? AND session_id = ? AND archived = 0
            """,
            (self._user_key(user_id), session_id),
        )

with self._lock:
    self._connection.close()
```

在连接创建处增加一条中文“为什么”注释：关闭线程检查只允许在所有连接访问均由实例锁
串行化时使用。在 `open_sqlite_checkpointer()` 增加对应注释，说明 SqliteSaver 自身负责
连接访问同步，生命周期仍由 context manager 关闭。

- [ ] **Step 4: 运行 SessionStore 与 SQLite checkpointer 测试并确认 GREEN**

Run:

```powershell
$env:PYTHONPATH='D:\CODE\Agents\backend\src'
& 'D:\CODE\Agents\backend\.venv\Scripts\python.exe' -m pytest `
  'D:\CODE\Agents\backend\tests\test_sessions.py' `
  'D:\CODE\Agents\backend\tests\test_graph_persistence.py' -q
```

Expected: PASS；同实例跨线程可用，只有主键/唯一键冲突被映射为业务错误。

- [ ] **Step 5: 本地提交 SQLite 线程安全修改**

```powershell
git add backend/src/core/sessions.py backend/src/core/persistence.py `
  backend/tests/test_sessions.py
git commit -m "fix: 保护 SQLite 会话并发访问"
```

### Task 5: 保护知识来源并校验 Citation 坐标

**Files:**
- Modify: `backend/tests/test_knowledge_loaders.py`
- Modify: `backend/tests/test_knowledge_models.py`
- Modify: `backend/tests/test_knowledge_service.py`
- Modify: `backend/src/core/knowledge/loaders.py:12-74`
- Modify: `backend/src/core/knowledge/models.py:5-48`
- Modify: `backend/src/core/knowledge/service.py:26-44`
- Modify: `backend/src/core/knowledge/chunking.py:25-49`

- [ ] **Step 1: 写入 source、Citation 与重复页回归测试**

```python
def test_loader_exposes_filename_by_default_and_accepts_source_label(
    tmp_path: Path,
) -> None:
    source = tmp_path / "private" / "lesson.txt"
    source.parent.mkdir()
    source.write_text("content", encoding="utf-8")

    default_document = load_text(source)[0]
    labeled_document = load_text(source, source_label="course://lesson")[0]

    assert default_document.source == "lesson.txt"
    assert str(source.parent) not in default_document.source
    assert labeled_document.source == "course://lesson"
    assert default_document.document_id == labeled_document.document_id


def test_search_hit_rejects_citation_that_does_not_match_chunk() -> None:
    chunk = KnowledgeChunk(
        chunk_id="guide:0:0:5",
        document_id="guide",
        content="Force",
        source="guide.txt",
        page=None,
        start=0,
        end=5,
    )
    citation = Citation(
        document_id="other",
        source=chunk.source,
        page=chunk.page,
        chunk_id=chunk.chunk_id,
    )

    with pytest.raises(ValidationError, match="citation"):
        SearchHit(chunk=chunk, citation=citation, score=1.0)


def test_service_rejects_duplicate_document_page_in_one_batch() -> None:
    service = KnowledgeService(InMemoryKnowledgeIndex())
    duplicate_page = [
        KnowledgeDocument(
            document_id="guide", content="first", source="guide.pdf", page=1
        ),
        KnowledgeDocument(
            document_id="guide", content="second", source="guide.pdf", page=1
        ),
    ]

    with pytest.raises(ValueError, match="duplicate document page"):
        service.add_documents(duplicate_page)
```

同时把现有 loader 断言从 `str(source)` 改为文件名，并给 PDF 添加
`source_label="course://physics"` 的断言。

- [ ] **Step 2: 运行知识层测试并确认 RED**

Run:

```powershell
$env:PYTHONPATH='D:\CODE\Agents\backend\src'
& 'D:\CODE\Agents\backend\.venv\Scripts\python.exe' -m pytest `
  'D:\CODE\Agents\backend\tests\test_knowledge_loaders.py' `
  'D:\CODE\Agents\backend\tests\test_knowledge_models.py' `
  'D:\CODE\Agents\backend\tests\test_knowledge_service.py' -q
```

Expected: FAIL；当前 source 暴露路径，SearchHit 接受不一致 Citation，重复页会静默碰撞。

- [ ] **Step 3: 实现公开 source、模型级坐标校验和批次唯一性**

```python
# loaders.py；load_text/load_pdf 都增加同名参数
def load_text(
    path: str | Path,
    *,
    document_id: str | None = None,
    source_label: str | None = None,
) -> list[KnowledgeDocument]:
    source = Path(path)
    public_source = _public_source(source, source_label)


def load_pdf(
    path: str | Path,
    *,
    document_id: str | None = None,
    source_label: str | None = None,
) -> list[KnowledgeDocument]:
    from pypdf import PdfReader

    source = Path(path)
    public_source = _public_source(source, source_label)


def _public_source(source: Path, source_label: str | None) -> str:
    """默认只公开文件名；本地路径仅参与稳定 document ID 计算。"""
    return source.name if source_label is None else source_label
```

两个 loader 构造 `KnowledgeDocument` 时都把 `source` 字段设为 `public_source`；
`_default_document_id(source)` 保持不变。删除“文本加载不需要 PDF 依赖”的不准确注释，
保留局部导入本身。

```python
# models.py
from typing import Any, Self
from pydantic import BaseModel, Field, model_validator

class SearchHit(BaseModel):
    chunk: KnowledgeChunk
    citation: Citation
    score: float = Field(ge=0)

    @model_validator(mode="after")
    def citation_matches_chunk(self) -> Self:
        expected = (
            self.chunk.document_id,
            self.chunk.source,
            self.chunk.page,
            self.chunk.chunk_id,
        )
        actual = (
            self.citation.document_id,
            self.citation.source,
            self.citation.page,
            self.citation.chunk_id,
        )
        if actual != expected:
            raise ValueError("citation must match chunk coordinates")
        return self
```

```python
# service.py；在分块和删除旧文档之前执行
coordinates: set[tuple[str, int | None]] = set()
for document in document_batch:
    coordinate = (document.document_id, document.page)
    if coordinate in coordinates:
        raise ValueError(
            f"duplicate document page: {document.document_id} page={document.page}"
        )
    coordinates.add(coordinate)
```

在 `chunking.py` 的 `page = ... else 0` 前补注释：`0` 只用于无分页文档的稳定 chunk ID，
真实 PDF 页码从 1 开始。在 service 整文档删除处保留并精炼“同一 document_id 整批替换”
的原因注释。

- [ ] **Step 4: 运行全部知识层测试并确认 GREEN**

Run:

```powershell
$env:PYTHONPATH='D:\CODE\Agents\backend\src'
& 'D:\CODE\Agents\backend\.venv\Scripts\python.exe' -m pytest `
  'D:\CODE\Agents\backend\tests\test_knowledge_chunking.py' `
  'D:\CODE\Agents\backend\tests\test_knowledge_index.py' `
  'D:\CODE\Agents\backend\tests\test_knowledge_loaders.py' `
  'D:\CODE\Agents\backend\tests\test_knowledge_models.py' `
  'D:\CODE\Agents\backend\tests\test_knowledge_service.py' `
  'D:\CODE\Agents\backend\tests\test_knowledge_tools.py' -q
```

Expected: PASS；对外引用不含本地父目录，命中坐标始终一致。

- [ ] **Step 5: 本地提交知识边界修改**

```powershell
git add backend/src/core/knowledge backend/tests/test_knowledge_chunking.py `
  backend/tests/test_knowledge_index.py backend/tests/test_knowledge_loaders.py `
  backend/tests/test_knowledge_models.py backend/tests/test_knowledge_service.py `
  backend/tests/test_knowledge_tools.py
git commit -m "fix: 校验知识来源与引用一致性"
```

### Task 6: 固定 uv 官方 PyPI 来源并重建锁文件

**Files:**
- Create: `backend/tests/test_project_config.py`
- Modify: `backend/pyproject.toml`
- Modify: `backend/uv.lock`

- [ ] **Step 1: 写入项目配置与锁文件来源测试**

```python
"""Project dependency source tests."""

from pathlib import Path
import tomllib

BACKEND_ROOT = Path(__file__).parents[1]


def test_uv_declares_official_pypi_as_default_index() -> None:
    with (BACKEND_ROOT / "pyproject.toml").open("rb") as stream:
        config = tomllib.load(stream)

    assert config["tool"]["uv"]["index"] == [
        {
            "name": "pypi",
            "url": "https://pypi.org/simple",
            "default": True,
        }
    ]


def test_lock_file_does_not_reference_local_mirror() -> None:
    lock_text = (BACKEND_ROOT / "uv.lock").read_text(encoding="utf-8")

    assert "pypi.tuna.tsinghua.edu.cn" not in lock_text
    assert 'registry = "https://pypi.org/simple"' in lock_text
```

- [ ] **Step 2: 运行配置测试并确认 RED**

Run:

```powershell
$env:PYTHONPATH='D:\CODE\Agents\backend\src'
& 'D:\CODE\Agents\backend\.venv\Scripts\python.exe' -m pytest `
  'D:\CODE\Agents\backend\tests\test_project_config.py' -q
```

Expected: FAIL；项目未声明默认索引，锁文件仍引用清华镜像。

- [ ] **Step 3: 声明官方索引并在不升级依赖的前提下重锁**

在 `backend/pyproject.toml` 增加当前 uv 官方支持的配置：

```toml
[[tool.uv.index]]
name = "pypi"
url = "https://pypi.org/simple"
default = true
```

Run:

```powershell
uv lock --directory 'D:\CODE\Agents\backend'
```

Expected: `uv.lock` 的包版本保持不变，仅 registry/download URL 切换为官方 PyPI；若版本
发生变化，停止并恢复锁文件后使用锁定版本约束重新生成，不接受无关升级。

- [ ] **Step 4: 验证配置测试和锁文件一致性**

Run:

```powershell
$env:PYTHONPATH='D:\CODE\Agents\backend\src'
& 'D:\CODE\Agents\backend\.venv\Scripts\python.exe' -m pytest `
  'D:\CODE\Agents\backend\tests\test_project_config.py' -q
uv lock --check --directory 'D:\CODE\Agents\backend'
git diff --unified=0 -- backend/uv.lock | Select-String -Pattern '^[+-]version = '
```

Expected: 两项测试 PASS，`uv lock --check` 成功，最后一条命令不显示版本增删行。

- [ ] **Step 5: 本地提交依赖来源修改**

```powershell
git add backend/pyproject.toml backend/uv.lock backend/tests/test_project_config.py
git commit -m "build: 固定 uv 官方依赖源" `
  -m "避免开发机全局镜像配置进入可复现锁文件。"
```

### Task 7: 更新清单、执行多代理复审、全量验收并推送

**Files:**
- Modify: `docs/TASK_BREAKDOWN_v2.md`
- Review only: Tasks 1-6 的全部源码、测试与配置 diff

- [ ] **Step 1: 更新任务清单的加固状态**

在 `docs/TASK_BREAKDOWN_v2.md` 的“当前冲刺”中把设计确认、实施计划、边界修复、关键
注释标为完成；在 1.3.1 中把 pending `resume()` 和单实例并发保护标为完成；不要把
PostgreSQL、token 预算或跨进程锁误标为完成。

Run:

```powershell
git diff --check -- docs/TASK_BREAKDOWN_v2.md
git add docs/TASK_BREAKDOWN_v2.md
git commit -m "docs: 更新推送前加固进度"
```

Expected: 文档无空白错误，`.gitignore` 不在暂存区。

- [ ] **Step 2: 派发三路独立只读复审**

使用三个 reviewer，分别检查：

1. ReAct/Graph/Tool 安全边界、错误脱敏和权限 fail-closed。
2. checkpoint/context/SessionStore 的并发、恢复和事务语义。
3. knowledge/Citation/uv 来源、注释质量和测试覆盖。

每个 reviewer 必须基于 `git diff origin/soldier...HEAD` 给出按严重级别排序的发现和精确
文件位置；不得直接修改文件。主代理逐条验证反馈，真实问题回到对应 Task 的 RED → GREEN
循环，误报则记录验证依据。

- [ ] **Step 3: 运行全量自动化验证**

Run:

```powershell
$env:PYTHONPATH='D:\CODE\Agents\backend\src'
& 'D:\CODE\Agents\backend\.venv\Scripts\python.exe' -m pytest `
  'D:\CODE\Agents\backend\tests' -q
& 'D:\CODE\Agents\backend\.venv\Scripts\ruff.exe' check `
  'D:\CODE\Agents\backend\src' 'D:\CODE\Agents\backend\tests'
& 'D:\CODE\Agents\backend\.venv\Scripts\python.exe' -m mypy `
  'D:\CODE\Agents\backend\src\core'
uv lock --check --directory 'D:\CODE\Agents\backend'
git diff --check
git status --short
```

Expected: pytest 全绿；Ruff 无问题；mypy 无错误；锁文件有效；diff 无空白错误；status 仅可
保留用户原有的 ` M .gitignore`，没有未提交的本轮文件。

- [ ] **Step 4: 运行真实 DeepSeek ReAct 冒烟测试**

Run:

```powershell
$env:PYTHONPATH='D:\CODE\Agents\backend\src'
& 'D:\CODE\Agents\backend\.venv\Scripts\python.exe' `
  'D:\CODE\Agents\backend\scripts\verify_deepseek_react.py'
```

Expected: 从项目根 `.env` 读取 DeepSeek 配置，实际调用 `double` 工具，完成至少两次
ReAct 迭代并输出 42；输出中不出现 API Key。

- [ ] **Step 5: 确认提交范围并推送 soldier**

Run:

```powershell
git diff --cached --name-only
git log --oneline origin/soldier..HEAD
git push origin soldier
git ls-remote origin refs/heads/soldier
git rev-parse HEAD
```

Expected: 暂存区为空，提交列表只含项目重构/加固/文档提交；push 成功；远端 soldier 哈希
与本地 HEAD 完全一致。绝不暂存或提交 `.gitignore`。

## 完成标准

- 业务工具权限缺失、未知或多余时 Graph 构造失败。
- 模型/工具原始异常和未知工具名不进入消息、事件、ToolResult 或 checkpoint。
- pending checkpoint 不能被新输入覆盖，且 `resume()` 可继续执行。
- 同 Graph/SessionStore 实例的线程并发具备明确串行边界。
- 模型上下文不含孤立、不完整或越过硬边界的 Tool Call 组。
- 默认 Citation source 不暴露本地目录，SearchHit 坐标一致，同批重复页被拒绝。
- uv 锁文件仅引用官方 PyPI，依赖版本无无关升级。
- 关键路径只有解释不变量的简短中文注释，无逐行复述注释。
- 三路复审无未解决的重要问题，自动化验证和真实 DeepSeek 冒烟均通过。
- 远端 `soldier` 与本地 HEAD 一致，用户的 `.gitignore` 变更保持未提交。
