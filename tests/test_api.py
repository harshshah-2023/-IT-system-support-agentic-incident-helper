"""API tests run a real guarded graph over scripted models, through the HTTP surface."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from incident_helper import api as api_module
from incident_helper import tools as T
from incident_helper.agents.guarded import build_guarded_graph
from incident_helper.domain import Incident, Severity
from support import ScriptedChatModel, tool_call

ROLLBACK = tool_call("rollback_deployment", "rb1", service="payments-api", version="2.13.4")


class FakeServices:
    def __init__(self, store, settings, gateway):
        self.store, self.settings, self.gateway = store, settings, gateway


@pytest.fixture
def client(store, settings, make_gateway, monkeypatch):
    T.use_store(store)
    store.save_incident(Incident(id="INC-1042", title="Slow payments", severity=Severity.SEV1))
    services = FakeServices(store, settings, make_gateway({}))
    monkeypatch.setattr(api_module, "get_services", lambda: services)
    model = ScriptedChatModel(script=[ROLLBACK, AIMessage(content="Rolled back.")])
    services.build_copilot = lambda checkpointer=None: build_guarded_graph(
        model, checkpointer=checkpointer, authorised=settings.oncall
    )
    with TestClient(api_module.app) as test_client:
        yield test_client


def events(response) -> list[tuple[str, dict]]:
    frames = response.text.replace("\r\n", "\n").split("\n\n")
    out = []
    for frame in frames:
        lines = dict(line.split(": ", 1) for line in frame.splitlines() if ": " in line)
        if "event" in lines:
            out.append((lines["event"], json.loads(lines["data"])))
    return out


def test_chat_pauses_and_status_shows_waiting(client, store):
    reply = client.post("/api/incidents/INC-1042/chat", json={"message": "roll it back"})
    done = events(reply)[-1]
    assert done[0] == "done" and done[1]["pending"]["tool_calls"][0]["args"]["version"] == "2.13.4"
    detail = client.get("/api/incidents/INC-1042").json()
    assert detail["incident"]["status"] == "awaiting_approval"
    assert store.service_status("payments-api").version == "2.14.0"


def test_second_message_is_refused_while_paused(client):
    client.post("/api/incidents/INC-1042/chat", json={"message": "roll it back"})
    assert client.post("/api/incidents/INC-1042/chat", json={"message": "again"}).status_code == 409


def test_unauthorised_approver_gets_403(client, store):
    client.post("/api/incidents/INC-1042/chat", json={"message": "roll it back"})
    body = {"action": "approve", "approver": "someone.else"}
    assert client.post("/api/incidents/INC-1042/decision", json=body).status_code == 403
    assert store.service_status("payments-api").version == "2.14.0"


def test_decision_with_nothing_pending_conflicts(client):
    assert (
        client.post(
            "/api/incidents/INC-1042/decision", json={"action": "approve", "approver": "asha.menon"}
        ).status_code
        == 409
    )


def test_approval_executes_and_marks_mitigated(client, store):
    client.post("/api/incidents/INC-1042/chat", json={"message": "roll it back"})
    reply = client.post(
        "/api/incidents/INC-1042/decision", json={"action": "approve", "approver": "asha.menon"}
    )
    assert any(kind == "tool_end" for kind, _ in events(reply))
    assert store.service_status("payments-api").version == "2.13.4"
    assert client.get("/api/incidents/INC-1042").json()["incident"]["status"] == "mitigated"


def test_unknown_decision_is_rejected_before_the_graph(client):
    client.post("/api/incidents/INC-1042/chat", json={"message": "roll it back"})
    body = {"action": "maybe", "approver": "asha.menon"}
    assert client.post("/api/incidents/INC-1042/decision", json=body).status_code == 422


def test_tool_output_is_sanitised_before_reaching_the_browser(store, settings, make_gateway, monkeypatch):
    T.use_store(store)
    store.save_incident(Incident(id="INC-1042", title="x", severity=Severity.SEV1))
    services = FakeServices(store, settings, make_gateway({}))
    monkeypatch.setattr(api_module, "get_services", lambda: services)
    logs = tool_call("get_service_logs", "l1", service="payments-api")
    model = ScriptedChatModel(script=[logs, AIMessage(content="ok")])
    services.build_copilot = lambda checkpointer=None: build_guarded_graph(model, checkpointer=checkpointer)
    with TestClient(api_module.app) as local:
        reply = local.post("/api/incidents/INC-1042/chat", json={"message": "logs"})
    assert "Pr0d-S3cret" not in reply.text and "priya.nair@example.com" not in reply.text


def test_rejection_does_not_mark_the_incident_mitigated(client):
    client.post("/api/incidents/INC-1042/chat", json={"message": "roll it back"})
    client.post("/api/incidents/INC-1042/decision", json={"action": "reject", "approver": "asha.menon"})
    assert client.get("/api/incidents/INC-1042").json()["incident"]["status"] != "mitigated"


def test_unknown_incident_is_404(client):
    assert client.get("/api/incidents/NOPE").status_code == 404


def test_estate_and_static_page_are_served(client):
    assert len(client.get("/api/estate").json()) == 11
    assert "Incident Response Helper" in client.get("/").text
