"""基于统一 ReAct Agent 的 LangGraph 编排。"""

from __future__ import annotations

from collections.abc import Collection, Hashable, Mapping, Sequence
from threading import RLock
from typing import Literal, cast

from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.runnables import Runnable, RunnableConfig, RunnableLambda
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, StateSnapshot, interrupt

from .context import MessageTokenCounter
from .events import ErrorCode, EventType, RunError, RunEvent
from .nodes import ReActAgentNode, create_agent_nodes
from .nodes.react_agent import ChatModel
from .state import (
    AgentRole,
    AgentState,
    HandoffApprovalAction,
    HandoffApprovalDecision,
    HandoffApprovalRequest,
    PendingHandoffApproval,
    TaskContext,
    ToolResult,
    create_initial_state,
)
from .tools import DEFAULT_TOOL_TIMEOUT_SECONDS, ToolRegistry

WorkerRole = Literal["teaching_assistant", "learning_assistant", "evaluator"]
CompiledAgentGraph = CompiledStateGraph[AgentState, None, AgentState, AgentState]
_HANDOFF_APPROVAL_NODE = "handoff_approval"


@tool
def handoff(target: WorkerRole) -> str:
    """将当前任务交给指定的专业 Agent。"""
    return target


class CollaborativeAgentGraph:
    """注册四个同构 ReAct Agent，并负责它们之间的路由。"""

    def __init__(
        self,
        *,
        model: ChatModel,
        tools: Sequence[BaseTool] = (),
        max_iterations: int = 5,
        max_context_messages: int | None = None,
        max_context_tokens: int | None = None,
        context_token_counter: MessageTokenCounter | None = None,
        tool_timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS,
        tool_timeouts: Mapping[str, float] | None = None,
        tool_permissions: Mapping[str, Collection[AgentRole]] | None = None,
        max_handoffs: int = 4,
        max_agent_switches: int = 8,
        checkpointer: BaseCheckpointSaver[str] | None = None,
        interrupt_before_handoff: bool = False,
    ) -> None:
        if max_handoffs <= 0:
            raise ValueError("max_handoffs must be positive")
        if max_agent_switches <= 0:
            raise ValueError("max_agent_switches must be positive")
        if interrupt_before_handoff and checkpointer is None:
            raise ValueError(
                "interrupt_before_handoff requires a configured checkpointer"
            )

        permissions = tool_permissions or {}
        business_tool_names = {business_tool.name for business_tool in tools}
        permission_names = set(permissions)
        unknown_permissions = permission_names - business_tool_names
        if unknown_permissions:
            names = ", ".join(sorted(unknown_permissions))
            raise ValueError(f"tool_permissions 包含非业务工具：{names}")
        missing_permissions = business_tool_names - permission_names
        if missing_permissions:
            names = ", ".join(sorted(missing_permissions))
            raise ValueError(f"tool_permissions 缺少业务工具：{names}")
        none_permissions = {
            name for name in business_tool_names if permissions[name] is None
        }
        if none_permissions:
            names = ", ".join(sorted(none_permissions))
            raise ValueError(f"tool_permissions 不允许业务工具权限为 None：{names}")

        registry = ToolRegistry()
        registry.register(handoff, allowed_roles={AgentRole.SUPERVISOR})
        for business_tool in tools:
            registry.register(
                business_tool,
                allowed_roles=permissions.get(business_tool.name),
            )
        self.registry = registry
        self.max_handoffs = max_handoffs
        self.max_agent_switches = max_agent_switches
        self.checkpointer = checkpointer
        self.interrupt_before_handoff = interrupt_before_handoff
        self._persistence_lock = RLock()
        # 创建4个同构的agent，均遵循react设计范式
        self.agents = create_agent_nodes(
            model=model,
            registry=registry,
            max_iterations=max_iterations,
            max_context_messages=max_context_messages,
            max_context_tokens=max_context_tokens,
            context_token_counter=context_token_counter,
            tool_timeout_seconds=tool_timeout_seconds,
            tool_timeouts=tool_timeouts,
        )

        # 图缓存，避免重复编译
        self._app: CompiledAgentGraph | None = None

    def build(self) -> CompiledAgentGraph:
        """构建一次并缓存可执行图。"""
        if self._app is not None:
            return self._app

        graph = StateGraph(AgentState)
        # 路由表 = 路由返回值 ： 图节点 （映射）
        routes: dict[Hashable, str] = {
            AgentRole.SUPERVISOR.value: AgentRole.SUPERVISOR.value,
            AgentRole.TEACHING_ASSISTANT.value: AgentRole.TEACHING_ASSISTANT.value,
            AgentRole.LEARNING_ASSISTANT.value: AgentRole.LEARNING_ASSISTANT.value,
            AgentRole.EVALUATOR.value: AgentRole.EVALUATOR.value,
            "end": END,
        }
        if self.interrupt_before_handoff:
            routes[_HANDOFF_APPROVAL_NODE] = _HANDOFF_APPROVAL_NODE

        for role, agent in self.agents.items():
            graph.add_node(role.value, self._wrap(agent))
            graph.add_conditional_edges(role.value, self._route, routes)

        if self.interrupt_before_handoff:
            graph.add_node(_HANDOFF_APPROVAL_NODE, self._approve_handoff)
            graph.add_conditional_edges(
                _HANDOFF_APPROVAL_NODE,
                self._route,
                routes,
            )

        graph.set_entry_point(AgentRole.SUPERVISOR.value)
        self._app = graph.compile(checkpointer=self.checkpointer)
        return self._app

    def _wrap(self, agent: ReActAgentNode) -> Runnable[AgentState, AgentState]:
        """把 ReAct 结果转换为 LangGraph 状态更新。"""

        def node(state: AgentState) -> AgentState:
            existing_error = state.get("run_error")
            if existing_error is not None:
                return cast(
                    AgentState,
                    {"next_agent": None, "run_error": existing_error},
                )

            existing_target = state.get("next_agent")
            registered_targets = {role.value for role in self.agents}
            if (
                existing_target is not None
                and existing_target not in registered_targets
            ):
                error = RunError(
                    error_code=ErrorCode.GRAPH_INVALID_TARGET,
                    message=f"非法 next_agent：{existing_target}",
                    agent=agent.role.value,
                )
                sequence = max(
                    (event.sequence for event in state.get("events", [])),
                    default=-1,
                )
                return cast(
                    AgentState,
                    {
                        "next_agent": None,
                        "run_error": error,
                        "events": [
                            RunEvent(
                                event_type=EventType.RUN_FAILED,
                                sequence=sequence + 1,
                                session_id=state.get("session_id"),
                                agent=agent.role.value,
                                success=False,
                                error_code=error.error_code,
                            )
                        ],
                    },
                )

            result = agent.run(state)

            updates = dict(result.updates)
            tool_results = cast(list[ToolResult], updates.get("tool_results", []))
            target = _handoff_target(tool_results)
            events = cast(list[RunEvent], updates.get("events", []))
            sequence = max(
                (
                    event.sequence
                    for event in [*state.get("events", []), *events]
                ),
                default=-1,
            )

            def emit(
                event_type: EventType,
                event_agent: str,
                *,
                success: bool = True,
                error_code: ErrorCode | None = None,
            ) -> None:
                nonlocal sequence
                sequence += 1
                events.append(
                    RunEvent(
                        event_type=event_type,
                        sequence=sequence,
                        session_id=state.get("session_id"),
                        agent=event_agent,
                        success=success,
                        error_code=error_code,
                    )
                )

            handoff_count = state.get("handoff_count", 0)
            switch_count = state.get("agent_switch_count", 0)
            updates["next_agent"] = None
            updates["run_error"] = None

            def fail(error: RunError) -> AgentState:
                updates["run_error"] = error
                emit(
                    EventType.RUN_FAILED,
                    agent.role.value,
                    success=False,
                    error_code=error.error_code,
                )
                updates["handoff_count"] = handoff_count
                updates["agent_switch_count"] = switch_count
                updates["events"] = events
                return cast(AgentState, updates)

            if result.error is not None:
                return fail(result.error)
            if target is not None and target not in registered_targets:
                return fail(
                    RunError(
                        error_code=ErrorCode.GRAPH_INVALID_TARGET,
                        message=f"非法 next_agent：{target}",
                        agent=agent.role.value,
                    )
                )

            if agent.role is AgentRole.SUPERVISOR:
                if target is not None:
                    if handoff_count + 1 > self.max_handoffs:
                        error = RunError(
                            error_code=ErrorCode.GRAPH_HANDOFF_LIMIT,
                            message=f"handoff 次数超过上限：{self.max_handoffs}",
                            agent=agent.role.value,
                        )
                        return fail(error)
                    if switch_count + 1 > self.max_agent_switches:
                        return fail(
                            RunError(
                                error_code=ErrorCode.GRAPH_SWITCH_LIMIT,
                                message=(
                                    "Agent 切换次数超过上限："
                                    f"{self.max_agent_switches}"
                                ),
                                agent=agent.role.value,
                            )
                        )
                    updates["next_agent"] = target
                    if self.interrupt_before_handoff:
                        updates["pending_handoff"] = HandoffApprovalRequest(
                            target_agent=AgentRole(target),
                            task_content=_latest_human_content(
                                state.get("messages", [])
                            ),
                        )
                    else:
                        handoff_count += 1
                        switch_count += 1
                        emit(EventType.AGENT_SWITCHED, target)
                else:
                    emit(EventType.RUN_COMPLETED, agent.role.value)
            else:
                if switch_count + 1 > self.max_agent_switches:
                    return fail(
                        RunError(
                            error_code=ErrorCode.GRAPH_SWITCH_LIMIT,
                            message=(
                                "Agent 切换次数超过上限："
                                f"{self.max_agent_switches}"
                            ),
                            agent=agent.role.value,
                        )
                    )
                switch_count += 1
                emit(EventType.AGENT_SWITCHED, AgentRole.SUPERVISOR.value)

            updates["handoff_count"] = handoff_count
            updates["agent_switch_count"] = switch_count
            updates["events"] = events
            return cast(AgentState, updates)

        return RunnableLambda(node)

    def _approve_handoff(self, state: AgentState) -> AgentState:
        """暂停并提交分派决定；恢复时仅重放这个无外部副作用的 gate。"""
        pending = state.get("pending_handoff")
        if pending is None:
            raise RuntimeError("handoff approval node requires a pending proposal")
        proposal = HandoffApprovalRequest.model_validate(pending)
        raw_decision = interrupt(proposal.model_dump(mode="json"))
        decision = HandoffApprovalDecision.model_validate(raw_decision)
        sequence = max(
            (event.sequence for event in state.get("events", [])),
            default=-1,
        )

        if decision.action is HandoffApprovalAction.REJECT:
            # 拒绝不执行 Worker 或自动重规划；本轮以成功终止事件安全收口。
            return cast(
                AgentState,
                {
                    "next_agent": None,
                    "pending_handoff": None,
                    "run_error": None,
                    "handoff_count": state.get("handoff_count", 0),
                    "agent_switch_count": state.get("agent_switch_count", 0),
                    "events": [
                        RunEvent(
                            event_type=EventType.RUN_COMPLETED,
                            sequence=sequence + 1,
                            session_id=state.get("session_id"),
                            agent=AgentRole.SUPERVISOR.value,
                            success=True,
                        )
                    ],
                },
            )

        target = decision.target_agent or proposal.target_agent
        handoff_count = state.get("handoff_count", 0)
        switch_count = state.get("agent_switch_count", 0)
        limit_error: RunError | None = None
        if handoff_count + 1 > self.max_handoffs:
            limit_error = RunError(
                error_code=ErrorCode.GRAPH_HANDOFF_LIMIT,
                message=f"handoff 次数超过上限：{self.max_handoffs}",
                agent=AgentRole.SUPERVISOR.value,
            )
        elif switch_count + 1 > self.max_agent_switches:
            limit_error = RunError(
                error_code=ErrorCode.GRAPH_SWITCH_LIMIT,
                message=f"Agent 切换次数超过上限：{self.max_agent_switches}",
                agent=AgentRole.SUPERVISOR.value,
            )
        if limit_error is not None:
            return cast(
                AgentState,
                {
                    "next_agent": None,
                    "pending_handoff": None,
                    "run_error": limit_error,
                    "handoff_count": handoff_count,
                    "agent_switch_count": switch_count,
                    "events": [
                        RunEvent(
                            event_type=EventType.RUN_FAILED,
                            sequence=sequence + 1,
                            session_id=state.get("session_id"),
                            agent=AgentRole.SUPERVISOR.value,
                            success=False,
                            error_code=limit_error.error_code,
                        )
                    ],
                },
            )

        updates: dict[str, object] = {
            "next_agent": target.value,
            "pending_handoff": None,
            "run_error": None,
            "handoff_count": handoff_count + 1,
            "agent_switch_count": switch_count + 1,
            "events": [
                RunEvent(
                    event_type=EventType.AGENT_SWITCHED,
                    sequence=sequence + 1,
                    session_id=state.get("session_id"),
                    agent=target.value,
                    success=True,
                )
            ],
        }
        if decision.task_content is not None:
            task_context = state.get("task_context")
            updates["task_context"] = (
                TaskContext(description=decision.task_content)
                if task_context is None
                else TaskContext.model_validate(task_context).model_copy(
                    update={"description": decision.task_content}
                )
            )
            # 追加而非替换原始消息，既保留审计历史，也让 Worker 看到最新任务。
            updates["messages"] = [HumanMessage(content=decision.task_content)]
        return cast(AgentState, updates)

    @staticmethod
    def _route(state: AgentState) -> str:
        """有 handoff 时转给目标；Worker 完成后回到 Supervisor。"""
        if state.get("run_error") is not None:
            return "end"
        if state.get("pending_handoff") is not None:
            return _HANDOFF_APPROVAL_NODE
        next_agent = state.get("next_agent")
        if next_agent in {role.value for role in AgentRole}:
            return next_agent
        if state.get("current_agent") != AgentRole.SUPERVISOR.value:
            return AgentRole.SUPERVISOR.value
        return "end"

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
        with self._persistence_lock:
            snapshot = app.get_state(config)
            if snapshot.next:
                resume_method = (
                    "resume_handoff()" if snapshot.interrupts else "resume()"
                )
                raise RuntimeError(
                    f"存在待恢复执行，请先调用 {resume_method}"
                )
            if snapshot.values:
                state = cast(
                    AgentState,
                    {
                        "messages": [HumanMessage(content=user_input)],
                        "next_agent": None,
                        "pending_handoff": None,
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
        """恢复普通 pending；动态人工断点必须由 resume_handoff 提交决定。"""
        config = self._thread_config(session_id, user_id)
        if self.checkpointer is None:
            raise ValueError("resume requires a configured checkpointer")
        app = self.build()
        with self._persistence_lock:
            snapshot = app.get_state(config)
            if not snapshot.values or not snapshot.next:
                raise ValueError("当前会话没有待恢复执行")
            # next 同时表示普通 pending 与动态 interrupt；仅后者要求人工决定。
            if snapshot.interrupts:
                raise ValueError(
                    "当前会话等待人工 handoff 决策，请调用 resume_handoff()"
                )
            return cast(AgentState, app.invoke(None, config=config))

    def resume_handoff(
        self,
        session_id: str,
        decision: HandoffApprovalDecision,
        user_id: str | None = None,
    ) -> AgentState:
        """校验 Interrupt ID，并恢复等待人工决定的 handoff gate。"""
        config = self._thread_config(session_id, user_id)
        if self.checkpointer is None:
            raise ValueError("resume_handoff requires a configured checkpointer")
        app = self.build()
        with self._persistence_lock:
            snapshot = app.get_state(config)
            pending = _pending_handoff_from_snapshot(snapshot)
            if not snapshot.next or pending is None:
                raise ValueError("当前会话没有待人工确认的 handoff 断点")
            current_id = pending.interrupt_id
            if decision.interrupt_id != current_id:
                raise ValueError("interrupt_id 与当前 handoff 断点不匹配")
            command: Command[str] = Command(
                resume={
                    current_id: decision.model_dump(mode="json"),
                }
            )
            return cast(AgentState, app.invoke(command, config=config))

    def get_pending_handoff(
        self,
        session_id: str,
        user_id: str | None = None,
    ) -> PendingHandoffApproval | None:
        """从 checkpoint 返回可公开恢复的 handoff 断点；无待确认时返回 None。"""
        config = self._thread_config(session_id, user_id)
        if self.checkpointer is None:
            raise ValueError(
                "get_pending_handoff requires a configured checkpointer"
            )
        app = self.build()
        with self._persistence_lock:
            return _pending_handoff_from_snapshot(app.get_state(config))

    @staticmethod
    def _thread_config(session_id: str, user_id: str | None) -> RunnableConfig:
        user_key = CollaborativeAgentGraph._user_key(user_id)
        session_key = CollaborativeAgentGraph._session_key(session_id)
        # 长度前缀避免分隔符碰撞，none 明示匿名租户，实现 user+session 隔离。
        thread_id = f"user:{user_key}|session:{session_key}"
        return {"configurable": {"thread_id": thread_id}}

    @staticmethod
    def _user_key(user_id: str | None) -> str:
        if user_id is None:
            return "none"
        if not user_id.strip():
            raise ValueError("user_id must not be empty")
        return f"value:{len(user_id)}:{user_id}"

    @staticmethod
    def _session_key(session_id: str) -> str:
        if not session_id.strip():
            raise ValueError("session_id must not be empty")
        return f"{len(session_id)}:{session_id}"

    def get_state(
        self,
        session_id: str,
        user_id: str | None = None,
    ) -> AgentState | None:
        """Return the latest persisted state for a user session."""
        config = self._thread_config(session_id, user_id)
        if self.checkpointer is None:
            raise ValueError("get_state requires a configured checkpointer")
        values = self.build().get_state(config).values
        if not values:
            return None
        return cast(AgentState, dict(values))

    def get_history(
        self,
        session_id: str,
        user_id: str | None = None,
    ) -> list[BaseMessage]:
        """Return the persisted messages for a user session."""
        state = self.get_state(session_id, user_id)
        if state is None:
            return []
        return list(state.get("messages", []))

    def get_node_info(self) -> dict[str, dict[str, str]]:
        """返回节点身份与 Prompt，便于调试和展示。"""
        return {
            role.value: {
                "role": role.value,
                "prompt": agent.system_prompt,
            }
            for role, agent in self.agents.items()
        }


def _handoff_target(tool_results: Sequence[ToolResult]) -> str | None:
    """只读取本次 Agent 调用产生的 handoff 结果。"""
    for result in reversed(tool_results):
        if result.tool_name == "handoff" and result.success:
            return result.output
    return None


def _latest_human_content(messages: Sequence[BaseMessage]) -> str:
    """读取本轮最近用户指令，作为人工确认时展示的初始任务。"""
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return str(message.content)
    return ""


def _interrupt_identifier(pending_interrupt: object) -> str:
    """兼容 LangGraph 0.4 的 interrupt_id 与新版 id 字段。"""
    identifier = getattr(pending_interrupt, "id", None)
    if identifier is None:
        identifier = getattr(pending_interrupt, "interrupt_id", None)
    if not isinstance(identifier, str) or not identifier:
        raise RuntimeError("LangGraph interrupt 缺少稳定标识")
    return identifier


def _pending_handoff_from_snapshot(
    snapshot: StateSnapshot,
) -> PendingHandoffApproval | None:
    """把 checkpoint 内部 interrupt 与 proposal 合成为稳定公开视图。"""
    values = cast(AgentState, snapshot.values)
    pending = values.get("pending_handoff")
    if pending is None:
        return None
    if len(snapshot.interrupts) != 1:
        raise RuntimeError("待确认 handoff 必须对应且仅对应一个 interrupt")
    return PendingHandoffApproval(
        interrupt_id=_interrupt_identifier(snapshot.interrupts[0]),
        request=HandoffApprovalRequest.model_validate(pending),
    )


__all__ = ["CollaborativeAgentGraph", "handoff"]
