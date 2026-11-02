"""Integration tests against real models. Skipped without a key. Run with: pytest -m live"""

import pytest
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.types import Command

from incident_helper.agents.guarded import build_guarded_graph
from incident_helper.agents.react import build_react_graph, thread_config
from incident_helper.config import Settings
from incident_helper.domain import Severity
from incident_helper.extraction import CustomerTicket, build_chat_model, extract_tickets
from incident_helper.gateway import ModelGateway, Tier
from incident_helper.gateway.cache import PromptCache
from incident_helper.gateway.telemetry import FinOpsLedger
from incident_helper.tools import use_store

live = Settings()
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not live.llm_configured, reason="no LLM credential configured"),
]


@pytest.fixture(autouse=True)
def _store(store):
    use_store(store)


def test_real_extraction_of_all_five_messages(store):
    tickets = extract_tickets(store.inbox())
    assert all(isinstance(t, CustomerTicket) for t in tickets)
    assert tickets[0].severity in (Severity.SEV1, Severity.SEV2)
    assert tickets[4].sentiment in ("angry", "frustrated")


def test_real_two_hop_follows_the_degraded_dependency():
    graph = build_react_graph(build_chat_model())
    query = "Checkout is slow. Which upstream service is degraded, and what was its latest release?"
    result = graph.invoke({"messages": [HumanMessage(content=query)]}, thread_config("live"))
    names = [m.name for m in result["messages"] if isinstance(m, ToolMessage)]
    assert names[0] == "check_service_status" and "get_recent_deployments" in names
    assert "2.14.0" in result["messages"][-1].text


def test_real_rollback_pauses_and_reject_leaves_state_alone(store, tmp_path):
    graph = build_guarded_graph(build_chat_model(spec=live.reasoning_model), authorised={"asha.menon"})
    config = thread_config("live-hitl", live.guarded_recursion_limit)
    prompt = "Roll payments-api back to 2.13.4 now."
    graph.invoke({"messages": [HumanMessage(content=prompt)], "incident_id": "INC-1042"}, config)
    assert graph.get_state(config).next == ("human_approval",)
    graph.invoke(Command(resume={"action": "reject", "approver": "asha.menon", "reason": "test"}), config)
    assert store.service_status("payments-api").version == "2.14.0"


def test_real_bad_key_falls_back(tmp_path):
    good = live.key_for("openai").split(",")[0]
    settings = Settings(
        **{
            **live.model_dump(),
            "openai_api_key": "sk-invalid-for-test",
            "openai_fallback_api_key": good,
            "var_dir": tmp_path,
        }
    )
    gateway = ModelGateway(settings, ledger=FinOpsLedger(), cache=PromptCache(tmp_path / "c.sqlite"))
    response = gateway.ask("Reply with OK", tier=Tier.REASONING, use_cache=False)
    assert response.telemetry.fallback_triggered is True and response.content
