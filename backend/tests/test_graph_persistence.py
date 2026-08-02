"""Conversation persistence tests for the collaborative graph."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from core.events import ErrorCode
from core.graph_builder import CollaborativeAgentGraph
from core.persistence import open_sqlite_checkpointer
from core.state import TaskContext, create_initial_state


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


def test_checkpointer_continues_the_same_user_session() -> None:
    model = ScriptedModel([AIMessage(content="first answer"), AIMessage(content="second answer")])
    graph = CollaborativeAgentGraph(model=model, checkpointer=InMemorySaver())

    graph.run("first question", session_id="session-1", user_id="user-1")
    result = graph.run("second question", session_id="session-1", user_id="user-1")

    assert _human_contents(model.calls[1]) == ["first question", "second question"]
    assert _human_contents(result["messages"]) == ["first question", "second question"]


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


@pytest.mark.parametrize("user_id", ["", "   "])
@pytest.mark.parametrize("method_name", ["run", "resume", "get_state", "get_history"])
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
@pytest.mark.parametrize("method_name", ["run", "resume", "get_state", "get_history"])
def test_graph_rejects_empty_session_ids(session_id: str, method_name: str) -> None:
    graph = CollaborativeAgentGraph(model=ScriptedModel([]), checkpointer=InMemorySaver())

    with pytest.raises(ValueError, match="session_id"):
        if method_name == "run":
            graph.run("question", session_id=session_id, user_id="user-1")
        else:
            getattr(graph, method_name)(session_id, user_id="user-1")
