"""Application services: one composition root wired once and shared by the CLI and the API."""

from __future__ import annotations

import json
import re
import sqlite3
from functools import cached_property, lru_cache
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.sqlite import SqliteSaver

from incident_helper.agents.deep import RcaWorkflow, Role, new_run_dir
from incident_helper.agents.guarded import build_guarded_graph
from incident_helper.config import Settings, get_settings
from incident_helper.domain import Incident
from incident_helper.embeddings import get_embeddings
from incident_helper.extraction import build_extractor, extract_tickets
from incident_helper.gateway import ModelGateway, Tier, get_gateway
from incident_helper.ops import OpsStore
from incident_helper.rag.schemas import RagResponse
from incident_helper.rag.tree import TreeRag, load_or_build_tree
from incident_helper.rag.vector import VectorRag
from incident_helper.security.injection import SemanticGuard
from incident_helper.tools import READ_TOOLS, get_store


class RagService:
    def __init__(self, gateway: ModelGateway, settings: Settings) -> None:
        self._gateway = gateway
        self._settings = settings

    @cached_property
    def vector(self) -> VectorRag:
        s = self._settings
        return VectorRag(
            s.runbook_path,
            get_embeddings(),
            self._gateway.chat_model(Tier.FAST, "rag_vector"),
            s.index_dir,
            threshold=s.rag_score_threshold,
            top_k=s.rag_top_k,
            chunk_size=s.rag_chunk_size,
            chunk_overlap=s.rag_chunk_overlap,
        )

    @cached_property
    def tree(self) -> TreeRag:
        s = self._settings
        root = load_or_build_tree(
            s.runbook_path, self._gateway.chat_model(Tier.FAST, "tree_summary"), s.index_dir
        )
        return TreeRag(
            # Navigation is this method's reasoning step: a cheap model measurably picks wrong branches.
            root,
            s.runbook_path.name,
            self._gateway.chat_model(Tier.REASONING, "tree_navigation"),
            self._gateway.chat_model(Tier.FAST, "rag_vectorless"),
        )

    def compare(self, question: str) -> dict[str, RagResponse]:
        return {"vector": self.vector.answer(question), "vectorless": self.tree.answer(question)}


def rag_tools(rag: RagService) -> list[BaseTool]:
    def render(response: RagResponse) -> str:
        cites = "; ".join(f"{c.section} (p.{c.page})" for c in response.citations) or "none"
        path = f"\nPath: {' | '.join(response.traversal)}" if response.traversal else ""
        return f"{response.answer}\nSources: {cites}{path}"

    @tool
    def search_runbook(question: str) -> str:
        """Look up a fact or procedure in the operations runbook with semantic search.

        Returns a cited answer, or 'Insufficient grounded context' when the runbook has nothing relevant.

        Args:
            question: A focused question about procedures, thresholds, contacts or policies.
        """
        return render(rag.vector.answer(question))

    @tool
    def navigate_runbook(question: str) -> str:
        """Find an answer by navigating the runbook's contents, section by section.

        Slower than search_runbook, but better when a procedure spans several chapters.
        Returns the answer with the exact path taken.

        Args:
            question: The question to answer from the runbook.
        """
        return render(rag.tree.answer(question))

    return [search_runbook, navigate_runbook]


class Services:
    def __init__(self, settings: Settings | None = None, gateway: ModelGateway | None = None) -> None:
        self.settings = settings or get_settings()
        self.gateway = gateway or get_gateway()
        self.store: OpsStore = get_store()
        self.rag = RagService(self.gateway, self.settings)

    @cached_property
    def checkpointer(self) -> SqliteSaver:
        connection = sqlite3.connect(self.settings.checkpoint_db, check_same_thread=False)
        return SqliteSaver(connection)

    def build_copilot(self, checkpointer: Any = None):
        return build_guarded_graph(
            self.gateway.chat_model(Tier.AUTO, "copilot"),
            tools=[*READ_TOOLS, *rag_tools(self.rag)],
            checkpointer=checkpointer or self.checkpointer,
            semantic=SemanticGuard(self.gateway.chat_model(Tier.FAST, "semantic_guard")),
            authorised=self.settings.oncall,
            summariser=self.gateway.chat_model(Tier.FAST, "summary"),
        )

    def build_rca(
        self,
        incident_id: str,
        fault: Role | None = None,
        max_replans: int = 2,
        on_event: Any = None,
        run_dir: Path | None = None,
    ) -> RcaWorkflow:
        gw = self.gateway
        return RcaWorkflow(
            gw.chat_model(Tier.REASONING, "rca_planner"),
            lambda role: gw.chat_model(Tier.REASONING if role == "root_cause" else Tier.FAST, f"rca_{role}"),
            gw.chat_model(Tier.REASONING, "rca_synth"),
            run_dir or new_run_dir(incident_id, self.settings.output_dir),
            fault=fault,
            max_replans=max_replans,
            on_event=on_event,
        )

    def quick_answer(self, question: str):
        """Gateway path for short questions: a live snapshot is the context, so the cache keys on state."""
        estate = [
            {
                "service": s.service,
                "state": s.state,
                "version": s.version,
                "healthy": f"{s.healthy_instances}/{s.instances}",
            }
            for s in self.store.estate_overview()
        ]
        system = (
            "You answer short questions about Larkspur Commerce production. Use only this live estate "
            f"snapshot. auth-service is the login service.\n{json.dumps(estate)}"
        )
        return self.gateway.ask(question, system=system, purpose="quick_answer")

    def triage_inbox(self) -> list[Incident]:
        """Task 1 in the product: turn the raw inbox into incidents."""
        inbox = self.store.inbox()
        tickets = extract_tickets(inbox, build_extractor(self.gateway.chat_model(Tier.FAST, "triage")))
        incidents = []
        for message, ticket in zip(inbox, tickets, strict=True):
            title = re.sub(r"^\[\w+\]\s*", "", message["subject"])
            incidents.append(
                self.store.save_incident(
                    Incident(
                        id=message["incident_id"],
                        title=title[:80],
                        severity=ticket.severity,
                        service=ticket.affected_service,
                        summary=ticket.summary,
                        sentiment=ticket.sentiment,
                        first_steps=ticket.action_items,
                        source_message_id=message["id"],
                        opened_at=message["received_at"],
                    )
                )
            )
        return incidents


@lru_cache
def get_services() -> Services:
    return Services()
