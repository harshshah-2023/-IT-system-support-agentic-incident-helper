import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import ValidationError

from incident_helper import tools as T
from incident_helper.agents.deep import InvestigationPlan, RcaWorkflow
from incident_helper.agents.workspace import Workspace
from support import ScriptedChatModel, structured, tool_call

CFG = {"configurable": {"thread_id": "rca"}, "recursion_limit": 40}
Q = {"question": "What caused last night's outage?", "incident_id": "INC-1042"}


@pytest.fixture(autouse=True)
def _store(store):
    T.use_store(store)


def plan_reply(chain=False):
    return structured(
        "InvestigationPlan",
        rationale="r",
        subtasks=[
            {"id": "timeline", "objective": "t", "depends_on": []},
            {"id": "root_cause", "objective": "r", "depends_on": ["timeline"] if chain else []},
            {"id": "prevention", "objective": "p", "depends_on": ["root_cause"] if chain else []},
        ],
    )


def report_reply(text="# Report\nbody"):
    return structured("Report", title="T", markdown=text)


def worker_script(role, filename=None, reads=()):
    steps = [tool_call("read_draft", f"rd{i}", filename=r) for i, r in enumerate(reads)]
    steps.append(
        tool_call("write_draft", "wd", filename=filename or f"{role}.md", content=f"{role} findings")
    )
    steps.append(AIMessage(content="Saved."))
    return steps


def workflow(tmp_path, workers, planner=None, synth=None, **kw):
    models = {role: ScriptedChatModel(script=script) for role, script in workers.items()}
    planner = planner or ScriptedChatModel(script=[plan_reply()])
    synth = synth or ScriptedChatModel(script=[report_reply()])
    return RcaWorkflow(planner, lambda r: models[r], synth, tmp_path / "run", **kw), models, synth


ALL = lambda: {r: worker_script(r) for r in ("timeline", "root_cause", "prevention")}  # noqa: E731


def test_plan_schema_demands_one_subtask_per_role():
    with pytest.raises(ValidationError):
        InvestigationPlan(rationale="r", subtasks=[{"id": "timeline", "objective": "x"}] * 3)


def test_plan_schema_rejects_cycles():
    with pytest.raises(ValidationError, match="cycle"):
        InvestigationPlan(
            rationale="r",
            subtasks=[
                {"id": "timeline", "objective": "x", "depends_on": ["prevention"]},
                {"id": "root_cause", "objective": "x"},
                {"id": "prevention", "objective": "x", "depends_on": ["timeline"]},
            ],
        )


def test_planner_runs_before_any_worker_and_report_is_written(tmp_path):
    events = []
    wf, _, _ = workflow(tmp_path, ALL(), on_event=lambda kind, data: events.append(kind))
    result = wf.compile().invoke(Q, CFG)
    assert events[0] == "plan"
    assert result["status"] == "complete"
    assert (tmp_path / "run" / "incident_report.md").read_text().startswith("# Report")


def test_findings_stay_on_disk_not_in_graph_state(tmp_path):
    wf, _, _ = workflow(tmp_path, ALL())
    result = wf.compile().invoke(Q, CFG)
    assert "root_cause findings" not in str(result)
    assert (tmp_path / "run" / "root_cause.md").read_text() == "root_cause findings"


def test_synthesizer_reads_the_files_from_disk(tmp_path):
    wf, _, synth = workflow(tmp_path, ALL())
    wf.compile().invoke(Q, CFG)
    assert "prevention findings" in synth.seen[0][-1].content


def test_dependencies_run_in_order_and_files_are_shared(tmp_path):
    workers = {
        "timeline": worker_script("timeline"),
        "root_cause": worker_script("root_cause", reads=["timeline.md"]),
        "prevention": worker_script("prevention", reads=["root_cause.md"]),
    }
    wf, models, _ = workflow(tmp_path, workers, planner=ScriptedChatModel(script=[plan_reply(chain=True)]))
    assert wf.compile().invoke(Q, CFG)["status"] == "complete"
    shown = [m.content for m in models["root_cause"].seen[-1] if isinstance(m, ToolMessage)]
    assert "timeline findings" in shown[0]


def test_workers_have_isolated_toolsets(tmp_path):
    wf, models, _ = workflow(tmp_path, ALL())
    wf.compile().invoke(Q, CFG)
    names = {r: {t.name for t in m.bound[0]} for r, m in models.items()}
    assert "get_service_metrics" in names["root_cause"] and "get_service_metrics" not in names["prevention"]
    assert "get_incident_history" in names["prevention"] and "get_incident_history" not in names["timeline"]


def test_worker_exception_is_replanned_and_retried(tmp_path):
    workers = {
        "timeline": worker_script("timeline"),
        "prevention": worker_script("prevention"),
        "root_cause": [RuntimeError("provider down"), *worker_script("root_cause")],
    }
    replan = structured("Replan", reasoning="transient", revised_objective="retry")
    wf, _, _ = workflow(tmp_path, workers, planner=ScriptedChatModel(script=[plan_reply(), replan]))
    result = wf.compile().invoke(Q, CFG)
    assert result["status"] == "complete" and result["replans"] == 1
    assert any("provider down" in e for e in result["errors"])


def test_file_deleted_mid_run_is_detected_and_regenerated(tmp_path):
    workers = ALL()
    workers["timeline"] = [*worker_script("timeline"), *worker_script("timeline")]
    replan = structured("Replan", reasoning="file lost", revised_objective="rebuild")
    wf, _, _ = workflow(tmp_path, workers, planner=ScriptedChatModel(script=[plan_reply(), replan]))
    graph = wf.compile(pause_before_gate=True)
    graph.invoke(Q, CFG)
    (tmp_path / "run" / "timeline.md").unlink()
    while graph.get_state(CFG).next:
        result = graph.invoke(None, CFG)
    assert any("timeline.md is missing" in e for e in result["errors"])
    assert result["status"] == "complete" and (tmp_path / "run" / "timeline.md").exists()


def test_exhausted_retries_surface_a_degraded_report(tmp_path):
    workers = ALL()
    workers["root_cause"] = [RuntimeError("down"), RuntimeError("down"), RuntimeError("down")]
    replan = structured("Replan", reasoning="x", revised_objective="y")
    wf, _, synth = workflow(
        tmp_path, workers, planner=ScriptedChatModel(script=[plan_reply(), replan, replan])
    )
    result = wf.compile().invoke(Q, CFG)
    assert result["status"] == "degraded"
    text = (tmp_path / "run" / "incident_report.md").read_text()
    assert text.startswith("> **Incomplete report.**") and "root_cause.md" in text
    assert "root_cause.md" in synth.seen[0][-1].content


def test_fault_injected_tool_exception_reaches_the_worker_as_an_error(tmp_path):
    workers = ALL()
    workers["root_cause"] = [
        tool_call("check_service_status", "c", service="payments-api"),
        *worker_script("root_cause"),
    ]
    wf, _, _ = workflow(tmp_path, workers, fault="root_cause", max_replans=0)
    result = wf.compile().invoke(Q, CFG)
    assert result["status"] == "degraded"
    assert any("Injected fault" in e for e in result["errors"])


def test_evidence_is_sanitised_for_workers(tmp_path):
    workers = ALL()
    workers["root_cause"] = [
        tool_call("get_service_logs", "c", service="payments-api"),
        *worker_script("root_cause"),
    ]
    wf, models, _ = workflow(tmp_path, workers)
    wf.compile().invoke(Q, CFG)
    log = next(m for m in models["root_cause"].seen[-1] if isinstance(m, ToolMessage))
    assert "Pr0d-S3cret" not in log.content and "SECURITY NOTICE" in log.content


def test_workspace_blocks_path_traversal(tmp_path):
    ws = Workspace(tmp_path / "w")
    for name in ("../evil.md", "a/b.md", "x.txt"):
        with pytest.raises(ValueError):
            ws.write(name, "x")


def test_read_of_missing_draft_lists_what_exists(tmp_path):
    ws = Workspace(tmp_path / "w")
    ws.write("a.md", "x")
    with pytest.raises(FileNotFoundError, match="a.md"):
        ws.read("b.md")


def test_human_message_import_is_used():
    assert HumanMessage(content="x").type == "human"
