"""同步聊天 REST API 测试。"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from langchain_core.messages import AIMessage, HumanMessage

from api.app import create_app
from core.events import ErrorCode, EventType, RunError, RunEvent
from core.knowledge.models import Citation as CoreCitation
from core.sessions import SessionStore
from core.state import (
    REFERENCES_METADATA_KEY,
    AgentRole,
    HandoffApprovalRequest,
    PendingHandoffApproval,
    TaskPlan,
    TaskPlanStatus,
    TaskPlanStep,
    TaskStepResult,
)


class ChatGraph:
    """为 API 测试提供可控的同步图替身。"""

    def __init__(
        self,
        state: dict[str, Any],
        *,
        previous_state: dict[str, Any] | None = None,
        pending_handoff: PendingHandoffApproval | None = None,
        run_exception: Exception | None = None,
    ) -> None:
        self._state = state
        self._previous_state = previous_state
        self._pending_handoff = pending_handoff
        self._run_exception = run_exception
        self.run_thread_id: int | None = None
        self.run_inputs: list[tuple[str, str, str | None]] = []

    def get_state(self, session_id: str, user_id: str | None = None) -> dict[str, Any] | None:
        return self._previous_state

    def run(self, user_input: str, session_id: str, user_id: str | None = None) -> dict[str, Any]:
        self.run_thread_id = threading.get_ident()
        self.run_inputs.append((user_input, session_id, user_id))
        if self._run_exception is not None:
            raise self._run_exception
        return self._state

    def get_pending_handoff(
        self, session_id: str, user_id: str | None = None
    ) -> PendingHandoffApproval | None:
        return self._pending_handoff


class BlockingChatGraph(ChatGraph):
    """A graph substitute that keeps one run active for concurrency tests."""

    def __init__(self) -> None:
        super().__init__(
            {
                "messages": [AIMessage(content="complete")],
                "events": [],
                "current_agent": "supervisor",
                "run_error": None,
                "pending_handoff": None,
            }
        )
        self.run_started = threading.Event()
        self.release_run = threading.Event()

    def run(self, user_input: str, session_id: str, user_id: str | None = None) -> dict[str, Any]:
        self.run_started.set()
        if not self.release_run.wait(timeout=2):
            raise TimeoutError("test run was not released")
        return super().run(user_input, session_id, user_id)


class WorkspaceAwareChatGraph(ChatGraph):
    """Capture the workspace capability forwarded by the HTTP session layer."""

    def __init__(self, state: dict[str, Any]) -> None:
        super().__init__(state)
        self.workspace_calls: list[tuple[str | None, tuple[str, ...]]] = []

    def run(
        self,
        user_input: str,
        session_id: str,
        user_id: str | None = None,
        *,
        workspace_root: str | None = None,
        additional_workspace_roots: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        self.workspace_calls.append((workspace_root, tuple(additional_workspace_roots)))
        return super().run(user_input, session_id, user_id)


def _chat_app(tmp_path: Path, graph: ChatGraph) -> tuple[FastAPI, SessionStore]:
    app = create_app()
    store = SessionStore(tmp_path / "sessions.sqlite3")
    app.state.session_store = store
    app.state.graph = graph
    return app, store


async def _post_chat(app: FastAPI, body: dict[str, str]) -> Response:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post("/chat", headers={"X-User-Id": "user-1"}, json=body)


def test_chat_creates_missing_session_runs_in_worker_and_returns_event_delta(tmp_path: Path) -> None:
    previous_event = RunEvent(
        event_type=EventType.AGENT_STARTED,
        sequence=0,
        session_id="session-1",
        agent="supervisor",
    )
    result_events = [
        previous_event,
        RunEvent(
            event_type=EventType.AGENT_COMPLETED,
            sequence=1,
            session_id="session-1",
            agent="evaluator",
            success=True,
        ),
        RunEvent(
            event_type=EventType.RUN_COMPLETED,
            sequence=2,
            session_id="session-1",
            agent=None,
            success=True,
        ),
    ]
    graph = ChatGraph(
        {
            "messages": [HumanMessage(content="请评估"), AIMessage(content="评估完成")],
            "events": result_events,
            "current_agent": "evaluator",
            "run_error": None,
            "pending_handoff": None,
        },
        previous_state={"events": [previous_event]},
    )
    app, store = _chat_app(tmp_path, graph)
    caller_thread = threading.get_ident()
    try:
        response = asyncio.run(_post_chat(app, {"session_id": "session-1", "message": "请评估"}))
        sessions = store.list_sessions(user_id="user-1")
    finally:
        store.close()

    assert response.status_code == 200
    assert graph.run_thread_id is not None
    assert graph.run_thread_id != caller_thread
    assert graph.run_inputs == [("请评估", "session-1", "user-1")]
    assert [event["sequence"] for event in response.json()["events"]] == [1, 2]
    assert [event["event_type"] for event in response.json()["events"]] == [
        "message_end",
        "done",
    ]
    assert response.json()["message"] == {
        "role": "assistant",
        "content": "评估完成",
        "agent": "evaluator",
        "created_at": None,
        "attachments": None,
    }
    assert response.json()["current_agent"] == "evaluator"
    assert [session.session_id for session in sessions] == ["session-1"]


def test_chat_runs_with_the_workspace_authorized_for_its_session(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    shared = tmp_path / "shared"
    primary.mkdir()
    shared.mkdir()
    graph = WorkspaceAwareChatGraph(
        {
            "messages": [HumanMessage(content="分析"), AIMessage(content="完成")],
            "events": [],
            "current_agent": "supervisor",
            "run_error": None,
            "pending_handoff": None,
        }
    )
    app, store = _chat_app(tmp_path, graph)
    store.create_session(
        "session-1",
        user_id="user-1",
        workspace_root=primary,
    )
    store.add_workspace_root("session-1", shared, user_id="user-1")
    try:
        response = asyncio.run(
            _post_chat(app, {"session_id": "session-1", "message": "分析"})
        )
    finally:
        store.close()

    assert response.status_code == 200
    assert graph.workspace_calls == [
        (str(primary.resolve()), (str(shared.resolve()),))
    ]


def test_chat_returns_run_errors_as_a_successful_contract_response(tmp_path: Path) -> None:
    previous_messages = [
        HumanMessage(content="earlier request"),
        AIMessage(content="earlier answer"),
    ]
    graph = ChatGraph(
        {
            "messages": previous_messages,
            "events": [
                RunEvent(
                    event_type=EventType.RUN_FAILED,
                    sequence=0,
                    session_id="session-1",
                    agent="supervisor",
                    success=False,
                    error_code=ErrorCode.MODEL_CALL_FAILED,
                )
            ],
            "current_agent": "supervisor",
            "run_error": RunError(
                error_code=ErrorCode.MODEL_CALL_FAILED,
                message="internal secret details",
                agent="supervisor",
            ),
            "pending_handoff": None,
        },
        previous_state={"messages": previous_messages},
    )
    app, store = _chat_app(tmp_path, graph)
    try:
        response = asyncio.run(_post_chat(app, {"session_id": "session-1", "message": "请评估"}))
    finally:
        store.close()

    assert response.status_code == 200
    assert response.json()["message"] is None
    assert response.json()["run_error"] == {
        "error_code": "model_call_failed",
        "message": "The request could not be completed.",
        "agent": "supervisor",
    }
    assert response.json()["events"][0]["event_type"] == "error"
    assert "internal secret details" not in response.text
    assert "earlier answer" not in response.text


def test_chat_returns_pending_handoff_when_the_graph_pauses(tmp_path: Path) -> None:
    pending_handoff = PendingHandoffApproval(
        interrupt_id="interrupt-1",
        request=HandoffApprovalRequest(
            target_agent=AgentRole.TEACHING_ASSISTANT,
            task_content="检查课程设计",
            plan_step_sequence=1,
        ),
    )
    graph = ChatGraph(
        {
            "messages": [HumanMessage(content="请检查")],
            "events": [],
            "current_agent": "supervisor",
            "run_error": None,
            "pending_handoff": {"ignored": "use public accessor"},
        },
        pending_handoff=pending_handoff,
    )
    app, store = _chat_app(tmp_path, graph)
    try:
        response = asyncio.run(_post_chat(app, {"session_id": "session-1", "message": "请检查"}))
    finally:
        store.close()

    assert response.status_code == 200
    assert response.json()["pending_handoff"] == {
        "interrupt_id": "interrupt-1",
        "request": {
            "target_agent": "teaching_assistant",
            "task_content": "检查课程设计",
            "plan_step_sequence": 1,
        },
    }


def test_chat_reports_a_pending_session_without_a_500(tmp_path: Path) -> None:
    graph = ChatGraph(
        {},
        run_exception=RuntimeError("存在待恢复执行，请先调用 resume_handoff()"),
    )
    app, store = _chat_app(tmp_path, graph)
    try:
        response = asyncio.run(_post_chat(app, {"session_id": "session-1", "message": "请继续"}))
    finally:
        store.close()

    assert response.status_code == 200
    assert response.json()["run_error"] == {
        "error_code": "session_busy",
        "message": "The session is waiting for a pending operation.",
        "agent": None,
    }


def test_chat_rejects_a_concurrent_request_for_the_same_session(tmp_path: Path) -> None:
    graph = BlockingChatGraph()
    app, store = _chat_app(tmp_path, graph)
    transport = ASGITransport(app=app)

    async def send_concurrent_requests() -> tuple[Response, Response]:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            first_task = asyncio.create_task(
                client.post(
                    "/chat",
                    headers={"X-User-Id": "user-1"},
                    json={"session_id": "session-1", "message": "first"},
                )
            )
            await asyncio.wait_for(asyncio.to_thread(graph.run_started.wait), timeout=1)
            second_task = asyncio.create_task(
                client.post(
                    "/chat",
                    headers={"X-User-Id": "user-1"},
                    json={"session_id": "session-1", "message": "second"},
                )
            )
            try:
                await asyncio.sleep(0.05)
                assert second_task.done()
                second_response = second_task.result()
            finally:
                graph.release_run.set()
                first_response = await first_task
                if not second_task.done():
                    await second_task
            return first_response, second_response

    try:
        first_response, second_response = asyncio.run(send_concurrent_requests())
    finally:
        store.close()

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert second_response.json()["run_error"] == {
        "error_code": "session_busy",
        "message": "Another request is already running for this session.",
        "agent": None,
    }
    assert second_response.json()["events"] == []
    assert graph.run_inputs == [("first", "session-1", "user-1")]


def test_chat_does_not_misclassify_an_unexpected_runtime_error(tmp_path: Path) -> None:
    graph = ChatGraph({}, run_exception=RuntimeError("internal invariant failed"))
    app, store = _chat_app(tmp_path, graph)
    try:
        response = asyncio.run(_post_chat(app, {"session_id": "session-1", "message": "go"}))
    finally:
        store.close()

    assert response.status_code == 200
    assert response.json()["run_error"] == {
        "error_code": "internal_error",
        "message": "The request could not be completed.",
        "agent": None,
    }
    assert "internal invariant failed" not in response.text


def test_invalid_chat_request_does_not_echo_message_content(tmp_path: Path) -> None:
    graph = ChatGraph({})
    app, store = _chat_app(tmp_path, graph)
    transport = ASGITransport(app=app)

    async def send_invalid_request() -> Response:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(
                "/chat",
                headers={"X-User-Id": "user-1"},
                json={"session_id": "session-1", "message": ""},
            )

    try:
        response = asyncio.run(send_invalid_request())
    finally:
        store.close()

    assert response.status_code == 422
    assert response.json() == {
        "detail": {"error_code": "invalid_request", "message": "Request is invalid."}
    }
    assert '"input"' not in response.text


@pytest.mark.parametrize(
    "body",
    [
        {"session_id": " ", "message": "valid message"},
        {"session_id": "session-1", "message": " "},
    ],
)
def test_whitespace_chat_fields_are_rejected_before_graph_execution(
    tmp_path: Path, body: dict[str, str]
) -> None:
    graph = ChatGraph({})
    app, store = _chat_app(tmp_path, graph)
    transport = ASGITransport(app=app)

    async def send_invalid_request() -> Response:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(
                "/chat",
                headers={"X-User-Id": "user-1"},
                json=body,
            )

    try:
        response = asyncio.run(send_invalid_request())
    finally:
        store.close()

    assert response.status_code == 422
    assert response.json() == {
        "detail": {"error_code": "invalid_request", "message": "Request is invalid."}
    }
    assert graph.run_inputs == []


# ── ChatResponse.references（T2 冒烟收尾：引用契约生效） ──────────────


def _cited_answer(content: str, *, page: int | None = None) -> AIMessage:
    """构造挂载 references 元数据的助手消息，模拟真实检索挂载。

    元数据格式与 core 的 _attach_references 一致：REFERENCES_METADATA_KEY
    指向 core Citation 的 model_dump(mode="json") dict 列表。
    """
    citation = CoreCitation(
        document_id="algebra",
        source="algebra.txt",
        page=page,
        chunk_id="chunk-algebra-1",
    )
    return AIMessage(
        content=content,
        additional_kwargs={REFERENCES_METADATA_KEY: [citation.model_dump(mode="json")]},
    )


def test_chat_fills_references_from_the_final_message_metadata(tmp_path: Path) -> None:
    """最终消息自带引用 → 响应 references 直接透传（core/API 字段同名）。"""
    first = CoreCitation(
        document_id="algebra", source="algebra.txt", page=3, chunk_id="chunk-a"
    )
    second = CoreCitation(
        document_id="stats", source="stats.txt", page=None, chunk_id="chunk-b"
    )
    answer = AIMessage(
        content="检索作答",
        additional_kwargs={
            REFERENCES_METADATA_KEY: [
                first.model_dump(mode="json"),
                second.model_dump(mode="json"),
            ]
        },
    )
    graph = ChatGraph(
        {
            "messages": [HumanMessage(content="请检索"), answer],
            "events": [],
            "current_agent": "learning_assistant",
            "run_error": None,
            "pending_handoff": None,
        },
    )
    app, store = _chat_app(tmp_path, graph)
    try:
        response = asyncio.run(_post_chat(app, {"session_id": "session-1", "message": "请检索"}))
    finally:
        store.close()

    assert response.status_code == 200
    assert response.json()["references"] == [
        {
            "document_id": "algebra",
            "source": "algebra.txt",
            "page": 3,
            "chunk_id": "chunk-a",
        },
        {
            "document_id": "stats",
            "source": "stats.txt",
            "page": None,
            "chunk_id": "chunk-b",
        },
    ]


def test_chat_references_are_none_when_the_final_message_has_none(tmp_path: Path) -> None:
    """最终消息无引用元数据 → 响应 references 为 None（无引用不携带）。"""
    graph = ChatGraph(
        {
            "messages": [HumanMessage(content="普通问题"), AIMessage(content="普通回答")],
            "events": [],
            "current_agent": "supervisor",
            "run_error": None,
            "pending_handoff": None,
        },
    )
    app, store = _chat_app(tmp_path, graph)
    try:
        response = asyncio.run(_post_chat(app, {"session_id": "session-1", "message": "普通问题"}))
    finally:
        store.close()

    assert response.status_code == 200
    assert response.json()["references"] is None


def test_chat_references_are_none_when_metadata_is_all_invalid(tmp_path: Path) -> None:
    """references 键存在但内容全非法 → core 归一化为空列表 → API 归一化为 None。

    脏数据防御链路：message_references 逐项跳过非法引用（缺必填字段
    chunk_id），列表无任何可解析项时返回空列表；_api_citations 再把
    空列表归一化为 None——与「无引用就不携带」的契约一致。
    """
    graph = ChatGraph(
        {
            "messages": [
                HumanMessage(content="请检索"),
                AIMessage(
                    content="检索作答",
                    additional_kwargs={
                        REFERENCES_METADATA_KEY: [
                            {"document_id": "algebra", "source": "algebra.txt"},
                        ]
                    },
                ),
            ],
            "events": [],
            "current_agent": "learning_assistant",
            "run_error": None,
            "pending_handoff": None,
        },
    )
    app, store = _chat_app(tmp_path, graph)
    try:
        response = asyncio.run(_post_chat(app, {"session_id": "session-1", "message": "请检索"}))
    finally:
        store.close()

    assert response.status_code == 200
    assert response.json()["references"] is None


def test_chat_falls_back_to_the_most_recent_cited_message_for_aggregated_answers(
    tmp_path: Path,
) -> None:
    """supervisor 聚合回答不带引用 → 回退取本轮最近的带引用 worker 作答。

    对应真实冒烟链路：learning_assistant 检索作答（带引用）→ supervisor
    汇总（不带引用）。响应 message 是汇总，references 回退到检索作答。
    """
    worker_answer = _cited_answer("检索作答")
    summary = AIMessage(content="汇总：检索作答")
    graph = ChatGraph(
        {
            "messages": [HumanMessage(content="请检索"), worker_answer, summary],
            "events": [],
            "current_agent": "supervisor",
            "run_error": None,
            "pending_handoff": None,
        },
    )
    app, store = _chat_app(tmp_path, graph)
    try:
        response = asyncio.run(_post_chat(app, {"session_id": "session-1", "message": "请检索"}))
    finally:
        store.close()

    assert response.status_code == 200
    assert response.json()["message"]["content"] == "汇总：检索作答"
    assert response.json()["references"] == [
        {
            "document_id": "algebra",
            "source": "algebra.txt",
            "page": None,
            "chunk_id": "chunk-algebra-1",
        }
    ]


def test_chat_does_not_leak_references_from_previous_rounds(tmp_path: Path) -> None:
    """引用只取本轮新增消息，历史轮次的引用不跨轮次渲染。"""
    previous_messages = [HumanMessage(content="旧问题"), _cited_answer("旧回答")]
    graph = ChatGraph(
        {
            "messages": [
                *previous_messages,
                HumanMessage(content="新问题"),
                AIMessage(content="新回答"),
            ],
            "events": [],
            "current_agent": "supervisor",
            "run_error": None,
            "pending_handoff": None,
        },
        previous_state={"messages": previous_messages},
    )
    app, store = _chat_app(tmp_path, graph)
    try:
        response = asyncio.run(_post_chat(app, {"session_id": "session-1", "message": "新问题"}))
    finally:
        store.close()

    assert response.status_code == 200
    assert response.json()["message"]["content"] == "新回答"
    assert response.json()["references"] is None


def test_chat_run_error_responses_carry_no_references(tmp_path: Path) -> None:
    """本轮没有作答消息（run_error 提前终止）→ references 为 None。"""
    graph = ChatGraph(
        {
            "messages": [HumanMessage(content="请评估")],
            "events": [
                RunEvent(
                    event_type=EventType.RUN_FAILED,
                    sequence=0,
                    session_id="session-1",
                    agent="supervisor",
                    success=False,
                    error_code=ErrorCode.MODEL_CALL_FAILED,
                )
            ],
            "current_agent": "supervisor",
            "run_error": RunError(
                error_code=ErrorCode.MODEL_CALL_FAILED,
                message="internal secret details",
                agent="supervisor",
            ),
            "pending_handoff": None,
        },
    )
    app, store = _chat_app(tmp_path, graph)
    try:
        response = asyncio.run(_post_chat(app, {"session_id": "session-1", "message": "请评估"}))
    finally:
        store.close()

    assert response.status_code == 200
    assert response.json()["message"] is None
    assert response.json()["references"] is None


# ── ChatResponse.task_plan / task_results（D2-T1：任务计划契约生效） ──────


def test_chat_response_includes_task_plan_and_results(tmp_path: Path) -> None:
    """final_state 带 core TaskPlan/TaskStepResult → 响应透传契约字段。

    core 校验约束：COMPLETED 计划必须 current_step_index == len(steps)，
    失败结果只带 error_code（本地可恢复错误码之一）。
    """
    plan = TaskPlan(
        steps=[
            TaskPlanStep(
                sequence=1,
                description="检查课程设计",
                target_agent=AgentRole.TEACHING_ASSISTANT,
            ),
            TaskPlanStep(
                sequence=2,
                description="制定学习规划",
                target_agent=AgentRole.LEARNING_ASSISTANT,
            ),
        ],
        current_step_index=2,
        status=TaskPlanStatus.COMPLETED,
    )
    results = [
        TaskStepResult(
            step_sequence=1,
            target_agent=AgentRole.TEACHING_ASSISTANT,
            success=True,
            output="课程设计检查完成",
        ),
        TaskStepResult(
            step_sequence=2,
            target_agent=AgentRole.LEARNING_ASSISTANT,
            success=False,
            error_code=ErrorCode.MODEL_CALL_FAILED,
        ),
    ]
    graph = ChatGraph(
        {
            "messages": [HumanMessage(content="分解任务"), AIMessage(content="完成")],
            "events": [],
            "current_agent": "supervisor",
            "run_error": None,
            "pending_handoff": None,
            "task_plan": plan,
            "task_results": results,
        },
    )
    app, store = _chat_app(tmp_path, graph)
    try:
        response = asyncio.run(_post_chat(app, {"session_id": "session-1", "message": "分解任务"}))
    finally:
        store.close()

    assert response.status_code == 200
    assert response.json()["task_plan"] == {
        "steps": [
            {
                "sequence": 1,
                "description": "检查课程设计",
                "target_agent": "teaching_assistant",
            },
            {
                "sequence": 2,
                "description": "制定学习规划",
                "target_agent": "learning_assistant",
            },
        ],
        "current_step_index": 2,
        "status": "completed",
    }
    assert response.json()["task_results"] == [
        {
            "step_sequence": 1,
            "target_agent": "teaching_assistant",
            "success": True,
            "output": "课程设计检查完成",
            "error_code": None,
        },
        {
            "step_sequence": 2,
            "target_agent": "learning_assistant",
            "success": False,
            "output": None,
            "error_code": "model_call_failed",
        },
    ]


def test_chat_response_task_plan_and_results_none_when_missing(tmp_path: Path) -> None:
    """final_state 不带 task_plan、task_results 为空列表 → 响应两者为 None。

    空列表按契约归一化为 None（_public_task_results 无有效项时返回
    None），与「无结果就不携带」的语义一致。
    """
    graph = ChatGraph(
        {
            "messages": [HumanMessage(content="普通问题"), AIMessage(content="普通回答")],
            "events": [],
            "current_agent": "supervisor",
            "run_error": None,
            "pending_handoff": None,
            "task_results": [],
        },
    )
    app, store = _chat_app(tmp_path, graph)
    try:
        response = asyncio.run(_post_chat(app, {"session_id": "session-1", "message": "普通问题"}))
    finally:
        store.close()

    assert response.status_code == 200
    assert response.json()["task_plan"] is None
    assert response.json()["task_results"] is None


def test_chat_response_task_plan_degrades_to_none_on_wrong_types(tmp_path: Path) -> None:
    """task_plan 类型不符、task_results 含非 core 项 → 防御性降级为 None。

    脏数据防御：task_plan 传字符串整体降级为 None；task_results 列表里
    的 dict 项逐项跳过，无有效项时整体为 None。
    """
    graph = ChatGraph(
        {
            "messages": [HumanMessage(content="请分解"), AIMessage(content="完成")],
            "events": [],
            "current_agent": "supervisor",
            "run_error": None,
            "pending_handoff": None,
            "task_plan": "not-a-plan",
            "task_results": [{"step_sequence": 1, "success": True}],
        },
    )
    app, store = _chat_app(tmp_path, graph)
    try:
        response = asyncio.run(_post_chat(app, {"session_id": "session-1", "message": "请分解"}))
    finally:
        store.close()

    assert response.status_code == 200
    assert response.json()["task_plan"] is None
    assert response.json()["task_results"] is None


def test_chat_titles_session_from_first_message_only(tmp_path: Path) -> None:
    """UX-20260808#1:首条用户消息提炼为会话标题,且只写一次。

    侧栏列表不再只显示 session_id:标题 = 消息压缩空白后截断;
    同会话后续消息不得覆盖首个标题(set_title_if_absent)。
    """
    graph = ChatGraph(
        {
            "messages": [AIMessage(content="完成")],
            "events": [],
            "current_agent": "supervisor",
            "run_error": None,
            "pending_handoff": None,
        }
    )
    app, store = _chat_app(tmp_path, graph)
    try:
        first = asyncio.run(
            _post_chat(
                app,
                {"session_id": "session-1", "message": "  什么是\n  注意力机制?  "},
            )
        )
        first_activity = store.list_sessions(user_id="user-1")[0].updated_at
        second = asyncio.run(
            _post_chat(app, {"session_id": "session-1", "message": "后续消息不改标题"})
        )
        records = store.list_sessions(user_id="user-1")
    finally:
        store.close()

    assert first.status_code == 200
    assert second.status_code == 200
    assert [record.title for record in records] == ["什么是 注意力机制?"]
    assert records[0].updated_at > first_activity
