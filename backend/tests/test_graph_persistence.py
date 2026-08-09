"""Conversation persistence tests for the collaborative graph."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver

from core.events import ErrorCode
from core.graph_builder import CollaborativeAgentGraph
from core.persistence import open_sqlite_checkpointer
from core.state import (
    HandoffApprovalAction,
    HandoffApprovalDecision,
    TaskContext,
    create_initial_state,
)


class ScriptedModel:
    """Return deterministic responses while recording model-visible history."""

    def __init__(self, responses: Sequence[AIMessage]) -> None:
        self.responses = list(responses)
        self.calls: list[list[BaseMessage]] = []

    def bind_tools(self, tools: Sequence[object]) -> ScriptedModel:
        return self

    def invoke(self, messages: list[BaseMessage]) -> AIMessage:
        self.calls.append(list(messages))
        return self.responses.pop(0)


class BlockingModel:
    """Block the first model call so concurrent graph entry is observable."""

    def __init__(self) -> None:
        self.first_entered = Event()
        self.second_entered = Event()
        self.release_first = Event()
        self._call_lock = Lock()
        self._call_count = 0

    def bind_tools(self, tools: Sequence[object]) -> BlockingModel:
        return self

    def invoke(self, messages: list[BaseMessage]) -> AIMessage:
        with self._call_lock:
            self._call_count += 1
            call_number = self._call_count
        if call_number == 1:
            self.first_entered.set()
            if not self.release_first.wait(timeout=2):
                raise TimeoutError("first model call was not released")
        else:
            self.second_entered.set()
        return AIMessage(content=f"answer {call_number}")


def _human_contents(messages: Sequence[BaseMessage]) -> list[str]:
    return [str(message.content) for message in messages if isinstance(message, HumanMessage)]


def _one_token_per_context_message(messages: Sequence[BaseMessage]) -> int:
    return sum(not isinstance(message, SystemMessage) for message in messages)


def _handoff_response(target: str = "teaching_assistant") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "handoff",
                "args": {"target": target},
                "id": "persisted-handoff",
                "type": "tool_call",
            }
        ],
    )


def test_checkpointer_continues_the_same_user_session() -> None:
    model = ScriptedModel([AIMessage(content="first answer"), AIMessage(content="second answer")])
    graph = CollaborativeAgentGraph(model=model, checkpointer=InMemorySaver())

    graph.run("first question", session_id="session-1", user_id="user-1")
    result = graph.run("second question", session_id="session-1", user_id="user-1")

    assert _human_contents(model.calls[1]) == ["first question", "second question"]
    assert _human_contents(result["messages"]) == ["first question", "second question"]


def test_token_trim_keeps_complete_checkpoint_history() -> None:
    model = ScriptedModel(
        [AIMessage(content="first answer"), AIMessage(content="second answer")]
    )
    graph = CollaborativeAgentGraph(
        model=model,
        checkpointer=InMemorySaver(),
        max_context_tokens=1,
        context_token_counter=_one_token_per_context_message,
    )

    graph.run("first question", session_id="token-session", user_id="user-1")
    result = graph.run(
        "second question",
        session_id="token-session",
        user_id="user-1",
    )

    visible_second_call = [
        message
        for message in model.calls[1]
        if not isinstance(message, SystemMessage)
    ]
    assert [
        (message.type, str(message.content)) for message in visible_second_call
    ] == [("human", "second question")]
    expected = [
        ("human", "first question"),
        ("ai", "first answer"),
        ("human", "second question"),
        ("ai", "second answer"),
    ]
    history = graph.get_history("token-session", user_id="user-1")
    assert [(message.type, str(message.content)) for message in history] == expected
    assert [
        (message.type, str(message.content)) for message in result["messages"]
    ] == expected


def test_get_state_and_history_read_the_latest_checkpoint() -> None:
    graph = CollaborativeAgentGraph(
        model=ScriptedModel([AIMessage(content="saved answer")]),
        checkpointer=InMemorySaver(),
    )
    result = graph.run("saved question", session_id="saved-session", user_id="user-1")

    state = graph.get_state("saved-session", user_id="user-1")
    history = graph.get_history("saved-session", user_id="user-1")

    assert state == result
    assert _human_contents(history) == ["saved question"]


def test_each_persisted_turn_resets_transient_run_state() -> None:
    model = ScriptedModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "handoff",
                        "args": {"target": "teaching_assistant"},
                        "id": "handoff-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="first handoff"),
            AIMessage(content="worker answer"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "handoff",
                        "args": {"target": "evaluator"},
                        "id": "handoff-2",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="second handoff"),
            AIMessage(content="fresh answer"),
        ]
    )
    graph = CollaborativeAgentGraph(
        model=model,
        max_handoffs=1,
        checkpointer=InMemorySaver(),
    )

    failed = graph.run("first turn", session_id="reset-session", user_id="user-1")
    recovered = graph.run("second turn", session_id="reset-session", user_id="user-1")

    assert failed["run_error"] is not None
    assert failed["run_error"].error_code is ErrorCode.GRAPH_HANDOFF_LIMIT
    assert failed["handoff_count"] == 1
    assert failed["agent_switch_count"] == 2
    assert recovered["run_error"] is None
    assert recovered["next_agent"] is None
    assert recovered["handoff_count"] == 0
    assert recovered["agent_switch_count"] == 0
    assert recovered["events"][: len(failed["events"])] == failed["events"]
    assert [event.sequence for event in recovered["events"]] == list(
        range(len(recovered["events"]))
    )


def test_each_persisted_turn_has_a_distinct_run_id_and_tags_its_events() -> None:
    graph = CollaborativeAgentGraph(
        model=ScriptedModel(
            [AIMessage(content="first answer"), AIMessage(content="second answer")]
        ),
        checkpointer=InMemorySaver(),
    )

    first = graph.run("first turn", session_id="run-session", user_id="user-1")
    first_run_id = first["run_id"]
    first_event_count = len(first["events"])
    second = graph.run("second turn", session_id="run-session", user_id="user-1")
    second_run_id = second["run_id"]

    assert isinstance(first_run_id, str) and first_run_id
    assert isinstance(second_run_id, str) and second_run_id
    assert first_run_id != second_run_id
    assert {event.run_id for event in second["events"][:first_event_count]} == {
        first_run_id
    }
    assert {event.run_id for event in second["events"][first_event_count:]} == {
        second_run_id
    }


def test_new_run_state_carries_the_session_workspace_capability(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    shared = tmp_path / "shared"
    primary.mkdir()
    shared.mkdir()
    graph = CollaborativeAgentGraph(
        model=ScriptedModel([AIMessage(content="answer")]),
    )

    result = graph.run(
        "question",
        session_id="workspace-session",
        user_id="user-1",
        workspace_root=str(primary),
        additional_workspace_roots=[str(shared)],
    )

    assert result["workspace_root"] == str(primary)
    assert result["additional_workspace_roots"] == [str(shared)]


def test_new_turn_preserves_persistent_task_fields() -> None:
    graph = CollaborativeAgentGraph(
        model=ScriptedModel(
            [AIMessage(content="first answer"), AIMessage(content="second answer")]
        ),
        checkpointer=InMemorySaver(),
    )
    session_id = "persistent-fields"
    user_id = "user-1"

    graph.run("first question", session_id=session_id, user_id=user_id)
    task_context = TaskContext(intent="teach")
    graph.build().update_state(
        graph._thread_config(session_id, user_id),
        {"task_context": task_context, "extra": {"course": "ml"}},
    )

    result = graph.run("second question", session_id=session_id, user_id=user_id)

    assert result["task_context"] == task_context
    assert result["extra"]["course"] == "ml"


def test_persisted_runs_are_serialized_per_graph_instance() -> None:
    model = BlockingModel()
    graph = CollaborativeAgentGraph(model=model, checkpointer=InMemorySaver())

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(graph.run, "first question", "shared-session", "user-1")
        try:
            assert model.first_entered.wait(timeout=1)
            second = executor.submit(
                graph.run,
                "second question",
                "shared-session",
                "user-1",
            )
            assert model.second_entered.wait(timeout=0.1) is False
        finally:
            model.release_first.set()

        first_result = first.result(timeout=2)
        second_result = second.result(timeout=2)

    assert first_result["messages"][-1].content == "answer 1"
    assert second_result["messages"][-1].content == "answer 2"


def test_checkpointer_isolates_different_sessions() -> None:
    model = ScriptedModel([AIMessage(content="answer one"), AIMessage(content="answer two")])
    graph = CollaborativeAgentGraph(model=model, checkpointer=InMemorySaver())

    graph.run("session one", session_id="session-1", user_id="user-1")
    result = graph.run("session two", session_id="session-2", user_id="user-1")

    assert _human_contents(model.calls[1]) == ["session two"]
    assert _human_contents(result["messages"]) == ["session two"]


def test_checkpointer_isolates_users_with_the_same_session_id() -> None:
    model = ScriptedModel([AIMessage(content="answer one"), AIMessage(content="answer two")])
    graph = CollaborativeAgentGraph(model=model, checkpointer=InMemorySaver())

    graph.run("user one", session_id="shared-session", user_id="user-1")
    result = graph.run("user two", session_id="shared-session", user_id="user-2")

    assert _human_contents(model.calls[1]) == ["user two"]
    assert _human_contents(result["messages"]) == ["user two"]


def test_missing_checkpoint_has_no_state_or_history() -> None:
    graph = CollaborativeAgentGraph(model=ScriptedModel([]), checkpointer=InMemorySaver())

    assert graph.get_state("missing", user_id="user-1") is None
    assert graph.get_history("missing", user_id="user-1") == []


def test_pending_checkpoint_must_be_resumed_without_appending_input() -> None:
    graph = CollaborativeAgentGraph(
        model=ScriptedModel([AIMessage(content="resumed answer")]),
        checkpointer=InMemorySaver(),
    )
    app = graph.build()
    config = graph._thread_config("pending-session", "user-1")
    initial_state = create_initial_state(
        session_id="pending-session",
        user_id="user-1",
    )
    initial_state["messages"] = [HumanMessage(content="pending question")]
    app.update_state(config, initial_state, as_node="__start__")

    assert app.get_state(config).next == ("supervisor",)
    with pytest.raises(RuntimeError, match="resume"):
        graph.run("must not be appended", "pending-session", "user-1")
    assert _human_contents(app.get_state(config).values["messages"]) == [
        "pending question"
    ]
    ordinary_pending_decision = HandoffApprovalDecision(
        interrupt_id="not-a-handoff-interrupt",
        action=HandoffApprovalAction.CONFIRM,
    )
    with pytest.raises(ValueError, match="人工.*断点"):
        graph.resume_handoff(
            "pending-session",
            ordinary_pending_decision,
            user_id="user-1",
        )
    assert graph.get_pending_handoff(
        "pending-session",
        user_id="user-1",
    ) is None

    result = graph.resume("pending-session", user_id="user-1")

    assert result["messages"][-1].content == "resumed answer"
    assert app.get_state(config).next == ()


@pytest.mark.parametrize("checkpoint_kind", ["missing", "completed"])
def test_resume_requires_a_pending_checkpoint(checkpoint_kind: str) -> None:
    responses = (
        [AIMessage(content="completed answer")]
        if checkpoint_kind == "completed"
        else []
    )
    graph = CollaborativeAgentGraph(
        model=ScriptedModel(responses),
        checkpointer=InMemorySaver(),
    )
    if checkpoint_kind == "completed":
        graph.run("completed question", session_id=checkpoint_kind, user_id="user-1")

    with pytest.raises(ValueError, match="待恢复"):
        graph.resume(checkpoint_kind, user_id="user-1")


@pytest.mark.parametrize("method_name", ["get_state", "get_history", "resume"])
def test_persistence_reads_require_a_checkpointer(method_name: str) -> None:
    graph = CollaborativeAgentGraph(model=ScriptedModel([]))

    with pytest.raises(ValueError, match="checkpointer"):
        getattr(graph, method_name)("session-1", user_id="user-1")


def test_handoff_resume_requires_a_checkpointer() -> None:
    graph = CollaborativeAgentGraph(model=ScriptedModel([]))
    decision = HandoffApprovalDecision(
        interrupt_id="interrupt-1",
        action=HandoffApprovalAction.CONFIRM,
    )

    with pytest.raises(ValueError, match="checkpointer"):
        graph.resume_handoff("session-1", decision, user_id="user-1")
    with pytest.raises(ValueError, match="checkpointer"):
        graph.get_pending_handoff("session-1", user_id="user-1")


def test_handoff_interrupt_uses_separate_resume_semantics() -> None:
    model = ScriptedModel(
        [
            _handoff_response(),
            AIMessage(content="分派提案已生成"),
            AIMessage(content="教学结果"),
            AIMessage(content="最终汇总"),
        ]
    )
    graph = CollaborativeAgentGraph(
        model=model,
        checkpointer=InMemorySaver(),
        interrupt_before_handoff=True,
    )
    session_id = "separate-resume"
    user_id = "user-1"
    graph.run("原始任务", session_id, user_id)
    paused_state = graph.get_state(session_id, user_id=user_id)
    assert paused_state is not None
    paused_messages = list(paused_state["messages"])
    pending = graph.get_pending_handoff(session_id, user_id=user_id)
    assert pending is not None

    with pytest.raises(ValueError, match="resume_handoff"):
        graph.resume(session_id, user_id=user_id)
    with pytest.raises(RuntimeError, match="resume_handoff"):
        graph.run("不得追加", session_id, user_id)

    unchanged = graph.get_state(session_id, user_id=user_id)
    assert unchanged is not None
    assert graph.get_pending_handoff(session_id, user_id=user_id) == pending
    assert unchanged["messages"] == paused_messages
    assert len(model.calls) == 2

    decision = HandoffApprovalDecision(
        interrupt_id=pending.interrupt_id,
        action=HandoffApprovalAction.CONFIRM,
    )
    result = graph.resume_handoff(session_id, decision, user_id=user_id)

    assert result["messages"][-1].content == "最终汇总"
    assert len(model.calls) == 4


def test_stale_handoff_decision_does_not_consume_interrupt() -> None:
    model = ScriptedModel(
        [_handoff_response(), AIMessage(content="分派提案已生成")]
    )
    graph = CollaborativeAgentGraph(
        model=model,
        checkpointer=InMemorySaver(),
        interrupt_before_handoff=True,
    )
    session_id = "stale-decision"
    user_id = "user-1"
    graph.run("原始任务", session_id, user_id)
    pending = graph.get_pending_handoff(session_id, user_id=user_id)
    assert pending is not None
    stale = HandoffApprovalDecision(
        interrupt_id="stale-interrupt-id",
        action=HandoffApprovalAction.REJECT,
    )

    with pytest.raises(ValueError, match="interrupt_id"):
        graph.resume_handoff(session_id, stale, user_id=user_id)

    unchanged = graph.get_pending_handoff(session_id, user_id=user_id)
    assert unchanged == pending
    assert len(model.calls) == 2

    current = HandoffApprovalDecision(
        interrupt_id=pending.interrupt_id,
        action=HandoffApprovalAction.REJECT,
    )
    result = graph.resume_handoff(session_id, current, user_id=user_id)
    assert result["pending_handoff"] is None


def test_sqlite_checkpointer_restores_after_graph_and_connection_reopen(
    tmp_path: Path,
) -> None:
    checkpoint_path = tmp_path / "nested" / "checkpoints.sqlite"
    first_model = ScriptedModel([AIMessage(content="first answer")])

    with open_sqlite_checkpointer(checkpoint_path) as first_saver:
        first_graph = CollaborativeAgentGraph(model=first_model, checkpointer=first_saver)
        first_graph.run("first question", session_id="session-1", user_id="user-1")

    assert checkpoint_path.is_file()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        first_saver.conn.execute("SELECT 1")

    second_model = ScriptedModel([AIMessage(content="second answer")])
    with open_sqlite_checkpointer(checkpoint_path) as second_saver:
        second_graph = CollaborativeAgentGraph(model=second_model, checkpointer=second_saver)
        result = second_graph.run(
            "second question",
            session_id="session-1",
            user_id="user-1",
        )

    assert _human_contents(second_model.calls[0]) == ["first question", "second question"]
    assert _human_contents(result["messages"]) == ["first question", "second question"]


def test_sqlite_handoff_interrupt_resumes_after_graph_reopen(
    tmp_path: Path,
) -> None:
    checkpoint_path = tmp_path / "hitl" / "checkpoints.sqlite"
    session_id = "persisted-approval"
    user_id = "user-1"
    first_model = ScriptedModel(
        [_handoff_response(), AIMessage(content="分派提案已生成")]
    )

    with open_sqlite_checkpointer(checkpoint_path) as first_saver:
        first_graph = CollaborativeAgentGraph(
            model=first_model,
            checkpointer=first_saver,
            interrupt_before_handoff=True,
        )
        first_graph.run("持久化任务", session_id, user_id)
        assert first_graph.get_pending_handoff(
            session_id,
            user_id=user_id,
        ) is not None

    second_model = ScriptedModel(
        [AIMessage(content="教学结果"), AIMessage(content="最终汇总")]
    )
    with open_sqlite_checkpointer(checkpoint_path) as second_saver:
        second_graph = CollaborativeAgentGraph(
            model=second_model,
            checkpointer=second_saver,
            interrupt_before_handoff=True,
        )
        restored = second_graph.get_pending_handoff(
            session_id,
            user_id=user_id,
        )
        assert restored is not None
        decision = HandoffApprovalDecision(
            interrupt_id=restored.interrupt_id,
            action=HandoffApprovalAction.CONFIRM,
        )

        result = second_graph.resume_handoff(
            session_id,
            decision,
            user_id=user_id,
        )
        completed_pending = second_graph.get_pending_handoff(
            session_id,
            user_id=user_id,
        )

    assert len(first_model.calls) == 2
    assert len(second_model.calls) == 2
    assert "助教" in str(second_model.calls[0][0].content)
    assert result["pending_handoff"] is None
    assert result["messages"][-1].content == "最终汇总"
    assert result["next_agent"] is None
    assert completed_pending is None


@pytest.mark.parametrize("user_id", ["", "   "])
@pytest.mark.parametrize(
    "method_name",
    ["run", "resume", "get_state", "get_history", "get_pending_handoff"],
)
def test_graph_rejects_empty_user_ids(user_id: str, method_name: str) -> None:
    graph = CollaborativeAgentGraph(model=ScriptedModel([]), checkpointer=InMemorySaver())

    with pytest.raises(ValueError, match="user_id"):
        if method_name == "run":
            graph.run("question", session_id="session-1", user_id=user_id)
        else:
            getattr(graph, method_name)("session-1", user_id=user_id)


def test_thread_id_uses_explicit_anonymous_and_value_user_keys() -> None:
    anonymous = CollaborativeAgentGraph._thread_config("session-1", None)
    identified = CollaborativeAgentGraph._thread_config("session-1", " user-1 ")

    assert anonymous["configurable"]["thread_id"] == "user:none|session:9:session-1"
    assert identified["configurable"]["thread_id"] == (
        "user:value:8: user-1 |session:9:session-1"
    )


@pytest.mark.parametrize("session_id", ["", "   "])
@pytest.mark.parametrize(
    "method_name",
    ["run", "resume", "get_state", "get_history", "get_pending_handoff"],
)
def test_graph_rejects_empty_session_ids(session_id: str, method_name: str) -> None:
    graph = CollaborativeAgentGraph(model=ScriptedModel([]), checkpointer=InMemorySaver())

    with pytest.raises(ValueError, match="session_id"):
        if method_name == "run":
            graph.run("question", session_id=session_id, user_id="user-1")
        else:
            getattr(graph, method_name)(session_id, user_id="user-1")
