"""基于统一 ReAct Agent 的 LangGraph 编排。"""

from __future__ import annotations

import json
from collections.abc import Collection, Hashable, Mapping, Sequence
from threading import RLock
from typing import Literal, cast

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)
from langchain_core.runnables import Runnable, RunnableConfig, RunnableLambda
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, StateSnapshot, interrupt
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .context import MessageTokenCounter
from .events import ErrorCode, EventType, RunError, RunEvent
from .nodes import ReActAgentNode, create_agent_nodes
from .nodes.react_agent import ChatModel
from .state import (
    AgentRole,
    AgentState,
    EvaluationResult,
    EvaluationVerdict,
    HandoffApprovalAction,
    HandoffApprovalDecision,
    HandoffApprovalRequest,
    Intent,
    PendingHandoffApproval,
    StudentLevel,
    TaskContext,
    TaskPlan,
    TaskPlanStatus,
    TaskPlanStep,
    TaskStepResult,
    ToolResult,
    create_initial_state,
    with_agent_role,
)
from .tools import DEFAULT_TOOL_TIMEOUT_SECONDS, ToolRegistry

WorkerRole = Literal["teaching_assistant", "learning_assistant", "evaluator"]
CompiledAgentGraph = CompiledStateGraph[AgentState, None, AgentState, AgentState]
_HANDOFF_APPROVAL_NODE = "handoff_approval"
_TASK_PLAN_DISPATCH_NODE = "task_plan_dispatch"
_TASK_RESULTS_MARKER = "[TASK_RESULTS]"


class _TaskPlanInput(BaseModel):
    """仅暴露给模型的计划输入，不允许模型伪造运行时游标。"""

    model_config = ConfigDict(extra="forbid")

    steps: list[TaskPlanStep] = Field(min_length=2)

    @model_validator(mode="after")
    def validate_complete_plan(self) -> _TaskPlanInput:
        TaskPlan(steps=self.steps)
        return self


@tool
def handoff(target: WorkerRole) -> str:
    """将当前任务交给指定的专业 Agent。"""
    return target


@tool(args_schema=_TaskPlanInput)
def create_task_plan(steps: list[TaskPlanStep]) -> str:
    """为需要至少两个有序子任务的复杂请求创建一次任务计划。"""
    return TaskPlan(steps=steps).model_dump_json()


class _IntentInput(BaseModel):
    """仅暴露给模型的意图分类输入，intent 取值由 Intent 枚举严格约束。

    extra="forbid" 防止模型夹带任意字段（与 _TaskPlanInput 同一约定），
    非法意图值会在工具执行层被 TOOL_INVALID_ARGUMENTS 拒绝，
    不会进入 ToolResult 审计记录。
    reason 不设长度硬约束：超长理由由工具函数截断（见 detect_intent），
    避免 schema 校验失败导致整个意图识别丢失。
    """

    model_config = ConfigDict(extra="forbid")

    intent: Intent
    reason: str = ""


@tool(args_schema=_IntentInput)
def detect_intent(intent: Intent, reason: str = "") -> str:
    """识别当前用户请求的教学意图，返回分类标签（决策前必调）。"""
    # 注意：LangChain 工具执行时把 args 原样传入（intent 是字符串而非
    # Intent 实例），因此这里用 Intent(intent) 显式转换——schema 校验
    # 已保证值是合法枚举，此转换不会失败，且保证输出永远是规范值。
    # reason 截断到 200 字符：审计字段有界（ToolResult.output 不膨胀），
    # 且超长理由不会让工具调用失败、意图识别丢失。
    # 返回 JSON 而非裸枚举值：ToolResult.output 是审计记录，
    # JSON 里同时保留意图与理由，_intent_from_results 只取 intent 字段。
    return json.dumps(
        {"intent": Intent(intent).value, "reason": reason[:200]},
        ensure_ascii=False,
    )


class _LevelInput(BaseModel):
    """仅暴露给模型的水平识别输入，level 取值由 StudentLevel 枚举严格约束。

    与 _IntentInput 同一约定：extra="forbid" 防止模型夹带任意字段，
    非法水平值会在工具执行层被 TOOL_INVALID_ARGUMENTS 拒绝，
    不会进入 ToolResult 审计记录。
    reason 不设长度硬约束：超长理由由工具函数截断（见 detect_level），
    避免 schema 校验失败导致整个水平识别丢失。
    """

    model_config = ConfigDict(extra="forbid")

    level: StudentLevel
    reason: str = ""


@tool(args_schema=_LevelInput)
def detect_level(level: StudentLevel, reason: str = "") -> str:
    """识别或更新学生水平画像（学生自报基础/进阶时调用），返回分类标签。"""
    # 与 detect_intent 同构：LangChain 工具执行时 args 里的 level 是
    # 字符串而非 StudentLevel 实例，这里显式转换保证输出永远是规范值
    # （schema 校验已保证值合法，转换不会失败）。
    # reason 截断到 200 字符：审计字段有界，超长理由不丢失水平识别。
    # 返回 JSON 而非裸枚举值：ToolResult.output 是审计记录，JSON 里同时
    # 保留水平与理由，_level_from_results 只取 level 字段。
    return json.dumps(
        {"level": StudentLevel(level).value, "reason": reason[:200]},
        ensure_ascii=False,
    )


class _EvaluationInput(BaseModel):
    """仅暴露给模型的评价输入，verdict 与两个维度取值由枚举严格约束。

    与 _IntentInput 同一约定：extra="forbid" 防止模型夹带任意字段，
    非法评价值会在工具执行层被 TOOL_INVALID_ARGUMENTS 拒绝，
    不会进入 ToolResult 审计记录。
    reason 不设长度硬约束：超长理由由工具函数截断（见 submit_evaluation），
    避免 schema 校验失败导致整个评价丢失。

    三个结论字段为何共用 EvaluationVerdict 枚举：总结论（verdict）与
    单维度结论（fact_accuracy / citation_completeness）语义一致
    （通过/存疑/不通过），复用同一枚举避免平行定义；verdict 与维度
    之间的一致性（如总评 fail 时维度不应全 pass）不做硬校验——骨架期
    最小可用，写入端只保证枚举合法，语义一致性留给 reason 与审计对照。
    """

    model_config = ConfigDict(extra="forbid")

    verdict: EvaluationVerdict
    fact_accuracy: EvaluationVerdict
    citation_completeness: EvaluationVerdict
    reason: str = ""


@tool(args_schema=_EvaluationInput)
def submit_evaluation(
    verdict: EvaluationVerdict,
    fact_accuracy: EvaluationVerdict,
    citation_completeness: EvaluationVerdict,
    reason: str = "",
) -> str:
    """提交对最终回答的结构化评价结论（评价 Agent 专用，决策后必调）。"""
    # 与 detect_intent 同构：LangChain 工具执行时 args 里的枚举值是
    # 字符串而非枚举实例，这里显式转换保证输出永远是规范值（schema
    # 校验已保证值合法，转换不会失败）。
    # reason 截断到 200 字符：审计字段有界（ToolResult.output 不膨胀），
    # 且超长理由不会让评价丢失；理由可能含被评价内容细节，事件层只取
    # verdict 摘要（脱敏见 events.py），完整 reason 存 state["evaluation"]。
    # 返回 JSON 而非裸枚举值：ToolResult.output 是审计记录，JSON 里同时
    # 保留总结论、两维度结论与理由，_evaluation_from_results 解析取用。
    return json.dumps(
        {
            "verdict": EvaluationVerdict(verdict).value,
            "fact_accuracy": EvaluationVerdict(fact_accuracy).value,
            "citation_completeness": EvaluationVerdict(citation_completeness).value,
            "reason": reason[:200],
        },
        ensure_ascii=False,
    )


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
        registry.register(
            create_task_plan,
            allowed_roles={AgentRole.SUPERVISOR},
        )
        # S2-T1 意图识别：detect_intent 仅 Supervisor 可用，
        # 与 handoff / create_task_plan 一样由模型在 ReAct 循环中调用。
        registry.register(
            detect_intent,
            allowed_roles={AgentRole.SUPERVISOR},
        )
        # S2-T2 学生水平画像：detect_level 仅 Supervisor 可用（与
        # detect_intent 同一约定），由模型在学生自报水平时调用。
        registry.register(
            detect_level,
            allowed_roles={AgentRole.SUPERVISOR},
        )
        # S2-T3 基础评价规则：submit_evaluation 仅 evaluator 可用，
        # 由评价 Agent 在 ReAct 循环中基于最终回答与检索证据调用
        # （prompt 约定，见 ROLE_PROMPTS[EVALUATOR]），结果经 _wrap
        # 解析写入 state["evaluation"] 并发 EVALUATION_COMPLETED 事件。
        registry.register(
            submit_evaluation,
            allowed_roles={AgentRole.EVALUATOR},
        )
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
            _TASK_PLAN_DISPATCH_NODE: _TASK_PLAN_DISPATCH_NODE,
            "end": END,
        }
        if self.interrupt_before_handoff:
            routes[_HANDOFF_APPROVAL_NODE] = _HANDOFF_APPROVAL_NODE

        for role, agent in self.agents.items():
            graph.add_node(role.value, self._wrap(agent))
            graph.add_conditional_edges(role.value, self._route, routes)

        graph.add_node(_TASK_PLAN_DISPATCH_NODE, self._dispatch_task_plan)
        graph.add_conditional_edges(
            _TASK_PLAN_DISPATCH_NODE,
            self._route,
            routes,
        )

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

            preflight_plan, preflight_error = _planned_worker_preflight(
                state,
                agent.role,
            )
            if preflight_error is not None:
                sequence = max(
                    (event.sequence for event in state.get("events", [])),
                    default=-1,
                )
                preflight_updates: dict[str, object] = {
                    "next_agent": None,
                    "run_error": preflight_error,
                    "events": [
                        RunEvent(
                            event_type=EventType.RUN_FAILED,
                            sequence=sequence + 1,
                            session_id=state.get("session_id"),
                            agent=agent.role.value,
                            success=False,
                            error_code=preflight_error.error_code,
                        )
                    ],
                }
                if preflight_plan is not None:
                    preflight_updates["task_plan"] = preflight_plan.model_copy(
                        update={"status": TaskPlanStatus.FAILED}
                    )
                return cast(AgentState, preflight_updates)

            aggregation_results: list[TaskStepResult] | None = None
            run_state = state
            if agent.role is AgentRole.SUPERVISOR:
                try:
                    aggregation_results = _ready_task_results(state)
                except ValueError:
                    plan = _task_plan_from_state(state)
                    error = RunError(
                        error_code=ErrorCode.GRAPH_AGGREGATION_INVALID,
                        message="任务结果与计划不一致，无法安全聚合",
                        agent=agent.role.value,
                    )
                    sequence = max(
                        (event.sequence for event in state.get("events", [])),
                        default=-1,
                    )
                    aggregation_failure_updates: dict[str, object] = {
                        "next_agent": None,
                        "run_error": error,
                        "events": [
                            RunEvent(
                                event_type=EventType.TASK_RESULTS_AGGREGATED,
                                sequence=sequence + 1,
                                session_id=state.get("session_id"),
                                agent=agent.role.value,
                                success=False,
                                error_code=error.error_code,
                            ),
                            RunEvent(
                                event_type=EventType.RUN_FAILED,
                                sequence=sequence + 2,
                                session_id=state.get("session_id"),
                                agent=agent.role.value,
                                success=False,
                                error_code=error.error_code,
                            ),
                        ],
                    }
                    if plan is not None:
                        aggregation_failure_updates["task_plan"] = plan.model_copy(
                            update={"status": TaskPlanStatus.FAILED}
                        )
                    return cast(AgentState, aggregation_failure_updates)
                if aggregation_results is not None:
                    plan = _task_plan_from_state(state)
                    if plan is None:
                        raise RuntimeError("aggregation requires a task plan")
                    run_state = cast(
                        AgentState,
                        {
                            **state,
                            "messages": [
                                *state.get("messages", []),
                                _task_results_message(plan, aggregation_results),
                            ],
                        },
                    )

            result = agent.run(run_state)

            updates = dict(result.updates)
            # ── 注入「产出 Agent 角色」元数据：写入会话历史的唯一闸口 ──
            # _wrap 节点是本次执行所有消息进入 state["messages"]（进而进入
            # checkpointer 持久化）的唯一入口，因此在这里统一给助手消息
            # 打上角色标记，一处覆盖最终回答与带 tool_calls 的中间助手消息。
            #
            # 为什么选这个注入点而不是 ReActAgentNode 内部：
            # 1) ReActAgentNode 是模型边界，其生成的消息会作为下一轮模型
            #    输入；在内部注入会让带角色标记的消息污染模型看到的上下文，
            #    在这里注入则消息仅写入持久化历史——当前 OpenAI 兼容
            #    provider 不会将该键透传给模型 API；若未来接入会透传
            #    additional_kwargs 的 provider，需重新评估该注入点。
            # 2) ReActAgentNode 的单元测试断言 additional_kwargs 精确相等
            #    （test_react_agent.py::test_react_agent_preserves_*），
            #    注入放在图层面既不改变节点语义，也不破坏单元契约。
            # 3) 未来新增图节点只要走 _wrap，就不会漏标角色。
            #
            # 边界情况：
            # - 只处理 AIMessage；HumanMessage（用户输入/任务描述）与
            #   ToolMessage（工具返回）不是助手产出，一律不注入。
            # - 失败轮次（模型调用失败、迭代超限）已产生的助手消息同样
            #   注入，保证历史里每条 AI 消息都有确定的产出者。
            # - 后续聚合逻辑（_replace_terminal_ai_output 等）用 model_copy
            #   仅替换 content，会原样保留 additional_kwargs，因此 Supervisor
            #   聚合的最终回答仍携带 supervisor 角色，不会因改内容而丢失。
            updates["messages"] = [
                with_agent_role(message, agent.role)
                if isinstance(message, AIMessage)
                else message
                for message in cast(list[BaseMessage], updates.get("messages", []))
            ]
            tool_results = cast(list[ToolResult], updates.get("tool_results", []))
            target = _handoff_target(tool_results)
            new_plan = _task_plan_from_results(tool_results)
            existing_plan = _task_plan_from_state(state)
            # ── S2-T1 意图识别：解析模型分类，并对「意图不明」做分派拦截 ──
            # 原理：detect_intent 是 Supervisor 决策前的必备工具（prompt 约定），
            # 其成功结果经 _intent_from_results 校验后成为本轮权威意图。若模型
            # 自报 UNCLEAR（无法确定）却仍试图 handoff 或 create_task_plan，
            # 说明模型违背了「不明即追问」的约定；这里直接把分派动作丢弃
            # （target/new_plan 置 None），让 ReAct 循环继续到模型输出澄清性
            # 回答，从而做到「不随意分派」的运行时硬保障，而不只依赖 prompt。
            #
            # 边界：
            # - 拦截只针对「模型自报 UNCLEAR 仍强行分派」这一种违约；
            #   模型跳过 detect_intent（intent=None）或误报其他意图
            #   （如把备课误报为答疑）属既定的兼容设计，不在此拦截——
            #   前者兼容旧行为与历史替身，后者由 Worker 与评价链路兜底；
            # - 模型在 UNCLEAR 后一直输出工具调用直到迭代超限，会走既有的
            #   REACT_ITERATION_LIMIT 失败路径（fail 分支），不会无限循环；
            # - 兼容旧行为：不调用 detect_intent 的模型（如历史测试替身）拿到
            #   intent=None，不触发拦截，行为与 S2-T1 之前完全一致。
            intent = _intent_from_results(tool_results)
            if (
                intent is Intent.UNCLEAR
                and agent.role is AgentRole.SUPERVISOR
                and (target is not None or new_plan is not None)
            ):
                target = None
                new_plan = None
            # ── S2-T2 学生水平画像：解析模型识别的水平并写入 state ──
            # 与 intent 的生命周期语义相反（这是本任务的关键设计）：
            # - intent 每轮重置、只属于「本轮」（run() 重置列表含 intent）；
            # - level 是「跨轮保留的学生画像」：本轮未调用 detect_level
            #   时保留 checkpoint 中的旧值（run() 的重置列表刻意不含
            #   level），首次提问无水平信息时为 None，读取侧按
            #   StudentLevel.UNKNOWN 归一（默认中等深度）。
            # 写入不依赖「确定分派」：学生自报水平即使本轮直接回答
            # （无 handoff/计划），也应记录进画像——跨轮画像要为后续
            # 轮次的分层讲解服务，这正是「保留而非重置」的意义。
            level = _level_from_results(tool_results)
            if level is not None:
                updates["level"] = level.value
            # 意图识别结果同步进跨轮持久字段 task_context.intent：
            # Worker 与聚合阶段可读取意图标签做针对性工作，同时保留审计轨迹。
            # 仅在确定分派（非 UNCLEAR、确有目标或计划）时写入，避免「直接回答
            # 澄清问题」这类无任务轮次污染任务上下文。
            # 水平画像在同一处、同一条件同步进 task_context.level（与
            # task_context.intent 同构的快照）：本轮新识别的水平优先，
            # 否则沿用 state 中保留的旧画像——保证分派给 Worker 的任务
            # 上下文始终携带「为哪个水平的学生讲解」，而 state["level"]
            # 仍是权威来源。
            if (
                intent is not None
                and intent is not Intent.UNCLEAR
                and (target is not None or new_plan is not None)
            ):
                current_level = (
                    level.value if level is not None else state.get("level")
                )
                existing_context = state.get("task_context")
                if existing_context is None:
                    updates["task_context"] = TaskContext(
                        intent=intent.value,
                        level=current_level or "",
                    )
                else:
                    context_update: dict[str, str] = {"intent": intent.value}
                    if current_level is not None:
                        context_update["level"] = current_level
                    updates["task_context"] = TaskContext.model_validate(
                        existing_context
                    ).model_copy(update=context_update)
            plan = existing_plan or new_plan
            replacing_plan = new_plan is not None and existing_plan is not None
            if new_plan is not None and existing_plan is None:
                updates["task_plan"] = new_plan
                updates["task_results"] = []
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
                plan_step_sequence: int | None = None,
                degraded: bool | None = None,
                event_intent: str | None = None,
                event_verdict: str | None = None,
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
                        plan_step_sequence=plan_step_sequence,
                        degraded=degraded,
                        intent=event_intent,
                        evaluation_verdict=event_verdict,
                    )
                )

            # ── S2-T1 意图事件与状态写入 ──
            # 事件是瞬时信号：消费方（api/chat.py 的 EVENT_TYPE_MAP 白名单）对
            # 未映射的新事件类型安全跳过，因此 INTENT_DETECTED 目前只对内部
            # 审计可见（state["events"]），前端流式协议不受影响；后续若要在
            # 前端展示意图，只需在 api 层映射表补一行，无需改 core。
            # state["intent"] 则是持久权威值（见 state.py 字段注释）。
            # 注意两处都写 intent.value（字符串）：state 通道与事件字段都
            # 只存 msgpack 原生类型，避免 checkpoint 对自定义枚举的反序列化
            # 注册依赖；本函数内部的路由判断仍用 Intent 枚举（intent 变量）。
            if intent is not None:
                updates["intent"] = intent.value
                emit(
                    EventType.INTENT_DETECTED,
                    agent.role.value,
                    event_intent=intent.value,
                )

            # ── S2-T3 评价结论：解析 submit_evaluation 结果并写入 state/事件 ──
            # 触发时机设计：评价作为 evaluator 的 ReAct 轮内动作（prompt +
            # 工具约定，与 detect_intent/detect_level 同构），在「最终回答
            # 产出后」（evaluator 轮结束时）由 _wrap 统一解析——不新增图
            # 节点/路由，不改聚合轮，事件协议向后兼容（EVENT_TYPE_MAP 对
            # 未知事件安全跳过）。模型先观察检索证据（ToolMessage），再
            # 调用 submit_evaluation，最后给出评价文本，_wrap 在轮末把
            # 结论写入 state["evaluation"]。
            #
            # 评价输入如何组装（「不凭空评价」的确定性保障）：
            # - 最终回答：在模型可见的消息历史中（ReAct 输入），无需额外注入；
            # - 本轮检索证据：以 ToolResult 进入模型上下文（工具观察），
            #   _wrap 解析时把「本轮 evaluator 成功执行、有输出、且不是
            #   submit_evaluation 本身」的工具名组装进
            #   EvaluationResult.evidence_tool_names——只记工具名不记正文
            #   （正文仍在 state["tool_results"] 按工具结果审计），既给审计
            #   留了「评价基于哪些证据」的核对线索，又不把证据正文复制进
            #   评价模型造成双重存储。
            # 未调用 submit_evaluation（旧行为/历史替身/模型违约）→
            # evaluation 保持 None、不发事件，运行不受影响。
            # 角色守卫（纯防御性）：只允许 evaluator 轮的 submit_evaluation
            # 结果进入解析与写入。当前权限模型下（注册时 allowed_roles 仅
            # evaluator）成功记录只可能来自 evaluator，其他角色的调用必然
            # TOOL_UNAUTHORIZED 失败；守卫保证未来权限调整也不会让非评价
            # 角色写入评价结论。
            evaluation_input = (
                _evaluation_from_results(tool_results)
                if agent.role is AgentRole.EVALUATOR
                else None
            )
            if evaluation_input is not None:
                # 证据工具名：本轮成功且有输出的工具结果，排除评价工具本身；
                # 保持出现顺序去重（dict.fromkeys），保证审计列表稳定可读。
                # 骨架期取舍：范围偏宽——凡成功有输出的业务工具都算「证据」
                # （可能含非检索类业务工具），不区分类型；S2-T4 引入 Citation
                # 结构化引用后，此处应按证据类型（Citation/检索类工具）过滤，
                # 只记录真正的检索证据来源。
                evidence_tools = list(
                    dict.fromkeys(
                        result.tool_name
                        for result in tool_results
                        if result.success
                        and result.output
                        and result.tool_name != "submit_evaluation"
                    )
                )
                # 边界语义（与 intent 写入模式一致）：若 evaluator 本轮先成功
                # 提交了 submit_evaluation、随后模型迭代超限或调用失败，会
                # 出现「state["evaluation"] 有结论 + RUN_FAILED 事件」共存——
                # 评价结论本身完整（工具已成功执行），失败发生在评价产出之后，
                # 两者是先后关系而非矛盾，审计者按事件序列读取即可。intent/
                # level 同此语义（成功工具结果先写、轮末失败再补 RUN_FAILED）。
                # 设计边界：「不凭空评价」目前无运行时硬校验——模型在零证据
                # （无任何检索工具调用）下提交 pass 不会被拦截。骨架期以
                # prompt 约定（禁止凭空评价）+ 审计闭环（evidence_tool_names
                # 记录本轮实际证据，审计者可见「零证据却判通过」的可疑评价）
                # 实现；S2-T5 做引用真实性校验时再考虑运行时加强。
                updates["evaluation"] = EvaluationResult(
                    verdict=EvaluationVerdict(evaluation_input["verdict"]),
                    fact_accuracy=EvaluationVerdict(
                        evaluation_input["fact_accuracy"]
                    ),
                    citation_completeness=EvaluationVerdict(
                        evaluation_input["citation_completeness"]
                    ),
                    reason=evaluation_input["reason"],
                    evidence_tool_names=evidence_tools,
                )
                # 事件脱敏：只发 verdict 摘要（无敏感正文），完整结论
                # （含 reason）在 state["evaluation"] 随 checkpoint 持久化。
                emit(
                    EventType.EVALUATION_COMPLETED,
                    agent.role.value,
                    event_verdict=evaluation_input["verdict"],
                )

            handoff_count = state.get("handoff_count", 0)
            switch_count = state.get("agent_switch_count", 0)
            updates["next_agent"] = None
            updates["run_error"] = None

            def fail(error: RunError) -> AgentState:
                updates["run_error"] = error
                if plan is not None and plan.status not in {
                    TaskPlanStatus.CANCELLED,
                    TaskPlanStatus.FAILED,
                }:
                    updates["task_plan"] = plan.model_copy(
                        update={"status": TaskPlanStatus.FAILED}
                    )
                if aggregation_results is not None:
                    emit(
                        EventType.TASK_RESULTS_AGGREGATED,
                        AgentRole.SUPERVISOR.value,
                        success=False,
                        error_code=error.error_code,
                        degraded=any(
                            not item.success for item in aggregation_results
                        ),
                    )
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

            planned_worker = (
                agent.role is not AgentRole.SUPERVISOR
                and plan is not None
                and plan.status is TaskPlanStatus.ACTIVE
            )
            recoverable_planned_error = (
                planned_worker
                and result.error is not None
                and result.error.error_code
                in {
                    ErrorCode.MODEL_CALL_FAILED,
                    ErrorCode.REACT_ITERATION_LIMIT,
                }
            )
            if result.error is not None and not recoverable_planned_error:
                return fail(result.error)
            if replacing_plan:
                return fail(
                    RunError(
                        error_code=ErrorCode.GRAPH_INVALID_TARGET,
                        message="当前用户轮次已存在任务计划，不允许覆盖",
                        agent=agent.role.value,
                    )
                )
            if target is not None and target not in registered_targets:
                return fail(
                    RunError(
                        error_code=ErrorCode.GRAPH_INVALID_TARGET,
                        message=f"非法 next_agent：{target}",
                        agent=agent.role.value,
                    )
                )

            if target is not None and plan is not None:
                if plan.status is not TaskPlanStatus.ACTIVE:
                    return fail(
                        RunError(
                            error_code=ErrorCode.GRAPH_INVALID_TARGET,
                            message="任务计划结束后不允许继续 handoff",
                            agent=agent.role.value,
                        )
                    )
                expected_target = plan.steps[
                    plan.current_step_index
                ].target_agent.value
                if target != expected_target:
                    return fail(
                        RunError(
                            error_code=ErrorCode.GRAPH_INVALID_TARGET,
                            message=f"handoff 目标偏离当前计划步骤：{target}",
                            agent=agent.role.value,
                        )
                    )
                # 活动计划由确定性调度节点分派；一致的模型 handoff 仅作冗余观察。
                target = None

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
                elif plan is None or plan.status is not TaskPlanStatus.ACTIVE:
                    if aggregation_results is not None:
                        if plan is None:
                            raise RuntimeError("aggregation requires a task plan")
                        generated = cast(
                            list[BaseMessage],
                            updates.get("messages", []),
                        )
                        fallback_used = _terminal_agent_output(generated) is None
                        if fallback_used:
                            updates["messages"] = _replace_terminal_ai_output(
                                generated,
                                _deterministic_aggregation(
                                    plan,
                                    aggregation_results,
                                ),
                            )
                            events = _mark_agent_completion_invalid(
                                events,
                                AgentRole.SUPERVISOR,
                            )
                        has_missing_results = any(
                            not item.success for item in aggregation_results
                        )
                        if has_missing_results and not fallback_used:
                            updates["messages"] = _append_missing_results_notice(
                                generated,
                                plan,
                                aggregation_results,
                            )
                        emit(
                            EventType.TASK_RESULTS_AGGREGATED,
                            agent.role.value,
                            degraded=has_missing_results or fallback_used,
                        )
                    emit(EventType.RUN_COMPLETED, agent.role.value)
            else:
                if plan is not None and plan.status is TaskPlanStatus.ACTIVE:
                    step = plan.steps[plan.current_step_index]
                    if step.target_agent is not agent.role:
                        return fail(
                            RunError(
                                error_code=ErrorCode.GRAPH_INVALID_TARGET,
                                message=(
                                    "当前 Worker 与计划步骤目标不一致："
                                    f"{agent.role.value}"
                                ),
                                agent=agent.role.value,
                            )
                        )
                    try:
                        existing_results = _task_results_from_state(state)
                        _validate_task_result_prefix(plan, existing_results)
                    except ValueError:
                        return fail(
                            RunError(
                                error_code=ErrorCode.GRAPH_AGGREGATION_INVALID,
                                message="已有任务结果与计划游标不一致",
                                agent=agent.role.value,
                            )
                        )
                    output = (
                        None
                        if result.error is not None
                        else _terminal_agent_output(result.messages)
                    )
                    result_error_code = (
                        result.error.error_code
                        if result.error is not None
                        else None
                    )
                    if result_error_code is None and output is None:
                        result_error_code = ErrorCode.AGENT_OUTPUT_INVALID
                        events = _mark_agent_completion_invalid(
                            events,
                            agent.role,
                        )
                    step_result = TaskStepResult(
                        step_sequence=step.sequence,
                        target_agent=step.target_agent,
                        success=result_error_code is None,
                        output=output,
                        error_code=result_error_code,
                    )
                    task_results = [*existing_results, step_result]
                    next_index = plan.current_step_index + 1
                    next_status = (
                        TaskPlanStatus.COMPLETED
                        if next_index == len(plan.steps)
                        else TaskPlanStatus.ACTIVE
                    )
                    plan = plan.model_copy(
                        update={
                            "current_step_index": next_index,
                            "status": next_status,
                        }
                    )
                    updates["task_plan"] = plan
                    updates["task_results"] = task_results
                    emit(
                        EventType.TASK_RESULT_ARCHIVED,
                        agent.role.value,
                        success=step_result.success,
                        error_code=step_result.error_code,
                        plan_step_sequence=step.sequence,
                    )
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

    def _dispatch_task_plan(self, state: AgentState) -> AgentState:
        """按持久化计划选择下一 Worker，不把顺序控制交还给模型。"""
        plan = _task_plan_from_state(state)
        if plan is None or plan.status is not TaskPlanStatus.ACTIVE:
            raise RuntimeError("task plan dispatch requires an active plan")
        step = plan.steps[plan.current_step_index]
        handoff_count = state.get("handoff_count", 0)
        switch_count = state.get("agent_switch_count", 0)
        sequence = max(
            (event.sequence for event in state.get("events", [])),
            default=-1,
        )

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
                    "current_agent": AgentRole.SUPERVISOR.value,
                    "next_agent": None,
                    "pending_handoff": None,
                    "task_plan": plan.model_copy(
                        update={"status": TaskPlanStatus.FAILED}
                    ),
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
            "current_agent": AgentRole.SUPERVISOR.value,
            "next_agent": step.target_agent.value,
            "pending_handoff": None,
            "task_plan": plan,
            "run_error": None,
            "handoff_count": handoff_count,
            "agent_switch_count": switch_count,
        }
        if self.interrupt_before_handoff:
            updates["pending_handoff"] = HandoffApprovalRequest(
                target_agent=step.target_agent,
                task_content=step.description,
                plan_step_sequence=step.sequence,
            )
        else:
            updates["messages"] = [HumanMessage(content=step.description)]
            updates["handoff_count"] = handoff_count + 1
            updates["agent_switch_count"] = switch_count + 1
            updates["events"] = [
                RunEvent(
                    event_type=EventType.AGENT_SWITCHED,
                    sequence=sequence + 1,
                    session_id=state.get("session_id"),
                    agent=step.target_agent.value,
                    success=True,
                )
            ]
        return cast(AgentState, updates)

    def _approve_handoff(self, state: AgentState) -> AgentState:
        """暂停并提交分派决定；恢复时仅重放这个无外部副作用的 gate。"""
        pending = state.get("pending_handoff")
        if pending is None:
            raise RuntimeError("handoff approval node requires a pending proposal")
        proposal = HandoffApprovalRequest.model_validate(pending)
        raw_decision = interrupt(
            proposal.model_dump(mode="json", exclude_none=True)
        )
        decision = HandoffApprovalDecision.model_validate(raw_decision)
        sequence = max(
            (event.sequence for event in state.get("events", [])),
            default=-1,
        )

        if decision.action is HandoffApprovalAction.REJECT:
            # 拒绝不执行 Worker 或自动重规划；本轮以成功终止事件安全收口。
            reject_updates: dict[str, object] = {
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
            }
            rejected_plan = _task_plan_for_proposal(state, proposal)
            if rejected_plan is not None:
                reject_updates["task_plan"] = rejected_plan.model_copy(
                    update={"status": TaskPlanStatus.CANCELLED}
                )
            return cast(
                AgentState,
                reject_updates,
            )

        target = decision.target_agent or proposal.target_agent
        task_content = decision.task_content or proposal.task_content
        planned = _task_plan_for_proposal(state, proposal)
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
            failure_updates: dict[str, object] = {
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
            }
            if planned is not None:
                failure_updates["task_plan"] = planned.model_copy(
                    update={"status": TaskPlanStatus.FAILED}
                )
            return cast(
                AgentState,
                failure_updates,
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
        if planned is not None:
            step_index = planned.current_step_index
            steps = list(planned.steps)
            steps[step_index] = steps[step_index].model_copy(
                update={
                    "target_agent": target,
                    "description": task_content,
                }
            )
            updates["task_plan"] = planned.model_copy(update={"steps": steps})
            updates["messages"] = [HumanMessage(content=task_content)]
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
        plan = _task_plan_from_state(state)
        if plan is not None and plan.status is TaskPlanStatus.ACTIVE:
            return _TASK_PLAN_DISPATCH_NODE
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
                        # S2-T1：每轮重新识别意图，旧意图随新轮清除，
                        # 避免上一轮的意图误导本轮路由。
                        "intent": None,
                        # S2-T3：评价结论是「本轮结论」（与 intent 同构），
                        # 新轮重置为 None——若跨轮保留，上一轮的评价徽章
                        # 会误导后续轮次的展示与审计。
                        "evaluation": None,
                        # S2-T2：这里刻意不重置 level——学生水平是「跨轮
                        # 保留的画像」（与 intent 相反），只有模型再次调用
                        # detect_level 时才覆盖；若随新轮重置，已建立的
                        # 水平画像会丢失，分层讲解也随之失效。
                        "task_plan": None,
                        "task_results": [],
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
        """Return the persisted messages for a user session.

        返回的消息与改动前完全同构：类型与 content 不变，只是助手消息
        （AIMessage）的 additional_kwargs 新增了 AGENT_ROLE_METADATA_KEY
        键。调用方可用 core.state.message_agent_role(message) 读出产出该
        消息的 Agent 角色（枚举值），用于前端角色徽章等展示。
        """
        state = self.get_state(session_id, user_id)
        if state is None:
            return []
        return list(state.get("messages", []))

    def get_node_info(self) -> dict[str, dict[str, str]]:
        """返回节点身份与 Prompt，便于调试和展示。

        注意：返回的是静态系统提示词（ROLE_PROMPTS 角色卡）。S2-T2 起，
        助学 Agent（learning_assistant）的讲解深度按学生水平分层，其运行时
        实际发给模型的 system prompt 是静态卡 + 「[当前学生水平:...]」动态
        水平段（见 prompts.learning_assistant_system_prompt，按
        state["level"] 每轮生成）；如需查看完整提示词，请对该角色调用该函数。
        """
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


def _intent_from_results(tool_results: Sequence[ToolResult]) -> Intent | None:
    """只读取本次 Supervisor 成功调用的最后一个 detect_intent 结果。

    写入端严格（工具 schema 用 Intent 枚举校验 + 工具函数输出固定 JSON），
    读取端宽容：解析失败返回 None 而非抛错——与 message_agent_role 的
    哲学一致，宁可让本轮退化为「无意图」也不让脏数据击穿运行。
    返回 None 表示模型未识别（或识别结果不可信），不会触发 UNCLEAR 拦截。
    """
    for result in reversed(tool_results):
        if result.tool_name == "detect_intent" and result.success:
            try:
                payload = json.loads(result.output)
                return Intent(str(payload["intent"]))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                return None
    return None


def _level_from_results(tool_results: Sequence[ToolResult]) -> StudentLevel | None:
    """只读取本次 Supervisor 成功调用的最后一个 detect_level 结果。

    与 _intent_from_results 同一哲学（写入端严格、读取端宽容）：
    解析失败返回 None 而非抛错，本轮退化为「水平未知」，不击穿运行。
    与意图的关键差异：返回 None 只表示「本轮未更新水平画像」，
    不会清空 checkpoint 中已保留的旧水平（跨轮保留语义，见 state.py
    AgentState.level 注释）——是否覆盖旧值由调用方决定。
    """
    for result in reversed(tool_results):
        if result.tool_name == "detect_level" and result.success:
            try:
                payload = json.loads(result.output)
                return StudentLevel(str(payload["level"]))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                return None
    return None


def _evaluation_from_results(
    tool_results: Sequence[ToolResult],
) -> dict[str, str] | None:
    """只读取本次 evaluator 成功调用的最后一个 submit_evaluation 结果。

    与 _intent_from_results 同一哲学（写入端严格、读取端宽容）：
    写入端由工具 schema 的枚举约束保证合法；读取端在此处再做一次
    枚举校验——历史脏数据或未来枚举变更产生的非法值抛 ValueError
    被捕获，返回 None 而非抛错，本轮退化为「无评价」，不击穿运行
    （也兼容未调 submit_evaluation 的旧行为与历史替身，见 _wrap 注释）。

    返回的 dict 只含四个模型填写的字段（verdict、fact_accuracy、
    citation_completeness、reason，枚举均为规范值字符串），证据工具名
    列表由调用方（_wrap）从本轮 tool_results 单独组装进
    EvaluationResult——证据是核心侧确定的「评价输入」记录，不由模型自报。
    """
    for result in reversed(tool_results):
        if result.tool_name == "submit_evaluation" and result.success:
            try:
                payload = json.loads(result.output)
                return {
                    "verdict": EvaluationVerdict(str(payload["verdict"])).value,
                    "fact_accuracy": EvaluationVerdict(
                        str(payload["fact_accuracy"])
                    ).value,
                    "citation_completeness": EvaluationVerdict(
                        str(payload["citation_completeness"])
                    ).value,
                    "reason": str(payload.get("reason", "")),
                }
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                return None
    return None


def _task_plan_from_results(tool_results: Sequence[ToolResult]) -> TaskPlan | None:
    """只解析本次 Supervisor 成功创建的最后一个结构化计划。"""
    for result in reversed(tool_results):
        if result.tool_name == "create_task_plan" and result.success:
            return TaskPlan.model_validate_json(result.output)
    return None


def _task_plan_from_state(state: AgentState) -> TaskPlan | None:
    raw_plan = state.get("task_plan")
    return None if raw_plan is None else TaskPlan.model_validate(raw_plan)


def _task_plan_for_proposal(
    state: AgentState,
    proposal: HandoffApprovalRequest,
) -> TaskPlan | None:
    """校验计划型审批仍对应 checkpoint 中未推进的当前步骤。"""
    if proposal.plan_step_sequence is None:
        return None
    plan = _task_plan_from_state(state)
    if plan is None or plan.status is not TaskPlanStatus.ACTIVE:
        raise RuntimeError("planned handoff requires an active task plan")
    step = plan.steps[plan.current_step_index]
    if step.sequence != proposal.plan_step_sequence:
        raise RuntimeError("planned handoff no longer matches current step")
    return plan


def _task_results_from_state(state: AgentState) -> list[TaskStepResult]:
    return [
        TaskStepResult.model_validate(result)
        for result in state.get("task_results", [])
    ]


def _planned_worker_preflight(
    state: AgentState,
    role: AgentRole,
) -> tuple[TaskPlan | None, RunError | None]:
    plan = _task_plan_from_state(state)
    if role is AgentRole.SUPERVISOR or plan is None:
        return plan, None
    if plan.status is not TaskPlanStatus.ACTIVE:
        return plan, None
    step = plan.steps[plan.current_step_index]
    if step.target_agent is not role:
        return plan, RunError(
            error_code=ErrorCode.GRAPH_INVALID_TARGET,
            message=f"当前 Worker 与计划步骤目标不一致：{role.value}",
            agent=role.value,
        )
    try:
        _validate_task_result_prefix(plan, _task_results_from_state(state))
    except ValueError:
        return plan, RunError(
            error_code=ErrorCode.GRAPH_AGGREGATION_INVALID,
            message="已有任务结果与计划游标不一致",
            agent=role.value,
        )
    return plan, None


def _validate_task_result_prefix(
    plan: TaskPlan,
    results: Sequence[TaskStepResult],
) -> None:
    """结果必须是当前计划从第一步开始的连续、同角色前缀。"""
    if len(results) != plan.current_step_index:
        raise ValueError("task result count does not match plan cursor")
    for result, step in zip(
        results,
        plan.steps[: len(results)],
        strict=True,
    ):
        if (
            result.step_sequence != step.sequence
            or result.target_agent is not step.target_agent
        ):
            raise ValueError("task result does not match plan step")


def _ready_task_results(state: AgentState) -> list[TaskStepResult] | None:
    plan = _task_plan_from_state(state)
    if plan is None or plan.status is not TaskPlanStatus.COMPLETED:
        return None
    results = _task_results_from_state(state)
    _validate_task_result_prefix(plan, results)
    if len(results) != len(plan.steps):
        raise ValueError("completed plan is missing task results")
    return results


def _terminal_agent_output(messages: Sequence[BaseMessage]) -> str | None:
    """仅接收本次 ReAct 执行的终态文本 AIMessage。"""
    if not messages:
        return None
    message = messages[-1]
    if not isinstance(message, AIMessage) or message.tool_calls:
        return None
    output = message.text.strip()
    return output or None


def _task_results_message(
    plan: TaskPlan,
    results: Sequence[TaskStepResult],
) -> SystemMessage:
    payload = [
        {
            "step_sequence": result.step_sequence,
            "description": step.description,
            "target_agent": result.target_agent.value,
            "success": result.success,
            "output": result.output,
            "error_code": (
                result.error_code.value if result.error_code is not None else None
            ),
        }
        for step, result in zip(plan.steps, results, strict=True)
    ]
    return SystemMessage(
        content=(
            f"{_TASK_RESULTS_MARKER}\n"
            f"{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
        ),
        name="task_results",
    )


def _deterministic_aggregation(
    plan: TaskPlan,
    results: Sequence[TaskStepResult],
) -> str:
    completed = [
        (
            f"#{result.step_sequence} "
            f"{plan.steps[result.step_sequence - 1].description}：{result.output}"
        )
        for result in results
        if result.success and result.output is not None
    ]
    sections = [
        "已完成部分：",
        "\n".join(completed) if completed else "无",
    ]
    failed = [result for result in results if not result.success]
    if failed:
        notices = [
            (
                f"#{result.step_sequence} "
                f"{plan.steps[result.step_sequence - 1].description}"
                f"（{result.error_code.value}）"
            )
            for result in failed
            if result.error_code is not None
        ]
        sections.extend(["", f"未完成子任务：{'；'.join(notices)}"])
    return "\n".join(sections)


def _replace_terminal_ai_output(
    messages: Sequence[BaseMessage],
    content: str,
) -> list[BaseMessage]:
    updated = list(messages)
    for index in range(len(updated) - 1, -1, -1):
        message = updated[index]
        if isinstance(message, AIMessage) and not message.tool_calls:
            # model_copy 仅替换 content，additional_kwargs 原样保留——
            # 因此 _wrap 注入的 agent 角色元数据在聚合改写内容后不丢失。
            updated[index] = message.model_copy(update={"content": content})
            return updated
    raise RuntimeError("aggregation completed without a terminal AIMessage")


def _append_missing_results_notice(
    messages: Sequence[BaseMessage],
    plan: TaskPlan,
    results: Sequence[TaskStepResult],
) -> list[BaseMessage]:
    failed = [result for result in results if not result.success]
    notices = [
        (
            f"#{result.step_sequence} "
            f"{plan.steps[result.step_sequence - 1].description}"
            f"（{result.error_code.value}）"
        )
        for result in failed
        if result.error_code is not None
    ]
    notice = f"未完成子任务：{'；'.join(notices)}"
    updated = list(messages)
    for index in range(len(updated) - 1, -1, -1):
        message = updated[index]
        if isinstance(message, AIMessage) and not message.tool_calls:
            answer = message.text.strip()
            content = (
                f"已完成部分：\n{answer}\n\n{notice}"
                if answer
                else f"已完成部分：无\n\n{notice}"
            )
            # 与 _replace_terminal_ai_output 同理：仅替换 content，
            # additional_kwargs（含 agent 角色元数据）原样保留。
            updated[index] = message.model_copy(update={"content": content})
            return updated
    raise RuntimeError("aggregation completed without a terminal AIMessage")


def _mark_agent_completion_invalid(
    events: Sequence[RunEvent],
    role: AgentRole,
) -> list[RunEvent]:
    updated = list(events)
    for index in range(len(updated) - 1, -1, -1):
        event = updated[index]
        if (
            event.event_type is EventType.AGENT_COMPLETED
            and event.agent == role.value
        ):
            updated[index] = event.model_copy(
                update={
                    "success": False,
                    "error_code": ErrorCode.AGENT_OUTPUT_INVALID,
                }
            )
            return updated
    raise RuntimeError("agent output validation requires a completion event")


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


__all__ = [
    "CollaborativeAgentGraph",
    "create_task_plan",
    "detect_intent",
    "detect_level",
    "handoff",
    "submit_evaluation",
]
