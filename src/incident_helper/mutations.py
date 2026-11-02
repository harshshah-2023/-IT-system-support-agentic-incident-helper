"""Task 1 live defence: what the LLM's `tool_calls` payload does when tool metadata changes."""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import StructuredTool, tool

from incident_helper.tools import check_service_status, get_recent_deployments, get_store

PROMPT = "payments-api looks slow. Check its health."


def _body(service: str) -> str:
    return get_store().service_status(service).model_dump_json()


def renamed_tool() -> StructuredTool:
    """Name is meaningless, docstring intact: the LLM selects from the description alone."""
    return StructuredTool.from_function(
        func=_body,
        name="xyz_123",
        description=check_service_status.description,
        args_schema=check_service_status.args_schema,
    )


def docstring_removal_error() -> str:
    """`@tool` refuses a function without a docstring, so that mutation fails at import time."""

    def check_service_status(service: str) -> str:
        return service

    try:
        tool(check_service_status)
    except ValueError as exc:
        return str(exc)
    return "no error raised"


def undocumented_tool() -> StructuredTool:
    """Forces the docstring-free case past `@tool` with a blank description, to see the LLM's behaviour."""
    return StructuredTool.from_function(func=_body, name="check_service_status", description=" ")


def describe(call_source: Any) -> list[dict[str, Any]]:
    return [{"name": c["name"], "args": c["args"]} for c in call_source.tool_calls]


def run_tool_mutations(model: BaseChatModel) -> dict[str, Any]:
    decoy = get_recent_deployments
    report: dict[str, Any] = {"prompt": PROMPT}
    for label, candidate in (
        ("baseline", check_service_status),
        ("renamed_to_xyz_123", renamed_tool()),
        ("docstring_blank", undocumented_tool()),
    ):
        reply = model.bind_tools([candidate, decoy]).invoke(PROMPT)
        report[label] = {
            "schema_description": candidate.description[:70],
            "tool_calls": describe(reply),
            "text": reply.text[:200],
        }
    report["docstring_deleted_under_@tool"] = docstring_removal_error()
    return report
