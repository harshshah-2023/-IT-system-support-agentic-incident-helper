"""Module 3B: vectorless tree RAG (PageIndex style). No embeddings and no vector store anywhere.

Index time: sections become a nested JSON tree, summarised bottom-up by the LLM.
Query time: the LLM reads child summaries and reasons its way down, possibly along several branches,
then answers from the exact raw text of the sections it reached.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from incident_helper.rag.document import Section, fingerprint, parse_document
from incident_helper.rag.schemas import (
    GROUNDED_SYSTEM,
    INSUFFICIENT,
    GroundedDraft,
    PageCitation,
    RagResponse,
    RagStats,
    format_blocks,
)

MAX_BRANCHES = 4
MAX_LEAVES = 10
MAX_RAW_CHARS = 6000
MAX_EXTRA_PASSES = 2
RETRY_BELOW_CONFIDENCE = 0.7


class TreeNode(BaseModel):
    id: str
    title: str
    level: int
    page_start: int = 1
    page_end: int = 1
    summary: str = ""
    text: str = ""
    children: list[TreeNode] = Field(default_factory=list)

    def walk(self) -> list[TreeNode]:
        return [self, *(node for child in self.children for node in child.walk())]


class NavigationStep(BaseModel):
    """Which children of the current node could contain the answer."""

    reasoning: str = Field(description="One or two sentences explaining the choice")
    selected: list[str] = Field(
        description="IDs of the children to open. Choose several when the question spans topics. "
        "Empty when none are relevant."
    )
    read_current_section: bool = Field(
        default=False, description="True when the current section's own text should also be read"
    )


def build_skeleton(sections: list[Section], title: str) -> TreeNode:
    """Turn the flat heading list into a nested tree. Pure, no LLM."""
    root = TreeNode(id="root", title=title, level=0)
    stack = [root]
    counters: dict[str, int] = {}
    for section in sections:
        while stack[-1].level >= section.level:
            stack.pop()
        parent = stack[-1]
        counters[parent.id] = counters.get(parent.id, 0) + 1
        node = TreeNode(
            id=f"{'' if parent is root else parent.id + '.'}{counters[parent.id]}",
            title=section.title,
            level=section.level,
            page_start=section.page_start,
            page_end=section.page_end,
            text=section.text,
        )
        parent.children.append(node)
        stack.append(node)
    return root


def summarise_tree(root: TreeNode, model: BaseChatModel, max_concurrency: int = 4) -> None:
    """Fill summaries from the deepest level upward, so that parents summarise their children."""
    by_level: dict[int, list[TreeNode]] = {}
    for node in root.walk():
        by_level.setdefault(node.level, []).append(node)
    for level in sorted(by_level, reverse=True):
        nodes = by_level[level]
        prompts = []
        for node in nodes:
            parts = [node.text[:3000]] if node.text else []
            parts += [f"- {child.title}: {child.summary}" for child in node.children]
            prompts.append(
                [
                    HumanMessage(
                        content=(
                            "Summarise this runbook section in at most 40 words for a table-of-contents index. "
                            "Name the concrete topics, services, procedures and thresholds covered, so that a "
                            f"reader knows whether to open it.\n\nSection: {node.title}\n\n"
                            + "\n".join(parts)
                        )
                    )
                ]
            )
        replies = model.batch(prompts, config={"max_concurrency": max_concurrency})
        for node, reply in zip(nodes, replies, strict=True):
            node.summary = reply.text.strip()


def load_or_build_tree(document: Path, model: BaseChatModel, index_dir: Path) -> TreeNode:
    path = index_dir / f"tree-{fingerprint(document)}.json"
    if path.exists():
        return TreeNode.model_validate_json(path.read_text(encoding="utf-8"))
    root = build_skeleton(parse_document(document), document.stem.replace("_", " ").title())
    summarise_tree(root, model)
    path.write_text(json.dumps(root.model_dump(), indent=1), encoding="utf-8")
    return root


class TreeRag:
    def __init__(
        self, root: TreeNode, source: str, navigator: BaseChatModel, generator: BaseChatModel | None = None
    ) -> None:
        self.root = root
        self.source = source
        self._navigate = navigator.with_structured_output(NavigationStep)
        self._generate = (generator or navigator).with_structured_output(GroundedDraft)
        self._gateway = getattr(navigator, "gateway", None)

    def _choose(self, question: str, node: TreeNode, path: str, already_read: list[str]) -> NavigationStep:
        listing = "\n".join(
            f"- id={child.id} | {child.title} (pages {child.page_start}-{child.page_end}): {child.summary}"
            for child in node.children
        )
        retry = ""
        if already_read:
            retry = (
                "\n\nThese sections were already read and did NOT fully answer the question: "
                f"{'; '.join(already_read)}. Choose where the missing part most likely is."
            )
        return self._navigate.invoke(
            [
                SystemMessage(
                    content=(
                        "You navigate a document's table of contents like an expert reader. Select every child "
                        f"that may be needed to answer ALL parts of the question, at most {MAX_BRANCHES}. Summaries "
                        "are short, so favour recall when a child is plausible. A question joining two topics "
                        "usually needs two branches. Select none only if the document clearly does not cover "
                        "the question's subject."
                    )
                ),
                HumanMessage(
                    content=f"Question: {question}{retry}\n\nYou are at: {path}\n\nChildren:\n{listing}"
                ),
            ]
        )

    def _descend(
        self,
        question: str,
        node: TreeNode,
        path: list[str],
        leaves: list[TreeNode],
        trace: list[dict],
        already_read: list[str],
    ) -> list[str]:
        """Depth-first traversal. Returns the human-readable path of each branch followed."""
        label = " -> ".join(path)
        step = self._choose(question, node, label, already_read)
        children = {child.id: child for child in node.children}
        chosen = [children[i] for i in step.selected if i in children][:MAX_BRANCHES]
        trace.append({"at": label, "reasoning": step.reasoning, "selected": [c.title for c in chosen]})
        if step.read_current_section and node.text and node not in leaves:
            leaves.append(node)
        visited: list[str] = []
        for child in chosen:
            if len(leaves) >= MAX_LEAVES:
                break
            branch = [*path, child.title]
            if child.children:
                visited += self._descend(question, child, branch, leaves, trace, already_read)
            elif child not in leaves:
                leaves.append(child)
                visited.append(" -> ".join(branch))
        return visited or ([label] if step.read_current_section else [])

    def _draft(self, question: str, leaves: list[TreeNode]) -> GroundedDraft:
        texts = [leaf.text[:MAX_RAW_CHARS] for leaf in leaves]
        labels = [f"{leaf.title}, pages {leaf.page_start}-{leaf.page_end}" for leaf in leaves]
        return self._generate.invoke(
            [
                SystemMessage(content=GROUNDED_SYSTEM),
                HumanMessage(content=f"Context:\n{format_blocks(texts, labels)}\n\nQuestion: {question}"),
            ]
        )

    def answer(self, question: str) -> RagResponse:
        started = time.perf_counter()
        mark = len(self._gateway.ledger.records) if self._gateway else 0
        leaves: list[TreeNode] = []
        trace: list[dict] = []
        visited = self._descend(question, self.root, ["Root"], leaves, trace, [])

        def stats() -> RagStats:
            records = self._gateway.ledger.records[mark:] if self._gateway else []
            return RagStats(
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
                input_tokens=sum(r.input_tokens for r in records),
                output_tokens=sum(r.output_tokens for r in records),
                llm_calls=len(records),
                retrieved=len(leaves),
            )

        traversal = [f"Visited: {path}" for path in visited] or ["Visited: Root"]
        if not leaves:
            return RagResponse(
                method="vectorless",
                question=question,
                answer=INSUFFICIENT,
                traversal=traversal,
                trace=trace,
                stats=stats(),
            )

        draft = self._draft(question, leaves)
        for _ in range(MAX_EXTRA_PASSES):
            # The reader's own judgement drives the retry: if what it opened was not enough, or it
            # is unsure, it goes back to the contents page knowing what it has already read.
            complete = draft.fully_answered and draft.confidence >= RETRY_BELOW_CONFIDENCE
            if complete and INSUFFICIENT.lower() not in draft.answer.lower():
                break
            before = len(leaves)
            visited += self._descend(
                question, self.root, ["Root"], leaves, trace, [leaf.title for leaf in leaves]
            )
            if len(leaves) == before:
                break
            draft = self._draft(question, leaves)
        traversal = [f"Visited: {path}" for path in visited]
        texts = [leaf.text[:MAX_RAW_CHARS] for leaf in leaves]
        valid = sorted({n for n in draft.cited_blocks if 1 <= n <= len(leaves)})
        citations = [
            PageCitation(
                source=self.source,
                page=leaves[n - 1].page_start,
                section=leaves[n - 1].title,
                quote=leaves[n - 1].text[:200],
            )
            for n in valid
        ]
        grounded = bool(citations) and INSUFFICIENT.lower() not in draft.answer.lower()
        return RagResponse(
            method="vectorless",
            question=question,
            answer=draft.answer if grounded else INSUFFICIENT,
            citations=citations if grounded else [],
            confidence=round(draft.confidence, 4) if grounded else 0.0,
            grounded=grounded,
            generation_called=True,
            traversal=traversal,
            trace=trace,
            contexts=texts,
            stats=stats(),
        )
