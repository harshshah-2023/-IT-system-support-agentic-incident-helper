"""FastAPI backend for the demonstration UI. All behaviour lives in the same services the CLI uses."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from incident_helper.agents.deep import ROLES
from incident_helper.agents.guarded import CANARY
from incident_helper.agents.react import thread_config
from incident_helper.domain import Incident, IncidentStatus, Severity
from incident_helper.evals.runner import load_results
from incident_helper.security.pii import sanitize
from incident_helper.services import get_services

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    services = get_services()
    async with AsyncSqliteSaver.from_conn_string(str(services.settings.checkpoint_db)) as saver:
        app.state.graph = services.build_copilot(checkpointer=saver)
        app.state.rca_runs = {}
        yield


app = FastAPI(title="Incident Response Helper", lifespan=lifespan)


class ChatRequest(BaseModel):
    message: str


class Decision(BaseModel):
    action: Literal["approve", "edit", "reject"]
    approver: str
    args: dict[str, Any] | None = None
    reason: str | None = None


class NewIncident(BaseModel):
    title: str
    severity: Severity = Severity.SEV3
    service: str | None = None
    summary: str = ""


class Question(BaseModel):
    question: str


class RcaRequest(BaseModel):
    question: str = "What caused this incident, and how do we stop it happening again?"
    fault: str | None = None


def _config(incident_id: str) -> dict[str, Any]:
    return thread_config(incident_id, get_services().settings.guarded_recursion_limit)


def _render(message: Any) -> dict[str, Any]:
    if isinstance(message, HumanMessage):
        return {"role": "human", "text": message.text}
    if isinstance(message, ToolMessage):
        return {"role": "tool", "name": message.name, "text": str(message.content), "status": message.status}
    if isinstance(message, AIMessage):
        return {
            "role": "ai",
            "text": message.text,
            "tool_calls": [{"name": c["name"], "args": c["args"]} for c in message.tool_calls],
        }
    return {"role": message.type, "text": message.text}


async def _pending(incident_id: str) -> dict[str, Any] | None:
    snapshot = await app.state.graph.aget_state(_config(incident_id))
    for task in snapshot.tasks:
        if task.interrupts:
            return task.interrupts[0].value
    return None


@app.get("/api/status")
def status() -> dict[str, Any]:
    services = get_services()
    s = services.settings
    return {
        "llm_configured": s.llm_configured,
        "fast_model": s.fast_model,
        "reasoning_model": s.reasoning_model,
        "fallback_model": s.fallback_model,
        "langsmith_configured": bool(s.langsmith_api_key),
        "document": s.runbook_path.name,
        "threshold": s.rag_score_threshold,
        "on_call": sorted(s.oncall),
    }


@app.get("/api/estate")
def estate() -> list[dict[str, Any]]:
    return [s.model_dump() for s in get_services().store.estate_overview()]


@app.get("/api/inbox")
def inbox() -> list[dict[str, str]]:
    return get_services().store.inbox()


@app.get("/api/incidents")
def incidents() -> list[dict[str, Any]]:
    return [i.model_dump() for i in get_services().store.list_incidents()]


@app.post("/api/triage")
async def triage() -> list[dict[str, Any]]:
    found = await asyncio.to_thread(get_services().triage_inbox)
    return [i.model_dump() for i in found]


@app.post("/api/incidents")
def create_incident(body: NewIncident) -> dict[str, Any]:
    store = get_services().store
    number = 2000 + len(store.list_incidents())
    return store.save_incident(Incident(id=f"INC-{number}", **body.model_dump())).model_dump()


@app.get("/api/incidents/{incident_id}")
async def incident_detail(incident_id: str) -> dict[str, Any]:
    store = get_services().store
    try:
        incident = store.get_incident(incident_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    snapshot = await app.state.graph.aget_state(_config(incident_id))
    values = snapshot.values or {}
    return {
        "incident": incident.model_dump(),
        "messages": [_render(m) for m in values.get("messages", [])],
        "pending": await _pending(incident_id),
        "approvals": values.get("approval_log", []),
        "security_events": values.get("security_events", []),
        "next": list(snapshot.next),
    }


async def _sse_from_graph(incident_id: str, graph_input: Any) -> AsyncIterator[dict[str, str]]:
    """Maps LangGraph `astream_events` to the UI's live timeline."""

    def send(kind: str, **data: Any) -> dict[str, str]:
        return {"event": kind, "data": json.dumps(data, default=str)}

    nodes = {
        "input_guardrail",
        "agent",
        "tool_guard",
        "human_approval",
        "tools",
        "tool_output_guard",
        "action_check",
        "output_guardrail",
    }
    live_text = ""
    try:
        async for event in app.state.graph.astream_events(graph_input, _config(incident_id), version="v2"):
            name, kind = event["name"], event["event"]
            if kind == "on_chat_model_stream" and event.get("metadata", {}).get("langgraph_node") == "agent":
                chunk = event["data"]["chunk"]
                if chunk.text:
                    # The output guardrail runs only after the stream, so tokens are screened here.
                    # Whole text each time: a secret split across two tokens is still caught.
                    live_text += chunk.text
                    if CANARY not in live_text:
                        yield send("token", text=sanitize(live_text).text)
            elif kind == "on_tool_start":
                yield send("tool_start", name=name, args=sanitize(str(event["data"].get("input"))).text)
            elif kind == "on_tool_end":
                output = event["data"].get("output")
                text = sanitize(str(getattr(output, "content", output))).text
                yield send("tool_end", name=name, output=text[:1500])
            elif kind == "on_chain_start" and name in nodes:
                if name == "agent":
                    live_text = ""
                yield send("node", name=name)
    except Exception as exc:
        yield send("error", message=f"{type(exc).__name__}: {exc}")

    pending = await _pending(incident_id)
    store = get_services().store
    incident = store.get_incident(incident_id)
    if pending:
        store.set_incident_status(incident_id, IncidentStatus.AWAITING_APPROVAL)
    elif incident.status in (IncidentStatus.OPEN, IncidentStatus.AWAITING_APPROVAL):
        store.set_incident_status(incident_id, IncidentStatus.INVESTIGATING)
    yield send("done", pending=pending)


@app.post("/api/incidents/{incident_id}/chat")
async def chat(incident_id: str, body: ChatRequest) -> EventSourceResponse:
    if await _pending(incident_id):
        raise HTTPException(409, "A rollback is waiting for a human decision")
    graph_input = {"messages": [HumanMessage(content=body.message)], "incident_id": incident_id}
    return EventSourceResponse(_sse_from_graph(incident_id, graph_input))


@app.post("/api/incidents/{incident_id}/decision")
async def decision(incident_id: str, body: Decision) -> EventSourceResponse:
    if not await _pending(incident_id):
        raise HTTPException(409, "Nothing is waiting for approval on this incident")
    services = get_services()
    if body.approver not in services.settings.oncall:
        raise HTTPException(403, f"{body.approver} is not an on-call engineer")

    async def stream() -> AsyncIterator[dict[str, str]]:
        before = len(services.store.audit_log())
        async for event in _sse_from_graph(incident_id, Command(resume=body.model_dump(exclude_none=True))):
            yield event
        # Mitigated only if a rollback really happened, not because someone clicked approve.
        new = services.store.audit_log()[: len(services.store.audit_log()) - before]
        if any(row["action"] == "rollback_executed" for row in new):
            services.store.set_incident_status(incident_id, IncidentStatus.MITIGATED)

    return EventSourceResponse(stream())


@app.post("/api/rag/compare")
async def rag_compare(body: Question) -> dict[str, Any]:
    results = await asyncio.to_thread(get_services().rag.compare, body.question)
    return {name: r.model_dump() for name, r in results.items()}


@app.get("/api/rag/stats")
def rag_stats() -> dict[str, Any]:
    path = get_services().settings.var_dir / "rag_comparison.md"
    return {"table": path.read_text(encoding="utf-8") if path.exists() else ""}


@app.post("/api/incidents/{incident_id}/rca")
async def start_rca(incident_id: str, body: RcaRequest) -> dict[str, str]:
    run_id = uuid4().hex[:8]
    events: list[dict[str, Any]] = []
    run: dict[str, Any] = {"events": events, "status": "running", "incident_id": incident_id, "dir": ""}
    app.state.rca_runs[run_id] = run

    def work() -> None:
        services = get_services()
        workflow = services.build_rca(
            incident_id, fault=body.fault, on_event=lambda k, d: events.append({"kind": k, **d})
        )  # type: ignore[arg-type]
        run["dir"] = str(workflow.workspace.root)
        try:
            result = workflow.compile().invoke(
                {"question": body.question, "incident_id": incident_id},
                {"configurable": {"thread_id": f"rca-{run_id}"}, "recursion_limit": 40},
            )
            run.update(
                status=result.get("status", "failed"),
                report_file=result.get("report_file", ""),
                errors=result.get("errors", []),
            )
        except Exception as exc:
            run.update(status="failed", errors=[f"{type(exc).__name__}: {exc}"])

    threading.Thread(target=work, daemon=True).start()
    return {"run_id": run_id}


@app.get("/api/rca/{run_id}")
def rca_status(run_id: str) -> dict[str, Any]:
    run = app.state.rca_runs.get(run_id)
    if run is None:
        raise HTTPException(404, "Unknown RCA run")
    report = ""
    if run.get("report_file"):
        report = Path(run["report_file"]).read_text(encoding="utf-8")
    drafts = {r: (Path(run["dir"]) / f"{r}.md").exists() for r in ROLES} if run["dir"] else {}
    return {**run, "report": report, "drafts": drafts}


@app.get("/api/rca/{run_id}/report.md", response_class=PlainTextResponse)
def rca_export(run_id: str) -> FileResponse:
    run = app.state.rca_runs.get(run_id)
    if not run or not run.get("report_file"):
        raise HTTPException(404, "No report for this run")
    return FileResponse(
        run["report_file"], media_type="text/markdown", filename=f"{run['incident_id']}-report.md"
    )


@app.get("/api/evals")
def evals() -> list[dict[str, Any]]:
    return load_results(get_services().settings)


@app.get("/api/gateway")
def gateway_summary() -> dict[str, Any]:
    ledger = get_services().gateway.ledger
    return {"summary": ledger.summary(), "recent": ledger.persisted(limit=40)[::-1]}


@app.post("/api/gateway/ask")
async def gateway_ask(body: Question) -> dict[str, Any]:
    services = get_services()
    response = await asyncio.to_thread(services.quick_answer, body.question)
    return {
        "content": response.content,
        "telemetry": response.telemetry.to_dict(),
        "route": response.route.model_dump() if response.route else None,
    }


@app.get("/api/audit")
def audit() -> list[dict[str, Any]]:
    return get_services().store.audit_log()


@app.post("/api/reset")
async def reset() -> dict[str, bool]:
    services = get_services()
    for incident in services.store.list_incidents():
        await app.state.graph.checkpointer.adelete_thread(incident.id)
    services.store.reset()
    services.gateway.cache.clear()
    return {"ok": True}


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
