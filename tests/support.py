"""Test doubles. These never ship in `src/`: production code has no scripted-LLM path."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable
from pydantic import Field

Script = list[AIMessage | Exception | Callable[[list[BaseMessage]], AIMessage]]


class ScriptedChatModel(BaseChatModel):
    """Replays a scripted conversation and records what it was shown."""

    script: list[Any] = Field(default_factory=list)
    seen: list[list[BaseMessage]] = Field(default_factory=list)
    bound: list[Any] = Field(default_factory=list)
    model_label: str = "scripted"

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Runnable:
        self.bound.append(tools)
        return self

    def _next(self, messages: list[BaseMessage]) -> AIMessage:
        self.seen.append(list(messages))
        if not self.script:
            raise AssertionError("ScriptedChatModel ran out of replies")
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        reply = step(messages) if callable(step) else step
        return reply.model_copy(update={"usage_metadata": reply.usage_metadata or usage(10, 5)})

    def _generate(
        self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs: Any
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._next(messages))])

    def _stream(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs: Any):
        reply = self._next(messages)
        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content=reply.content,
                tool_call_chunks=[
                    {
                        "name": c["name"],
                        "args": __import__("json").dumps(c["args"]),
                        "id": c["id"],
                        "index": i,
                        "type": "tool_call_chunk",
                    }
                    for i, c in enumerate(reply.tool_calls)
                ],
                usage_metadata=reply.usage_metadata,
            )
        )


def usage(input_tokens: int, output_tokens: int) -> dict[str, int]:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


def tool_call(name: str, call_id: str = "call_1", **args: Any) -> AIMessage:
    return AIMessage(
        content="", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}]
    )


def structured(schema_name: str, call_id: str = "call_s", **args: Any) -> AIMessage:
    """A reply as produced under `with_structured_output` (forced tool call)."""
    return tool_call(schema_name, call_id, **args)
