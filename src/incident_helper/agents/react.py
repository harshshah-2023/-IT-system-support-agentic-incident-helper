"""Task 2: the cyclic ReAct graph (Reason -> Act -> Observe) with persistence and trimming."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AnyMessage, SystemMessage
from langchain_core.runnables import RunnableConfig, RunnableLambda
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition

from incident_helper.llm_context import MESSAGE_CAP, trim_for_model
from incident_helper.tools import READ_TOOLS

RECURSION_LIMIT = 6

SYSTEM_PROMPT = """You are an on-call SRE assistant for Larkspur Commerce.
Investigate with tools before you conclude. Follow up on `degraded_dependencies`, then check releases.
Name the evidence behind each claim. Say what you could not determine. Be concise."""


class AgentState(TypedDict):
    """`add_messages` is the reducer: node output is merged into history by id, not overwritten."""

    messages: Annotated[list[AnyMessage], add_messages]


def make_agent_node(
    model: BaseChatModel,
    tools: Sequence[BaseTool],
    system_prompt: str = SYSTEM_PROMPT,
    max_messages: int = MESSAGE_CAP,
) -> RunnableLambda:
    """The LLM node. Trimming applies to this model call only; the full history stays in the checkpoint.

    Sync and async bodies are both provided so that `astream_events` receives tokens as they arrive.
    """
    bound = model.bind_tools(list(tools))

    def _prompt(state: dict[str, Any]) -> list[AnyMessage]:
        system = system_prompt
        if state.get("summary"):
            system += f"\n\nEarlier in this investigation:\n{state['summary']}"
        return trim_for_model([SystemMessage(content=system), *state["messages"]], max_messages)

    def call(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
        return {"messages": [bound.invoke(_prompt(state), config)]}

    async def acall(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
        return {"messages": [await bound.ainvoke(_prompt(state), config)]}

    return RunnableLambda(call, acall, name="agent")


def build_react_graph(
    model: BaseChatModel,
    tools: Sequence[BaseTool] = READ_TOOLS,
    checkpointer: BaseCheckpointSaver | None = None,
):
    builder = StateGraph(AgentState)
    builder.add_node("agent", make_agent_node(model, tools))
    builder.add_node("tools", ToolNode(list(tools), handle_tool_errors=True))
    builder.add_edge(START, "agent")
    # `tools_condition` returns "tools" when the last AIMessage has tool_calls, otherwise END.
    builder.add_conditional_edges("agent", tools_condition, {"tools": "tools", END: END})
    builder.add_edge("tools", "agent")
    return builder.compile(checkpointer=checkpointer or MemorySaver())


def thread_config(thread_id: str, recursion_limit: int = RECURSION_LIMIT) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id}, "recursion_limit": recursion_limit}
