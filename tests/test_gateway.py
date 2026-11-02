import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel

from incident_helper.gateway import ModelGateway, Tier
from incident_helper.gateway.cache import PromptCache, entities
from incident_helper.gateway.core import simulated_rate_limit
from incident_helper.gateway.keys import KeyPool, fingerprint
from incident_helper.gateway.pricing import estimate_cost
from incident_helper.gateway.router import Route, SemanticRouter
from support import ScriptedChatModel, structured, usage

FAST, REASON, FALLBACK = "openai:gpt-4o-mini", "openai:gpt-4o", "openai:gpt-4o-mini"
REQUIRED_KEYS = {
    "model_used",
    "fallback_triggered",
    "cache_hit",
    "latency_ms",
    "input_tokens",
    "output_tokens",
    "estimated_cost_usd",
}


def reply(text="ok", i=100, o=20):
    return AIMessage(content=text, usage_metadata=usage(i, o))


def test_telemetry_record_has_every_required_key(make_gateway):
    gw = make_gateway({REASON: ScriptedChatModel(script=[reply()]), FAST: ScriptedChatModel()})
    response = gw.ask("anything", tier=Tier.REASONING, use_cache=False)
    assert set(response.telemetry.to_dict()) >= REQUIRED_KEYS
    assert response.telemetry.input_tokens == 100
    assert response.telemetry.estimated_cost_usd == pytest.approx(0.00045)


def test_rate_limit_falls_back_without_raising(make_gateway):
    primary = ScriptedChatModel(script=[simulated_rate_limit(), simulated_rate_limit()])
    fallback = ScriptedChatModel(script=[reply("from fallback")])
    gw = make_gateway({REASON: primary, FAST: fallback})
    response = gw.ask("anything", tier=Tier.REASONING, use_cache=False)
    assert response.content == "from fallback"
    assert response.telemetry.fallback_triggered is True
    assert response.telemetry.model_used == FALLBACK


def test_same_model_retry_that_succeeds_is_not_a_fallback(make_gateway):
    primary = ScriptedChatModel(script=[simulated_rate_limit(), reply("second try")])
    gw = make_gateway({REASON: primary, FAST: ScriptedChatModel()})
    response = gw.ask("q", tier=Tier.REASONING, use_cache=False)
    assert response.content == "second try"
    assert response.telemetry.fallback_triggered is False
    assert response.telemetry.attempts


def test_healthy_call_does_not_report_fallback(make_gateway):
    gw = make_gateway({REASON: ScriptedChatModel(script=[reply()]), FAST: ScriptedChatModel()})
    assert gw.ask("x", tier=Tier.REASONING, use_cache=False).telemetry.fallback_triggered is False


def test_chaos_mode_triggers_fallback(make_gateway, settings):
    chaos = settings.model_copy(update={"gateway_chaos": "auth"})
    gw = make_gateway({FAST: ScriptedChatModel(script=[reply("saved")])}, settings=chaos)
    response = gw.ask("q", tier=Tier.REASONING, use_cache=False)
    assert (response.content, response.telemetry.fallback_triggered) == ("saved", True)


def test_all_candidates_failing_is_a_real_error(make_gateway):
    gw = make_gateway(
        {
            REASON: ScriptedChatModel(script=[ValueError("a")]),
            FAST: ScriptedChatModel(script=[ValueError("b")]),
        }
    )
    with pytest.raises(ValueError):
        gw.ask("q", tier=Tier.REASONING, use_cache=False)


def test_exact_cache_second_call_is_free_and_under_50ms(make_gateway):
    model = ScriptedChatModel(script=[reply("answer")])
    gw = make_gateway({REASON: model, FAST: model})
    first = gw.ask("What is the SEV1 acknowledge time?", tier=Tier.REASONING)
    second = gw.ask("what is the  SEV1 acknowledge time?", tier=Tier.REASONING)
    assert (first.telemetry.cache_hit, second.telemetry.cache_hit) == (False, True)
    assert second.content == "answer"
    assert second.telemetry.latency_ms <= 50
    assert second.telemetry.estimated_cost_usd == 0
    assert len(model.seen) == 1


def test_cache_entry_expires(tmp_path):
    now = [1000.0]
    cache = PromptCache(tmp_path / "c.sqlite", ttl_seconds=10, clock=lambda: now[0])
    cache.store("q", "ns", "a")
    assert cache.lookup_exact("q", "ns").text == "a"
    now[0] += 11
    assert cache.lookup_exact("q", "ns") is None


def fixed_embed(text):
    return [1.0, 0.0]  # every prompt looks identical, the worst case for a semantic cache


def test_semantic_cache_matches_paraphrase_of_same_entity(tmp_path):
    cache = PromptCache(tmp_path / "c.sqlite", embed=fixed_embed)
    cache.store("Is the login service up?", "ns", "yes")
    assert cache.lookup_semantic("Is login working right now?", "ns").kind == "semantic"


def test_semantic_cache_never_crosses_services(tmp_path):
    cache = PromptCache(tmp_path / "c.sqlite", embed=fixed_embed)
    cache.store("Is the login service up?", "ns", "yes")
    assert cache.lookup_semantic("Is the payments service up?", "ns") is None


def test_changed_system_context_cannot_serve_a_stale_answer(make_gateway):
    model = ScriptedChatModel(script=[reply("degraded"), reply("healthy")])
    gw = make_gateway({REASON: model, FAST: model})
    first = gw.ask("Is login up?", system="state: degraded", tier=Tier.FAST)
    second = gw.ask("Is login up?", system="state: healthy", tier=Tier.FAST)
    assert (first.content, second.content) == ("degraded", "healthy")


def test_entity_extraction_sees_versions_and_services():
    assert {"payments-api", "2.13.4"} <= entities("Roll payments-api to 2.13.4")


def test_key_pool_rotates_and_cools_down():
    now = [0.0]
    pool = KeyPool(["a", "b", "c"], cooldown_seconds=30, clock=lambda: now[0])
    assert pool.ordered()[0] == "a"
    assert pool.ordered()[0] == "b"
    pool.report_failure(fingerprint("c"))
    assert pool.ordered() == ["a", "b", "c"]
    now[0] = 31
    assert [pool.ordered()[0] for _ in range(3)] == ["a", "b", "c"]


def test_second_key_is_tried_before_the_fallback_model(make_gateway, settings):
    multi = settings.model_copy(update={"openai_api_key": type(settings.openai_api_key)("k1,k2")})
    bad = ScriptedChatModel(script=[ValueError("401")])
    good = ScriptedChatModel(script=[reply("second key")])
    gw = ModelGateway(
        settings=multi,
        model_factory=lambda spec, key: {("openai:gpt-4o", "k1"): bad, ("openai:gpt-4o", "k2"): good}.get(
            (spec, key), ScriptedChatModel()
        ),
        cache=PromptCache(settings.var_dir / "c.sqlite"),
    )
    assert gw.ask("q", tier=Tier.REASONING, use_cache=False).content == "second key"


def test_gateway_chat_model_supports_structured_output_through_fallback(make_gateway):
    class Verdict(BaseModel):
        ok: bool

    primary = ScriptedChatModel(script=[simulated_rate_limit(), simulated_rate_limit()])
    fallback = ScriptedChatModel(script=[structured("Verdict", ok=True)])
    gw = make_gateway({REASON: primary, FAST: fallback})
    result = gw.chat_model(Tier.REASONING).with_structured_output(Verdict).invoke("hi")
    assert result == Verdict(ok=True)
    assert gw.ledger.records[-1].fallback_triggered is True


def test_gateway_chat_model_streams_tokens_and_logs_once(make_gateway):
    gw = make_gateway({REASON: ScriptedChatModel(script=[reply("streamed")]), FAST: ScriptedChatModel()})
    chunks = list(gw.chat_model(Tier.REASONING).stream([HumanMessage(content="hi")]))
    assert "".join(c.text for c in chunks) == "streamed"
    assert len(gw.ledger.records) == 1


def test_unknown_model_cost_is_not_invented():
    assert estimate_cost("openai:brand-new-model", 10, 10) is None
    assert estimate_cost("gpt-4o-mini-2024-07-18", 1_000_000, 0) == 0.15


def test_ledger_writes_valid_json_lines(settings):
    gw = ModelGateway(
        settings=settings,
        model_factory=lambda s, k: ScriptedChatModel(script=[reply()]),
        cache=PromptCache(settings.var_dir / "c.sqlite"),
    )
    gw.ask("x", tier=Tier.FAST, use_cache=False)
    rows = [json.loads(x) for x in (settings.var_dir / "telemetry.jsonl").read_text().splitlines()]
    assert rows[0]["model_used"] == FAST
    assert gw.ledger.summary()["calls"] == 1


@pytest.fixture(scope="module")
def router():
    from incident_helper.embeddings import get_embeddings

    e = get_embeddings()
    return SemanticRouter(e.embed_documents, e.embed_query)


@pytest.mark.parametrize(
    "query",
    [
        "Is the login service up?",
        "Is payments-api healthy right now?",
        "Who is the Incident Commander on call?",
        "How many instances does checkout-web have?",
    ],
)
def test_router_sends_lookups_to_the_cheap_model(router, query):
    assert router.classify(query).route is Route.SIMPLE_FAQ


@pytest.mark.parametrize(
    "query",
    [
        "Why did payments fail after the 2 a.m. release?",
        "What caused last night's outage and how do we stop it recurring?",
        "Why are logins failing even though the database looks healthy?",
        "Should we fail over the database or roll back the release first?",
    ],
)
def test_router_sends_diagnosis_to_the_reasoning_model(router, query):
    assert router.classify(query).route is Route.COMPLEX_REASONING
