"""Context-window management: message-count trimming, token counting, sliding-window summaries."""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
    trim_messages,
)
from langchain_core.messages.utils import count_tokens_approximately

MESSAGE_CAP = 6


def count_tokens(messages: Sequence[AnyMessage]) -> int:
    return count_tokens_approximately(list(messages))


def _drop_orphans(messages: list[AnyMessage]) -> list[AnyMessage]:
    """Providers reject histories with a ToolMessage whose AIMessage was trimmed, and the reverse."""
    announced = {call["id"] for m in messages if isinstance(m, AIMessage) for call in m.tool_calls}
    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    kept: list[AnyMessage] = []
    for message in messages:
        if isinstance(message, ToolMessage) and message.tool_call_id not in announced:
            continue
        unanswered = isinstance(message, AIMessage) and any(
            c["id"] not in answered for c in message.tool_calls
        )
        # Only a pending request at the very end of history may be unanswered.
        if unanswered and message is not messages[-1]:
            continue
        kept.append(message)
    return kept


def trim_for_model(messages: Sequence[AnyMessage], max_messages: int = MESSAGE_CAP) -> list[AnyMessage]:
    """Cap non-system context at the last `max_messages`, keeping the system prompt.

    `token_counter=len` makes `trim_messages` count messages rather than tokens. `start_on` is
    deliberately omitted because a long ReAct loop can hold no human message in its final six; that
    case would otherwise empty the context. Pairing is repaired by `_drop_orphans` instead.
    """
    system = [m for m in messages if isinstance(m, SystemMessage)]
    body = [m for m in messages if not isinstance(m, SystemMessage)]
    trimmed = trim_messages(body, max_tokens=max_messages, token_counter=len, strategy="last")
    return [*system[:1], *_drop_orphans(list(trimmed))]


def summarise_window(
    messages: Sequence[AnyMessage],
    model: BaseChatModel,
    existing_summary: str = "",
    keep_last: int = MESSAGE_CAP,
    trigger_tokens: int = 2000,
) -> tuple[str, list[RemoveMessage]]:
    """Sliding-window summarisation: fold the oldest messages into a running summary.

    Returns the new summary and `RemoveMessage` deltas for `add_messages`. No-ops below the trigger,
    so ordinary turns cost no extra model call.
    """
    if count_tokens(messages) < trigger_tokens or len(messages) <= keep_last:
        return existing_summary, []
    cut = len(messages) - keep_last
    while cut > 0 and isinstance(messages[cut], ToolMessage):
        cut -= 1  # never split a tool call from its result
    older = messages[:cut]
    if not older:
        return existing_summary, []
    transcript = "\n".join(f"{m.type}: {m.text}" for m in older if m.text)
    prompt = (
        "Maintain a running summary of an IT incident investigation. Keep services, versions, "
        "metrics, decisions and open questions. Be terse.\n\n"
        f"Current summary:\n{existing_summary or '(none)'}\n\nNew messages:\n{transcript}"
    )
    summary = model.invoke([HumanMessage(content=prompt)]).text
    return summary, [RemoveMessage(id=m.id) for m in older if m.id]
