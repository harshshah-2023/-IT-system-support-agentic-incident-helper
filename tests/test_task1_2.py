import threading
import time

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.errors import GraphRecursionError

from incident_helper import tools as T
from incident_helper.agents.react import build_react_graph, thread_config
from incident_helper.extraction import CustomerTicket, extract_tickets, run_tool_loop, to_prompt
from incident_helper.llm_context import summarise_window, trim_for_model
from support import ScriptedChatModel, structured, tool_call


@pytest.fixture(autouse=True)
def _store(store):
    T.use_store(store)
    return store


TICKET = dict(urgency="high", sentiment="angry", summary="s", action_items=["a"], severity="SEV1")


def test_batch_extracts_each_message_and_respects_concurrency(store):
    inbox = store.inbox()
    assert len(inbox) == 5

    class Probe:
        """Records peak parallelism through the real Runnable.batch."""

        active = peak = 0
        lock = threading.Lock()

    from langchain_core.runnables import RunnableLambda

    def fake(_):
        with Probe.lock:
            Probe.active += 1
            Probe.peak = max(Probe.peak, Probe.active)
        time.sleep(0.03)
        with Probe.lock:
            Probe.active -= 1
        return CustomerTicket(**TICKET)

    tickets = extract_tickets(inbox, RunnableLambda(fake), max_concurrency=2)
    assert len(tickets) == 5
    assert Probe.peak == 2


def test_customer_pii_is_removed_before_the_llm_sees_a_message(store):
    angry = store.inbox()[4]
    text = to_prompt(angry)[1].content
    assert "98765" not in text and "priya.nair" not in text
    assert "[REDACTED_EMAIL]" in text and "[REDACTED_PHONE]" in text


def test_ticket_validation_rejects_values_outside_the_schema():
    with pytest.raises(ValueError):
        CustomerTicket(**{**TICKET, "urgency": "whenever"})


def test_manual_tool_loop_produces_all_message_types():
    model = ScriptedChatModel(
        script=[tool_call("check_service_status", service="payments-api"), AIMessage(content="done")]
    )
    messages = run_tool_loop("status?", model)
    assert [type(m) for m in messages] == [SystemMessage, HumanMessage, AIMessage, ToolMessage, AIMessage]
    assert "2.14.0" in messages[3].content


def test_tools_expose_docstring_and_types_to_the_llm():
    schema = T.check_service_status.tool_call_schema.model_json_schema()
    assert "service" in schema["properties"]
    assert "degraded_dependencies" in T.check_service_status.description


def two_hop_script():
    """Tool B's argument is only knowable from Tool A's output."""

    def second(messages):
        status = next(m for m in messages if isinstance(m, ToolMessage))
        assert "payments-api" in status.content
        return tool_call("get_recent_deployments", "call_2", service="payments-api")

    return [
        tool_call("check_service_status", "call_1", service="checkout-web"),
        second,
        AIMessage(content="Release 2.14.0."),
    ]


def test_two_hop_runs_within_one_invoke_under_recursion_limit_6():
    graph = build_react_graph(ScriptedChatModel(script=two_hop_script()))
    result = graph.invoke({"messages": [HumanMessage(content="Checkout is slow")]}, thread_config("a"))
    calls = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert [m.name for m in calls] == ["check_service_status", "get_recent_deployments"]
    assert "dep-7741" in calls[1].content


def test_threads_do_not_share_memory():
    model = ScriptedChatModel(
        script=[AIMessage(content="one"), AIMessage(content="two"), AIMessage(content="three")]
    )
    graph = build_react_graph(model)
    graph.invoke({"messages": [HumanMessage(content="payments thread")]}, thread_config("p"))
    graph.invoke({"messages": [HumanMessage(content="login thread")]}, thread_config("l"))
    graph.invoke({"messages": [HumanMessage(content="payments again")]}, thread_config("p"))
    last = " ".join(m.text for m in model.seen[-1])
    assert "payments thread" in last and "login thread" not in last
    assert len(graph.get_state(thread_config("l")).values["messages"]) == 2


def test_runaway_loop_stops_at_recursion_limit():
    looping = [tool_call("list_services", f"c{i}") for i in range(10)]
    graph = build_react_graph(ScriptedChatModel(script=looping))
    with pytest.raises(GraphRecursionError):
        graph.invoke({"messages": [HumanMessage(content="loop")]}, thread_config("x"))


def test_stream_modes_yield_different_structures():
    updates = list(
        build_react_graph(ScriptedChatModel(script=two_hop_script())).stream(
            {"messages": [HumanMessage(content="q")]}, thread_config("u"), stream_mode="updates"
        )
    )
    values = list(
        build_react_graph(ScriptedChatModel(script=two_hop_script())).stream(
            {"messages": [HumanMessage(content="q")]}, thread_config("v"), stream_mode="values"
        )
    )
    assert list(updates[0]) == ["agent"] and list(updates[1]) == ["tools"]
    assert list(values[0]) == ["messages"]
    assert [len(v["messages"]) for v in values] == [1, 2, 3, 4, 5, 6]


def test_model_sees_at_most_six_messages_but_checkpoint_keeps_all():
    model = ScriptedChatModel(script=[AIMessage(content=str(i)) for i in range(6)])
    graph = build_react_graph(model)
    for i in range(6):
        graph.invoke({"messages": [HumanMessage(content=f"q{i}")]}, thread_config("t"))
    assert len([m for m in model.seen[-1] if not isinstance(m, SystemMessage)]) == 6
    assert isinstance(model.seen[-1][0], SystemMessage)
    assert len(graph.get_state(thread_config("t")).values["messages"]) == 12


def test_trim_never_leaves_an_orphan_tool_message():
    call = tool_call("x", "c1")
    history = [
        HumanMessage(content="q"),
        call,
        ToolMessage(content="r", tool_call_id="c1"),
        *[AIMessage(content=str(i)) for i in range(5)],
    ]
    trimmed = trim_for_model(history, 6)
    assert not any(isinstance(m, ToolMessage) for m in trimmed)


def test_trim_keeps_context_when_no_human_message_in_window():
    messages = [
        HumanMessage(content="q"),
        *[
            m
            for i in range(4)
            for m in (tool_call("x", f"c{i}"), ToolMessage(content="r", tool_call_id=f"c{i}"))
        ],
    ]
    assert trim_for_model(messages, 6)


def test_time_travel_replays_from_earlier_checkpoint():
    graph = build_react_graph(ScriptedChatModel(script=[*two_hop_script(), AIMessage(content="replayed")]))
    config = thread_config("tt")
    graph.invoke({"messages": [HumanMessage(content="q")]}, config)
    history = list(graph.get_state_history(config))
    before_last_agent = next(s for s in history if s.next == ("agent",))
    replay = graph.invoke(None, {**config, "configurable": before_last_agent.config["configurable"]})
    assert replay["messages"][-1].content == "replayed"


def test_sliding_window_summary_folds_old_messages():
    history = [HumanMessage(content="word " * 2000, id=f"h{i}") for i in range(8)]
    summary, removals = summarise_window(
        history, ScriptedChatModel(script=[AIMessage(content="digest")]), trigger_tokens=100, keep_last=2
    )
    assert summary == "digest"
    assert len(removals) == 6


def test_summary_skipped_below_trigger():
    summary, removals = summarise_window([HumanMessage(content="hi", id="1")], ScriptedChatModel(), "old")
    assert (summary, removals) == ("old", [])


def test_structured_helper_shape():
    assert structured("X", a=1).tool_calls[0]["args"] == {"a": 1}
