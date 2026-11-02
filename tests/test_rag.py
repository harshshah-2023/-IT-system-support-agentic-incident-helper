from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from incident_helper.config import ROOT
from incident_helper.embeddings import get_embeddings
from incident_helper.evals.dataset import GOLDEN, dataset_counts
from incident_helper.evals.judges import RUBRICS, JudgePanel
from incident_helper.evals.runner import HALLUCINATION, make_target
from incident_helper.rag.document import parse_document
from incident_helper.rag.schemas import INSUFFICIENT, RagResponse
from incident_helper.rag.tree import TreeNode, TreeRag, build_skeleton, summarise_tree
from incident_helper.rag.vector import VectorRag, chunk_sections
from support import ScriptedChatModel, structured

RUNBOOK = ROOT / "data" / "runbook"
PDF = RUNBOOK / "larkspur_runbook.pdf"


def test_document_has_at_least_15_pages():
    from pypdf import PdfReader

    assert len(PdfReader(str(PDF)).pages) >= 15


@pytest.mark.parametrize("name", ["larkspur_runbook.pdf", "larkspur_runbook.md", "larkspur_runbook.html"])
def test_every_format_parses_to_the_same_outline(name):
    sections = parse_document(RUNBOOK / name)
    assert len(sections) == len(parse_document(PDF))
    assert any("5.5" in s.title for s in sections)


def test_unsupported_format_is_rejected(tmp_path):
    bad = tmp_path / "x.docx"
    bad.write_text("x")
    with pytest.raises(ValueError, match="Unsupported"):
        parse_document(bad)


def test_chunks_keep_source_page_and_section():
    chunks = chunk_sections(parse_document(PDF), "runbook.pdf")
    assert all({"source", "page", "section"} <= set(c.metadata) for c in chunks)
    assert len({c.metadata["page"] for c in chunks}) > 15


@pytest.fixture(scope="module")
def index_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("idx")


def vector(index_dir, script=(), **kw):
    model = ScriptedChatModel(script=list(script))
    return VectorRag(PDF, get_embeddings(), model, index_dir, **kw), model


@pytest.mark.parametrize(
    "question",
    [
        "Which laptop should our team buy?",
        "What is your CEO's salary?",
        "Who won yesterday's cricket match?",
    ],
)
def test_off_topic_question_is_refused_without_calling_the_generator(index_dir, question):
    rag, model = vector(index_dir)
    result = rag.answer(question)
    assert result.answer == INSUFFICIENT
    assert result.generation_called is False and model.seen == []


def test_relevant_question_returns_citations_scores_and_confidence(index_dir):
    draft = structured("GroundedDraft", answer="Use patronictl [1].", cited_blocks=[1], confidence=0.9)
    rag, model = vector(index_dir, [draft])
    result = rag.answer("How do we switch to the backup database?")
    assert result.generation_called and result.grounded
    assert result.citations[0].page > 0 and result.citations[0].section
    assert 0 < result.similarity_score <= 1 and 0 < result.confidence <= 1
    assert "<block" in model.seen[0][-1].content


def test_answer_without_valid_citation_is_rejected(index_dir):
    draft = structured("GroundedDraft", answer="Trust me.", cited_blocks=[99], confidence=1.0)
    rag, _ = vector(index_dir, [draft])
    assert rag.answer("How do we switch to the backup database?").answer == INSUFFICIENT


def test_failover_passage_is_retrieved_from_real_document(index_dir):
    rag, _ = vector(index_dir)
    assert any(
        "patronictl failover" in doc.page_content
        for doc, _ in rag.retrieve("How do we fail over payments-db to the standby?")
    )


def small_tree():
    sections = parse_document(RUNBOOK / "larkspur_runbook.md")
    root = build_skeleton(sections, "Runbook")
    for node in root.walk():
        node.summary = node.title
    return root


def find(root: TreeNode, needle: str) -> TreeNode:
    return next(n for n in root.walk() if needle in n.title)


def test_skeleton_nests_headings_and_keeps_raw_text():
    root = small_tree()
    db = find(root, "5. Database")
    assert any(c.title.startswith("5.5") for c in db.children)
    assert len(root.children) == 12


def test_summaries_are_built_bottom_up():
    root = TreeNode(
        id="root",
        title="R",
        level=0,
        children=[
            TreeNode(
                id="1", title="A", level=1, children=[TreeNode(id="1.1", title="B", level=2, text="raw")]
            ),
        ],
    )
    model = ScriptedChatModel(
        script=[lambda m: AIMessage(content=f"sum:{'B' if 'Section: B' in m[-1].content else 'other'}")] * 3
    )
    summarise_tree(root, model, max_concurrency=1)
    assert root.children[0].children[0].summary == "sum:B"
    assert "sum:B" in model.seen[1][-1].content


def nav(*ids, why="because"):
    return structured("NavigationStep", reasoning=why, selected=list(ids))


def test_navigation_reads_exact_raw_text_and_reports_the_path():
    root = small_tree()
    chapter = find(root, "5. Database")
    section = find(chapter, "5.5")
    leaf = section.children[0]
    script = [
        nav(chapter.id),
        nav(section.id),
        nav(leaf.id),
        structured("GroundedDraft", answer="Do it [1].", cited_blocks=[1], confidence=0.8),
    ]
    model = ScriptedChatModel(script=script)
    result = TreeRag(root, "rb.pdf", model).answer("failover?")
    assert result.traversal[0].startswith("Visited: Root -> 5. Database")
    assert leaf.text[:50] in result.contexts[0]
    assert result.citations[0].section == leaf.title
    assert result.trace[0]["reasoning"] == "because"


def test_navigation_follows_two_branches_for_multi_section_questions():
    root = TreeNode(
        id="root",
        title="R",
        level=0,
        children=[
            TreeNode(id="1", title="A", level=1, text="alpha"),
            TreeNode(id="2", title="B", level=1, text="beta"),
        ],
    )
    script = [
        nav("1", "2"),
        structured("GroundedDraft", answer="x [1][2]", cited_blocks=[1, 2], confidence=0.9),
    ]
    result = TreeRag(root, "rb", ScriptedChatModel(script=script)).answer("q")
    assert len(result.traversal) == 2 and len(result.citations) == 2


def test_navigator_selecting_nothing_returns_insufficient_without_generation():
    model = ScriptedChatModel(script=[nav()])
    result = TreeRag(small_tree(), "rb", model).answer("laptop?")
    assert result.answer == INSUFFICIENT and not result.generation_called


def test_hallucinated_child_id_is_ignored():
    root = TreeNode(
        id="root", title="R", level=0, children=[TreeNode(id="1", title="A", level=1, text="alpha")]
    )
    result = TreeRag(root, "rb", ScriptedChatModel(script=[nav("99")])).answer("q")
    assert result.answer == INSUFFICIENT


def test_tree_module_has_no_vector_dependency():
    source = (ROOT / "src/incident_helper/rag/tree.py").read_text(encoding="utf-8")
    code = source.split('"""', 2)[2]
    assert not any(word in code.lower() for word in ("faiss", "embedding", "chroma"))


def test_golden_dataset_shape():
    assert len(GOLDEN) == 6
    counts = dataset_counts()
    assert counts["factual"] >= 4 and counts["multi_hop"] == 1 and counts["adversarial_unanswerable"] == 1


def test_judge_output_carries_score_and_reasoning():
    model = ScriptedChatModel(
        script=[structured("CountedResult", reasoning="claim 2 unsupported", total=2, passed=1)]
    )
    out = JudgePanel(model).evaluator("groundedness")(
        {"answer": "a", "contexts": ["c"]}, {"answer": "r"}, {"question": "q"}
    )
    assert (out["score"], out["comment"]) == (0.5, "claim 2 unsupported")


def test_groundedness_of_a_refusal_needs_no_llm_call():
    out = JudgePanel(ScriptedChatModel()).evaluator("groundedness")(
        {"answer": INSUFFICIENT, "contexts": []}, {}, {}
    )
    assert out["score"] == 1.0


def test_refusal_with_appended_fabrication_is_still_judged():
    model = ScriptedChatModel(script=[structured("CountedResult", reasoning="fabricated", total=1, passed=0)])
    out = JudgePanel(model).evaluator("groundedness")(
        {"answer": INSUFFICIENT + " The CFO approves.", "contexts": []}, {}, {}
    )
    assert out["score"] == 0.0


def test_correct_refusal_is_not_penalised_for_being_short():
    panel = JudgePanel(ScriptedChatModel())
    for key in ("answer_relevance", "conciseness", "tone"):
        assert (
            panel.evaluator(key)({"answer": INSUFFICIENT}, {"answer": INSUFFICIENT}, {"question": "q"})[
                "score"
            ]
            == 1.0
        )


def test_navigator_goes_back_when_the_draft_says_part_is_missing():
    root = TreeNode(
        id="root",
        title="R",
        level=0,
        children=[
            TreeNode(id="1", title="A", level=1, text="alpha"),
            TreeNode(id="2", title="B", level=1, text="beta"),
        ],
    )
    script = [
        nav("1"),
        structured(
            "GroundedDraft", answer="half [1]", cited_blocks=[1], confidence=0.9, fully_answered=False
        ),
        nav("2"),
        structured(
            "GroundedDraft", answer="all [1][2]", cited_blocks=[1, 2], confidence=0.9, fully_answered=True
        ),
    ]
    result = TreeRag(root, "rb", ScriptedChatModel(script=script)).answer("q")
    assert result.answer == "all [1][2]" and len(result.traversal) == 2


def test_hallucination_flag_changes_only_the_answer():
    target = make_target(
        lambda q: RagResponse(method="m", question=q, answer="Base."), inject_hallucination=True
    )
    assert target({"question": "q"})["answer"] == "Base." + HALLUCINATION


def test_all_required_rubrics_exist():
    assert {
        "correctness",
        "groundedness",
        "answer_relevance",
        "context_relevance",
        "conciseness",
        "tone",
    } <= set(RUBRICS)
