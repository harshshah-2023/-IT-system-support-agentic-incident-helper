"""The LLM gateway: one entry point for model selection, fallback, caching and telemetry.

`ModelGateway.ask()` is the full request pipeline for a plain prompt. `ModelGateway.chat_model()` returns
a `GatewayChatModel`, a real `BaseChatModel`, so LangGraph agents, `bind_tools` and
`with_structured_output` all run through the same fallback and telemetry code without knowing it.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from typing import Any
from uuid import UUID

import httpx
import openai
from langchain.chat_models import init_chat_model
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable
from pydantic import ConfigDict, Field

from incident_helper.config import Settings, get_settings
from incident_helper.gateway.cache import CacheHit, PromptCache
from incident_helper.gateway.keys import KeyPool, fingerprint
from incident_helper.gateway.pricing import estimate_cost
from incident_helper.gateway.router import Route, RouteDecision, SemanticRouter
from incident_helper.gateway.telemetry import FinOpsLedger, TelemetryRecord


class Tier(StrEnum):
    FAST = "fast"
    REASONING = "reasoning"
    JUDGE = "judge"
    AUTO = "auto"


ModelFactory = Callable[[str, str], BaseChatModel]

# Worth a short retry on the same model. Auth failures are not: they go straight to the fallback.
TRANSIENT_ERRORS: tuple[type[Exception], ...] = (
    openai.RateLimitError,
    openai.APITimeoutError,
    openai.APIConnectionError,
    openai.InternalServerError,
    httpx.TimeoutException,
)
_PROVIDER_KEY_ARG = {"google_genai": "google_api_key", "ollama": ""}


def default_model_factory(spec: str, api_key: str, *, timeout: float = 60.0) -> BaseChatModel:
    """Provider-agnostic construction. Retries live in the gateway, so clients do not retry too."""
    provider = spec.split(":", 1)[0]
    kwargs: dict[str, Any] = {"temperature": 0, "timeout": timeout}
    if provider == "ollama":
        return init_chat_model(spec, temperature=0)
    arg = _PROVIDER_KEY_ARG.get(provider, "api_key")
    kwargs["max_retries"] = 0
    if api_key:
        kwargs[arg] = api_key
    return init_chat_model(spec, **kwargs)


def simulated_rate_limit() -> openai.RateLimitError:
    request = httpx.Request("POST", "https://chaos.invalid/v1/chat/completions")
    return openai.RateLimitError(
        "simulated 429", response=httpx.Response(429, request=request), body={"error": "simulated"}
    )


def simulated_auth_failure() -> openai.AuthenticationError:
    request = httpx.Request("POST", "https://chaos.invalid/v1/chat/completions")
    return openai.AuthenticationError(
        "simulated 401", response=httpx.Response(401, request=request), body={"error": "simulated"}
    )


class ChaosChatModel(BaseChatModel):
    """Always fails with a genuine provider exception type, for the fallback demonstration."""

    mode: str = "rate_limit"

    @property
    def _llm_type(self) -> str:
        return "chaos"

    def _generate(self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kw: Any):
        raise simulated_auth_failure() if self.mode == "auth" else simulated_rate_limit()

    def bind_tools(self, tools: Any, **kwargs: Any) -> Runnable:
        return self


@dataclass(slots=True)
class GatewayResponse:
    content: str
    telemetry: TelemetryRecord
    route: RouteDecision | None = None


class _AttemptTracker(BaseCallbackHandler):
    """Observes which candidate actually served the request inside `.with_fallbacks()`."""

    def __init__(self) -> None:
        self.runs: dict[UUID, str] = {}
        self.failed: list[str] = []
        self.failed_keys: list[str] = []
        self.keys: dict[UUID, str] = {}
        self.served_by: str | None = None
        self.first_attempt: tuple[str, str] | None = None
        self.served_attempt: tuple[str, str] | None = None
        self.usage: tuple[int, int] = (0, 0)

    def on_chat_model_start(self, serialized, messages, *, run_id, metadata=None, **kw) -> None:
        metadata = metadata or {}
        self.runs[run_id] = metadata.get("gw_candidate", "unknown")
        self.keys[run_id] = metadata.get("gw_key", "")
        if self.first_attempt is None:
            self.first_attempt = (self.runs[run_id], self.keys[run_id])

    def on_llm_end(self, response, *, run_id, **kw) -> None:
        self.served_by = self.runs.get(run_id, "unknown")
        self.served_attempt = (self.served_by, self.keys.get(run_id, ""))

    def on_llm_error(self, error, *, run_id, **kw) -> None:
        self.failed.append(f"{self.runs.get(run_id, 'unknown')}:{type(error).__name__}")
        self.failed_keys.append(self.keys.get(run_id, ""))


class ModelGateway:
    def __init__(
        self,
        settings: Settings | None = None,
        model_factory: ModelFactory | None = None,
        ledger: FinOpsLedger | None = None,
        cache: PromptCache | None = None,
        router: SemanticRouter | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._factory = model_factory or (
            lambda spec, key: default_model_factory(spec, key, timeout=self.settings.llm_timeout_seconds)
        )
        self.ledger = ledger or FinOpsLedger(self.settings.var_dir / "telemetry.jsonl")
        self._cache = cache
        self._router = router
        self._models: dict[tuple[str, str], BaseChatModel] = {}
        self._pools: dict[str, KeyPool] = {}

    # ------------------------------------------------------------------ lazily built components
    @property
    def cache(self) -> PromptCache:
        if self._cache is None:
            from incident_helper.embeddings import get_embeddings

            self._cache = PromptCache(
                self.settings.var_dir / "llm_cache.sqlite",
                embed=get_embeddings().embed_query,
                ttl_seconds=self.settings.cache_ttl_seconds,
                semantic_threshold=self.settings.semantic_cache_threshold,
            )
        return self._cache

    @property
    def router(self) -> SemanticRouter:
        if self._router is None:
            from incident_helper.embeddings import get_embeddings

            embedder = get_embeddings()
            self._router = SemanticRouter(embedder.embed_documents, embedder.embed_query)
        return self._router

    def route(self, prompt: str) -> RouteDecision:
        return self.router.classify(prompt)

    def _tier_for(self, tier: Tier, messages: Sequence[BaseMessage]) -> tuple[Tier, RouteDecision | None]:
        if tier is not Tier.AUTO:
            return tier, None
        last_human = next(
            (
                m.text
                for m in reversed(messages)
                if isinstance(m, HumanMessage) and not m.text.startswith("[system]")
            ),
            "",
        )
        decision = self.route(last_human)
        return (Tier.FAST if decision.route is Route.SIMPLE_FAQ else Tier.REASONING), decision

    def _spec(self, tier: Tier) -> str:
        return {
            Tier.FAST: self.settings.fast_model,
            Tier.REASONING: self.settings.reasoning_model,
            Tier.JUDGE: self.settings.judge_model,
        }[tier]

    def _pool(self, provider: str, *, fallback: bool = False) -> KeyPool:
        name = f"{provider}:fallback" if fallback else provider
        if name not in self._pools:
            raw = self.settings.key_for(provider)
            if fallback and provider == "openai" and self.settings.openai_fallback_api_key:
                raw = self.settings.openai_fallback_api_key.get_secret_value()
            self._pools[name] = KeyPool.parse(raw)
        return self._pools[name]

    def _model(self, spec: str, key: str) -> BaseChatModel:
        cached = self._models.get((spec, key))
        if cached is None:
            cached = self._models[(spec, key)] = self._factory(spec, key)
        return cached

    def _chain(self, tier: Tier, bind: Callable[[BaseChatModel], Runnable] | None) -> Runnable:
        """Build primary[key1..n] -> fallback[key1..n] using the standard `.with_fallbacks()`."""
        spec = self._spec(tier)
        fallback_spec = self.settings.fallback_model
        slots = [(spec, False)]
        if fallback_spec != spec or self.settings.openai_fallback_api_key:
            slots.append((fallback_spec, True))

        candidates: list[Runnable] = []
        if self.settings.gateway_chaos != "none":
            chaos = ChaosChatModel(mode=self.settings.gateway_chaos)
            candidates.append(chaos.with_config(metadata={"gw_candidate": spec, "gw_key": "chaos"}))
        for candidate_spec, is_fallback in slots:
            if self.settings.gateway_chaos != "none" and not is_fallback:
                continue  # the chaos model stands in for the primary
            pool = self._pool(candidate_spec.split(":", 1)[0], fallback=is_fallback)
            for key in pool.ordered():
                model: Runnable = self._model(candidate_spec, key)
                model = bind(model) if bind else model
                candidates.append(
                    model.with_retry(
                        retry_if_exception_type=TRANSIENT_ERRORS,
                        wait_exponential_jitter=True,
                        stop_after_attempt=2,
                    ).with_config(metadata={"gw_candidate": candidate_spec, "gw_key": fingerprint(key)})
                )
        return candidates[0].with_fallbacks(candidates[1:]) if len(candidates) > 1 else candidates[0]

    def _finish(
        self,
        tracker: _AttemptTracker,
        message: AIMessage,
        started: float,
        tier: Tier,
        route: RouteDecision | None,
        purpose: str,
    ) -> TelemetryRecord:
        primary = self._spec(tier)
        model_used = tracker.served_by or primary
        for key_id in tracker.failed_keys:
            for pool in self._pools.values():
                pool.report_failure(key_id)
        usage = message.usage_metadata or {}
        input_tokens, output_tokens = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
        entry = TelemetryRecord(
            model_used=model_used,
            fallback_triggered=tracker.served_attempt != tracker.first_attempt,
            cache_hit=False,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=estimate_cost(model_used, input_tokens, output_tokens),
            tier=tier.value,
            route=route.route.value if route else "",
            purpose=purpose,
            attempts=tracker.failed,
        )
        self.ledger.record(entry)
        return entry

    # ------------------------------------------------------------------ calls used by GatewayChatModel
    def complete(
        self,
        messages: list[BaseMessage],
        *,
        tier: Tier,
        bind: Callable[[BaseChatModel], Runnable] | None = None,
        purpose: str = "",
        **kwargs: Any,
    ) -> AIMessage:
        return self._run(messages, tier, bind, purpose, **kwargs)[0]

    def _run(
        self, messages, tier, bind=None, purpose="", **kwargs
    ) -> tuple[AIMessage, TelemetryRecord, RouteDecision | None]:
        started = time.perf_counter()
        resolved, route = self._tier_for(tier, messages)
        tracker = _AttemptTracker()
        chain = self._chain(resolved, bind)
        message = chain.invoke(messages, config={"callbacks": [tracker]}, **kwargs)
        return message, self._finish(tracker, message, started, resolved, route, purpose), route

    async def acomplete(
        self,
        messages: list[BaseMessage],
        *,
        tier: Tier,
        bind: Callable[[BaseChatModel], Runnable] | None = None,
        purpose: str = "",
        **kwargs: Any,
    ) -> AIMessage:
        started = time.perf_counter()
        resolved, route = self._tier_for(tier, messages)
        tracker = _AttemptTracker()
        chain = self._chain(resolved, bind)
        message = await chain.ainvoke(messages, config={"callbacks": [tracker]}, **kwargs)
        self._finish(tracker, message, started, resolved, route, purpose)
        return message

    def stream(self, messages, *, tier: Tier, bind=None, purpose: str = "", **kw) -> Iterator[AIMessageChunk]:
        started = time.perf_counter()
        resolved, route = self._tier_for(tier, messages)
        tracker = _AttemptTracker()
        merged: AIMessageChunk | None = None
        for chunk in self._chain(resolved, bind).stream(messages, config={"callbacks": [tracker]}, **kw):
            merged = chunk if merged is None else merged + chunk
            yield chunk
        if merged is not None:
            self._finish(tracker, merged, started, resolved, route, purpose)

    async def astream(
        self, messages, *, tier: Tier, bind=None, purpose: str = "", **kw
    ) -> AsyncIterator[AIMessageChunk]:
        started = time.perf_counter()
        resolved, route = self._tier_for(tier, messages)
        tracker = _AttemptTracker()
        merged: AIMessageChunk | None = None
        chain = self._chain(resolved, bind)
        async for chunk in chain.astream(messages, config={"callbacks": [tracker]}, **kw):
            merged = chunk if merged is None else merged + chunk
            yield chunk
        if merged is not None:
            self._finish(tracker, merged, started, resolved, route, purpose)

    # ------------------------------------------------------------------ public API
    def _cache_namespace(self, system: str) -> str:
        """Any change to the models or the context gets a fresh namespace, so stale answers cannot hit."""
        profile = (
            self.settings.fast_model,
            self.settings.reasoning_model,
            self.settings.fallback_model,
            system,
        )
        return hashlib.sha256(json.dumps(profile).encode()).hexdigest()[:16]

    def ask(
        self,
        prompt: str,
        *,
        system: str = "",
        tier: Tier = Tier.AUTO,
        use_cache: bool = True,
        purpose: str = "ask",
    ) -> GatewayResponse:
        """Cache lookup, then routing, then a model call with fallbacks. Telemetry on every path."""
        started = time.perf_counter()
        namespace = self._cache_namespace(system)
        if use_cache:
            # Exact lookup precedes routing so a hit does not pay for an embedding.
            hit = self.cache.lookup_exact(prompt, namespace) or self.cache.lookup_semantic(prompt, namespace)
            if hit is not None:
                return self._cache_response(hit, started, purpose)

        messages: list[BaseMessage] = [SystemMessage(content=system)] if system else []
        messages.append(HumanMessage(content=prompt))
        message, record, route = self._run(messages, tier, purpose=purpose)
        if use_cache:
            self.cache.store(prompt, namespace, message.text)
        return GatewayResponse(message.text, record, route)

    def _cache_response(self, hit: CacheHit, started: float, purpose: str) -> GatewayResponse:
        entry = TelemetryRecord(
            model_used="cache",
            fallback_triggered=False,
            cache_hit=True,
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
            input_tokens=0,
            output_tokens=0,
            estimated_cost_usd=0.0,
            purpose=purpose,
            cache_kind=hit.kind,
        )
        self.ledger.record(entry)
        return GatewayResponse(hit.text, entry)

    def chat_model(self, tier: Tier = Tier.AUTO, purpose: str = "") -> GatewayChatModel:
        return GatewayChatModel(gateway=self, tier=tier, purpose=purpose)


class GatewayChatModel(BaseChatModel):
    """Adapter that lets anything expecting a chat model transparently use the gateway."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    gateway: Any = Field(exclude=True)
    tier: Tier = Tier.AUTO
    purpose: str = ""
    bound_tools: Any = None
    bound_kwargs: dict[str, Any] = Field(default_factory=dict)

    @property
    def _llm_type(self) -> str:
        return "incident-gateway"

    def bind_tools(self, tools: Any, *, tool_choice: Any = None, **kwargs: Any) -> Runnable:
        extra = dict(kwargs)
        if tool_choice is not None:
            extra["tool_choice"] = tool_choice
        return self.model_copy(update={"bound_tools": list(tools), "bound_kwargs": extra})

    def _binder(self) -> Callable[[BaseChatModel], Runnable] | None:
        if self.bound_tools is None:
            return None
        tools, kwargs = self.bound_tools, self.bound_kwargs
        # `ls_structured_output_format` is tracing metadata for BaseChatModel, not for providers' APIs.
        return lambda model: model.bind_tools(tools, **kwargs)

    def _call_args(self) -> dict[str, Any]:
        return {"tier": self.tier, "bind": self._binder(), "purpose": self.purpose}

    def _generate(
        self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs: Any
    ) -> ChatResult:
        message = self.gateway.complete(messages, **self._call_args())
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _agenerate(
        self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs: Any
    ) -> ChatResult:
        message = await self.gateway.acomplete(messages, **self._call_args())
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _stream(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs: Any):
        for chunk in self.gateway.stream(messages, **self._call_args()):
            yield ChatGenerationChunk(message=chunk)

    async def _astream(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs: Any):
        async for chunk in self.gateway.astream(messages, **self._call_args()):
            yield ChatGenerationChunk(message=chunk)


@lru_cache
def get_gateway() -> ModelGateway:
    return ModelGateway()
