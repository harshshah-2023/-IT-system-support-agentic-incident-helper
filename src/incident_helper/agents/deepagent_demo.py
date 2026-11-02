"""The same plan-delegate-write pattern using the `deepagents` harness instead of a hand-built graph.

The main RCA uses an explicit StateGraph because the assessment needs inspectable planner, gate and
synthesizer nodes. This shows the higher-level alternative: planning tools, a sandboxed filesystem and
sub-agent delegation come built in.
"""

from __future__ import annotations

from typing import Any

from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from langchain_core.messages import HumanMessage

from incident_helper.gateway import Tier


def run(services: Any, question: str) -> str:
    from incident_helper.services import rag_tools

    root = services.settings.output_dir / "deepagent"
    root.mkdir(parents=True, exist_ok=True)
    runbook_expert = {
        "name": "runbook-expert",
        "description": "Answers questions from the operations runbook, with citations.",
        "system_prompt": "Answer only from the runbook tools and cite the section and page.",
        "tools": rag_tools(services.rag)[:1],
    }
    agent = create_deep_agent(
        model=services.gateway.chat_model(Tier.REASONING, "deepagent"),
        system_prompt=(
            "You are an incident documentation lead. The workspace holds only your own output, never the "
            "runbook, so do not search files for facts. Get every fact by delegating to runbook-expert "
            "with the task tool, then write the requested file."
        ),
        subagents=[runbook_expert],
        backend=FilesystemBackend(root_dir=str(root), virtual_mode=True),
    )
    result = agent.invoke({"messages": [HumanMessage(content=question)]}, {"recursion_limit": 40})
    return f"{result['messages'][-1].text}\n\nFiles: {sorted(p.name for p in root.iterdir())}"
