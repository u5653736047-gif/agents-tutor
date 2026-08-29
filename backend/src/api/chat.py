"""Synchronous chat REST route backed by the collaborative graph."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Iterable
from datetime import datetime
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Request, status
from langchain_core.messages import AIMessage, BaseMessage
from starlette.concurrency import run_in_threadpool

from api.attachments import compose_message_with_attachments
from api.files import attachments_for_generated_files
from api.schemas import (
    AgentRole,
    ApiErrorCode,
    ChatRequest,
    ChatResponse,
    Citation,
    ErrorResponse,
    GradingItemDto,
    GradingResultDto,
    HandoffRequest,
    Message,
    MessageRole,
    PendingHandoff,
    PendingToolApproval,
    RunError,
    RunEvent,
    StreamEventType,
    TaskPlan,
    TaskPlanStatus,
    TaskPlanStep,
    TaskResult,
    ToolApprovalRequest,
    WorkerAgentRole,
    WorkflowProgress,
    WorkflowStatus,
    WorkflowStep,
    WorkflowStepStatus,
)
from api.schemas import (
    ErrorCode as ApiRunErrorCode,
)
from api.sessions import current_user_id
from core.events import EventType
from core.events import RunError as CoreRunError
from core.events import RunEvent as CoreRunEvent
from core.graph_builder import CollaborativeAgentGraph
from core.sessions import SessionRecord, SessionStore, derive_session_title
from core.state import AgentState, PendingHandoffApproval
from core.state import GradingResult as CoreGradingResult
from core.state import PendingToolApproval as CorePendingToolApproval
from core.state import TaskPlan as CoreTaskPlan
from core.state import TaskStepResult as CoreTaskStepResult
from core.state import WorkflowState as CoreWorkflowState
from core.state import message_grading as core_message_grading
from core.state import message_references as core_message_references

router = APIRouter(tags=["chat"])
CHAT_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_400_BAD_REQUEST: {"model": ErrorResponse},
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse},
}
EVENT_TYPE_MAP = {
    EventType.AGENT_STARTED: StreamEventType.THINKING,
    EventType.AGENT_REASONING: StreamEventType.REASONING,
    EventType.TOOL_STARTED: StreamEventType.TOOL_CALL,
    EventType.TOOL_COMPLETED: StreamEventType.TOOL_RESULT,
    EventType.TOOL_OUTPUT: StreamEventType.TOOL_OUTPUT,
    EventType.AGENT_COMPLETED: StreamEventType.MESSAGE_END,
    EventType.AGENT_SWITCHED: StreamEventType.AGENT_SWITCH,
    EventType.RUN_FAILED: StreamEventType.ERROR,
    EventType.RUN_COMPLETED: StreamEventType.DONE,
}
PENDING_RESUME_ERROR_PREFIX = "存在待恢复执行，请先调用 "


def _session_store(request: Request) -> SessionStore:
    return cast(SessionStore, request.app.state.session_store)


def _graph(request: Request) -> CollaborativeAgentGraph:
    return cast(CollaborativeAgentGraph, request.app.state.graph)


def session_lock(
    request: Request, session_id: str, user_id: str | None
) -> asyncio.Lock:
    locks = cast(
        dict[tuple[str | None, str], asyncio.Lock], request.app.state.chat_session_locks
    )
    key = (user_id, session_id)
    lock = locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        locks[key] = lock
    return lock


def _public_agent(value: object) -> AgentRole | None:
    if not isinstance(value, str):
        return None
    try:
        return AgentRole(value)
    except ValueError:
        return None


def _public_task_plan(plan: object) -> TaskPlan | None:
    """core TaskPlan → 公开契约 TaskPlan（字段一一对应；整体类型不符 → None）。

    core 与 API 的 TaskPlan / TaskPlanStep 字段同名同义，这里按字段
    逐项映射；core 的 WorkerAgentRole 是 AgentRole 的 Literal 别名、
    API 侧是独立枚举（值字符串一致），target_agent 显式按值转换，
    status 同理（core StrEnum 与 api Enum 值一致）。注意：字段级非法
    值（如未知枚举）会抛 ValueError——core 模型 extra="forbid" + 类型
    校验保证 CoreTaskPlan 实例字段必然合法，isinstance 入口已挡掉
    脏 dict，故不做字段级降级（与 _public_event 的策略一致）。
    """
    if not isinstance(plan, CoreTaskPlan):
        return None
    return TaskPlan(
        steps=[
            TaskPlanStep(
                sequence=step.sequence,
                description=step.description,
                target_agent=WorkerAgentRole(step.target_agent.value),
            )
            for step in plan.steps
        ],
        current_step_index=plan.current_step_index,
        status=TaskPlanStatus(plan.status.value),  # core StrEnum 与 api Enum 值一致
    )


def _public_task_results(results: object) -> list[TaskResult] | None:
    """core TaskStepResult 列表 → 公开契约 TaskResult 列表（缺失/类型不符 → None）。

    逐项 isinstance 防御：列表中出现非 core TaskStepResult 的脏项时
    跳过而不是整体失败；全部跳过（或空列表）时归一化为 None，与
    「无结果就不携带」的契约一致。error_code 与 target_agent 按值
    转换到 API 侧枚举（core/API 枚举值字符串一致，见 _public_event）。
    """
    if not isinstance(results, list):
        return None
    public: list[TaskResult] = []
    for item in results:
        if not isinstance(item, CoreTaskStepResult):
            continue
        public.append(
            TaskResult(
                step_sequence=item.step_sequence,
                target_agent=WorkerAgentRole(item.target_agent.value),
                success=item.success,
                output=item.output,
                error_code=(
                    ApiRunErrorCode(item.error_code.value)
                    if item.error_code is not None
                    else None
                ),
            )
        )
    return public or None


def _public_workflow(workflow: object) -> WorkflowProgress | None:
    """core WorkflowState → 公开契约 WorkflowProgress（类型不符 → None）。

    投影边界（lesson-workflow-design §七）：不携带 artifact_root 绝对
    路径与 budget_used 内部计数，artifacts 保持注册时登记的相对路径；
    其余字段与 core 同名同义、按值转换枚举（core StrEnum 与 api Enum
    值一致，与 _public_task_plan 同一策略，不做字段级降级）。
    """
    if not isinstance(workflow, CoreWorkflowState):
        return None
    return WorkflowProgress(
        workflow_id=workflow.workflow_id,
        status=WorkflowStatus(workflow.status.value),
        steps=[
            WorkflowStep(
                step_id=step.step_id,
                worker_role=WorkerAgentRole(step.worker_role.value),
                status=WorkflowStepStatus(step.status.value),
                attempts=step.attempts,
                summary=step.summary,
            )
            for step in workflow.steps
        ],
        current_step_index=workflow.current_step_index,
        artifacts=list(workflow.artifacts),
        error_code=(
            ApiRunErrorCode(workflow.error_code.value)
            if workflow.error_code is not None
            else None
        ),
    )


def _safe_created_at(message: BaseMessage) -> datetime | None:
    created_at = getattr(message, "created_at", None)
    return created_at if isinstance(created_at, datetime) else None


def _is_answer_message(message: BaseMessage) -> bool:
    """是否为一条「作答消息」：助手输出、无工具调用、纯文本内容。

    最终响应 message 与 references 都按这个判定找消息，保证两者
    指向同一轮作答（引用必须与回答内容对应）。
    """
    return (
        isinstance(message, AIMessage)
        and not message.tool_calls
        and isinstance(message.content, str)
    )


def _final_assistant_message(
    state: AgentState,
    previous_message_count: int,
    user_id: str | None = None,
) -> Message | None:
    agent = _public_agent(state.get("current_agent"))
    messages = state.get("messages", [])
    for message in reversed(messages[previous_message_count:]):
        if not _is_answer_message(message):
            continue
        content = message.content
        if not isinstance(content, str):
            # 防御性收窄：_is_answer_message 内部的 isinstance 判断不会
            # 跨函数传播类型收窄，这里显式重复一次，让 mypy 确认 content
            # 是纯文本（运行时必然成立，与 api/sessions._public_message
            # 「非纯文本内容不对外暴露」的公开口径保持一致）。
            continue
        return Message(
            role=MessageRole.ASSISTANT,
            content=content,
            agent=agent,
            created_at=_safe_created_at(message),
            # T5-3：officecli_edit 生成的文件注册为可下载附件（无则 None）。
            attachments=attachments_for_generated_files(user_id, message),
            # P2-12：该消息若挂着批改元数据则随消息透出（无则 None）。
            grading=_message_grading_dto(message),
        )
    return None


def _api_citations(message: BaseMessage) -> list[Citation] | None:
    """把 core 消息元数据里的引用转成 API 契约的 Citation 列表。

    core 与 API 的 Citation 字段同名同义（document_id / source /
    page / chunk_id），core 侧已做过逻辑来源与字段校验，这里按
    model_dump 结果逐项 validate 直接透传，不需要字段映射。core
    返回空列表（元数据有 references 键但内容不可解析的脏数据）时
    归一化为 None——与「无引用就不携带」的契约一致。
    """
    citations = core_message_references(message)
    if not citations:
        return None
    return [
        Citation.model_validate(citation.model_dump(mode="json"))
        for citation in citations
    ]


def _response_references(
    state: AgentState, previous_message_count: int
) -> list[Citation] | None:
    """本轮响应要携带的引用列表（口径与 S2-T4 的「按作答消息渲染」一致）。

    验收要求「最终回答携带 references 元数据且引用来自真实检索」，
    而检索证据挂在 worker 的作答消息上、supervisor 的聚合回答本身
    不带引用（S2-T4 语义：引用跟随使用证据作答的消息）。采用两级口径：

    1. 优先取本轮最新作答消息（与响应 message 同一条消息）自身的
       引用——严格按消息，引用与回答内容一一对应；
    2. 若最新作答消息无引用（典型场景：supervisor 聚合回答），回退
       扫描本轮更早的作答消息（从新到旧），取最近一条带引用的——
       聚合回答的内容正是对这些 worker 检索作答的汇总，最近一次
       检索的引用与回答内容仍然对应，同时保证验收场景引用可见；
    3. 本轮没有任何作答消息（如 run_error 提前终止）或均无引用 →
       None，与 core「无引用就不携带」的零命中语义一致。

    只扫描本轮新增消息（previous_message_count 之后），历史轮次的
    引用不跨轮次渲染。
    """
    messages = state.get("messages", [])
    new_messages = messages[previous_message_count:]
    final_message: BaseMessage | None = None
    for message in reversed(new_messages):
        if _is_answer_message(message):
            final_message = message
            break
    if final_message is None:
        return None
    references = _api_citations(final_message)
    if references is not None:
        return references
    for message in reversed(new_messages):
        if _is_answer_message(message):
            references = _api_citations(message)
            if references is not None:
                return references
    return None


def _public_grading(grading: object) -> GradingResultDto | None:
    """core GradingResult → 公开契约 GradingResultDto（字段逐项映射）。

    与 _public_task_plan 同一哲学：整体类型不符 → None（宽容读取，
    checkpoint 反序列化后的脏 dict 不击穿响应）；core 模型 extra=
    "forbid" + 校验保证实例字段必然合法，故不做字段级降级。
    """
    if not isinstance(grading, CoreGradingResult):
        return None
    return GradingResultDto(
        items=[
            GradingItemDto(
                question_id=item.question_id,
                score=item.score,
                max_score=item.max_score,
                feedback=item.feedback,
                knowledge_point=item.knowledge_point,
                error_tag=item.error_tag,
            )
            for item in grading.items
        ],
        overall_comment=grading.overall_comment,
        total_score=grading.total_score,
        max_total_score=grading.max_total_score,
    )


def _message_grading_dto(message: BaseMessage) -> GradingResultDto | None:
    """消息元数据里的批改结论 → 契约 DTO（历史回放用，pi 审查 🟡4）。"""
    return _public_grading(core_message_grading(message))


def _previous_sequence(state: AgentState | None) -> int:
    if state is None:
        return -1
    return max(
        (
            event.sequence
            for event in state.get("events", [])
            if isinstance(event, CoreRunEvent)
        ),
        default=-1,
    )


def _previous_message_count(state: AgentState | None) -> int:
    return 0 if state is None else len(state.get("messages", []))


def _public_event(event: CoreRunEvent) -> RunEvent | None:
    event_type = EVENT_TYPE_MAP.get(event.event_type)
    if event_type is None:
        return None
    error_code = None
    if event.error_code is not None:
        error_code = ApiRunErrorCode(event.error_code.value)
    return RunEvent(
        event_type=event_type,
        sequence=event.sequence,
        session_id=event.session_id,
        run_id=event.run_id,
        agent=_public_agent(event.agent),
        tool_name=event.tool_name,
        tool_call_id=event.tool_call_id,
        parent_tool_call_id=event.parent_tool_call_id,
        input_summary=event.input_summary,
        output_summary=event.output_summary,
        content=event.content,
        output_stream=event.output_stream,
        message_id=event.message_id,
        is_delta=(False if event.event_type is EventType.AGENT_REASONING else None),
        success=event.success,
        duration_ms=event.duration_ms,
        error_code=error_code,
        plan_step_sequence=event.plan_step_sequence,
        degraded=event.degraded,
    )


def _public_events(events: Iterable[object], sequence: int) -> list[RunEvent]:
    return [
        public_event
        for event in events
        if isinstance(event, CoreRunEvent)
        and event.sequence > sequence
        and (public_event := _public_event(event)) is not None
    ]


def _public_run_error(error: object) -> RunError | None:
    if not isinstance(error, CoreRunError):
        return None
    return RunError(
        error_code=ApiRunErrorCode(error.error_code.value),
        message="The request could not be completed.",
        agent=_public_agent(error.agent),
    )


def _public_pending_handoff(pending: object) -> PendingHandoff | None:
    if not isinstance(pending, PendingHandoffApproval):
        return None
    return PendingHandoff(
        interrupt_id=pending.interrupt_id,
        request=HandoffRequest(
            target_agent=WorkerAgentRole(pending.request.target_agent.value),
            task_content=pending.request.task_content,
            plan_step_sequence=pending.request.plan_step_sequence,
        ),
    )


def _ensure_session(session_store: SessionStore, session_id: str, user_id: str | None) -> None:
    if any(
        record.session_id == session_id
        for record in session_store.list_sessions(user_id=user_id, include_archived=True)
    ):
        return
    try:
        session_store.create_session(session_id, user_id=user_id)
    except ValueError:
        if not any(
            record.session_id == session_id
            for record in session_store.list_sessions(user_id=user_id, include_archived=True)
        ):
            raise


def _ensure_session_with_title(
    session_store: SessionStore,
    session_id: str,
    user_id: str | None,
    message: str,
    touch_activity: bool = True,
) -> SessionRecord:
    """Ensure the session exists, then title it from its first user message.

    侧栏列表不再只显示 session_id:标题取首条用户消息的压缩截断,
    且只写一次(set_title_if_absent)——后续消息/断线重连回放不会
    覆盖;存量老会话在下次发消息时按同规则补标题。
    """
    _ensure_session(session_store, session_id, user_id)
    title = derive_session_title(message)
    if title is not None:
        session_store.set_title_if_absent(session_id, title, user_id=user_id)
    if touch_activity:
        session_store.touch_session(session_id, user_id=user_id)
    record = session_store.get_session(session_id, user_id=user_id)
    if record is None:
        raise RuntimeError("session disappeared after creation")
    return record


def _workspace_call_kwargs(
    method: object,
    session: SessionRecord,
) -> dict[str, Any]:
    """Pass workspace capability only to graph implementations that support it.

    返回 dict[str, Any] 而非 dict[str, object]：调用方以 ** 解包传给
    run/stream（参数类型为 str | None / Sequence[str]），object 值与
    这些形参不兼容会被 mypy 拒绝；Any 是「动态透传」的正确口径
    （值在运行时由本函数按参数名精选，类型安全由 SessionRecord 保证）。
    """
    if not callable(method):
        return {}
    parameters = inspect.signature(method).parameters
    kwargs: dict[str, Any] = {}
    if "workspace_root" in parameters:
        kwargs["workspace_root"] = session.workspace_root
    if "additional_workspace_roots" in parameters:
        kwargs["additional_workspace_roots"] = session.additional_workspace_roots
    # S5-C1 决策 2：会话绑定的知识空间经图入口写入 extra（未绑定 =
    # "public"，单路 public 过滤检索）。按方法签名门控传递：测试替身
    # 的 run/stream 未声明该参数时不传（零回归）。
    # P1-2 显性化兜底：空串/空白与 None 同归 public，避免 `or` 的隐式
    # 真值语义掩盖空串边界；与 tools.py `knowledge_scope.get() or "public"`
    # 互为冗余防御但此处显式分支更可审计。
    if "knowledge_namespace" in parameters:
        raw_ns = session.knowledge_namespace
        if isinstance(raw_ns, str):
            stripped = raw_ns.strip()
            kwargs["knowledge_namespace"] = stripped if stripped else "public"
        else:
            kwargs["knowledge_namespace"] = raw_ns if raw_ns else "public"
    return kwargs


def _run_graph_turn(
    graph: CollaborativeAgentGraph,
    message: str,
    session_id: str,
    user_id: str | None,
    session: SessionRecord,
) -> AgentState:
    return graph.run(
        message,
        session_id,
        user_id,
        **_workspace_call_kwargs(graph.run, session),
    )


def _public_pending_tool_approval(
    pending: object,
) -> PendingToolApproval | None:
    if not isinstance(pending, CorePendingToolApproval):
        return None
    return PendingToolApproval(
        interrupt_id=pending.interrupt_id,
        request=ToolApprovalRequest(
            tool_call_id=pending.request.tool_call_id,
            tool_name=pending.request.tool_name,
            agent_role=AgentRole(pending.request.agent_role.value),
            arguments=pending.request.arguments,
        ),
    )


async def pending_handoff_for_session(
    graph: CollaborativeAgentGraph, session_id: str, user_id: str | None
) -> PendingHandoff | None:
    pending_method = getattr(graph, "get_pending_handoff", None)
    if not callable(pending_method):
        return None
    pending = await run_in_threadpool(pending_method, session_id, user_id)
    return _public_pending_handoff(pending)


async def pending_tool_approval_for_session(
    graph: CollaborativeAgentGraph, session_id: str, user_id: str | None
) -> PendingToolApproval | None:
    pending_method = getattr(graph, "get_pending_tool_approval", None)
    if not callable(pending_method):
        return None
    pending = await run_in_threadpool(
        pending_method,
        session_id,
        user_id,
    )
    return _public_pending_tool_approval(pending)


def session_busy_response(session_id: str, message: str) -> ChatResponse:
    return ChatResponse(
        session_id=session_id,
        run_error=RunError(
            error_code=ApiErrorCode.SESSION_BUSY,
            message=message,
        ),
    )


async def chat_response_for_state(
    graph: CollaborativeAgentGraph,
    state: AgentState,
    session_id: str,
    user_id: str | None,
    previous_state: AgentState | None,
) -> ChatResponse:
    """Convert one completed graph transition into the public chat contract."""
    previous_count = _previous_message_count(previous_state)
    return ChatResponse(
        session_id=session_id,
        run_id=state.get("run_id"),
        message=_final_assistant_message(state, previous_count, user_id),
        references=_response_references(state, previous_count),
        # P2-12：本轮批改结论（grading 通道每轮重置；历史轮经消息
        # 元数据恢复，见 Message.grading）。
        grading=_public_grading(state.get("grading")),
        task_plan=_public_task_plan(state.get("task_plan")),
        task_results=_public_task_results(state.get("task_results")),
        workflow=_public_workflow(state.get("workflow")),
        events=_public_events(state.get("events", []), _previous_sequence(previous_state)),
        run_error=_public_run_error(state.get("run_error")),
        pending_handoff=await pending_handoff_for_session(graph, session_id, user_id),
        pending_tool_approval=await pending_tool_approval_for_session(
            graph,
            session_id,
            user_id,
        ),
        current_agent=_public_agent(state.get("current_agent")),
    )


@router.post("/chat", response_model=ChatResponse, responses=CHAT_ERROR_RESPONSES)
async def chat(
    payload: ChatRequest,
    request: Request,
    user_id: Annotated[str | None, Depends(current_user_id)],
) -> ChatResponse:
    """Run one synchronous collaboration turn in a worker thread."""
    graph = _graph(request)
    session_store = _session_store(request)
    active_session_lock = session_lock(request, payload.session_id, user_id)
    if active_session_lock.locked():
        return session_busy_response(
            payload.session_id,
            "Another request is already running for this session.",
        )

    async with active_session_lock:
        session = await run_in_threadpool(
            _ensure_session_with_title,
            session_store,
            payload.session_id,
            user_id,
            payload.message,
        )
        # P2-7：消费 attachments 契约字段（此前路由忽略）——附件提取
        # 文本拼入本轮用户消息；无附件时与原消息逐字节一致（零回归）。
        # 会话标题仍用原消息（简洁），附件材料只进模型上下文。
        # 提取含磁盘 IO / PDF 全页解析 / OCR CPU 推理，必须走线程池
        # （审查 C1：同步执行会阻塞事件循环，殃及全部并发请求），
        # 与下方 _ensure_session_with_title / get_state 同一模式。
        message_text = await run_in_threadpool(
            compose_message_with_attachments,
            payload.message,
            payload.attachments,
            user_id,
            getattr(request.app.state, "ocr_provider", None),
            getattr(request.app.state, "vision_provider", None),
        )
        previous_state = await run_in_threadpool(
            graph.get_state, payload.session_id, user_id
        )
        try:
            state = await run_in_threadpool(
                _run_graph_turn,
                graph,
                message_text,
                payload.session_id,
                user_id,
                session,
            )
        except RuntimeError as error:
            pending_handoff = await pending_handoff_for_session(
                graph, payload.session_id, user_id
            )
            pending_tool_approval = await pending_tool_approval_for_session(
                graph,
                payload.session_id,
                user_id,
            )
            if (
                pending_handoff is not None
                or pending_tool_approval is not None
                or str(error).startswith(
                    PENDING_RESUME_ERROR_PREFIX
                )
            ):
                return ChatResponse(
                    session_id=payload.session_id,
                    run_error=RunError(
                        error_code=ApiErrorCode.SESSION_BUSY,
                        message="The session is waiting for a pending operation.",
                    ),
                    pending_handoff=pending_handoff,
                    pending_tool_approval=pending_tool_approval,
                )
            return ChatResponse(
                session_id=payload.session_id,
                run_error=RunError(
                    error_code=ApiErrorCode.INTERNAL_ERROR,
                    message="The request could not be completed.",
                ),
            )
        except Exception:  # noqa: BLE001 - graph boundary exposes only stable error data
            return ChatResponse(
                session_id=payload.session_id,
                run_error=RunError(
                    error_code=ApiErrorCode.INTERNAL_ERROR,
                    message="The request could not be completed.",
                ),
            )

        return await chat_response_for_state(
            graph,
            state,
            payload.session_id,
            user_id,
            previous_state,
        )
