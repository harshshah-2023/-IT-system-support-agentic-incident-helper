"""Module 3A: traditional vector RAG with a hard relevance gate and citation enforcement."""

from __future__ import annotations

import re
import time
from pathlib import Path

from langchain_community.vectorstores import FAISS
from langchain_community.vectorstores.utils import DistanceStrategy
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_text_splitters import RecursiveCharacterTextSplitter

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


def _identity(score: float) -> float:
    return score


def chunk_sections(
    sections: list[Section], source: str, chunk_size: int = 900, chunk_overlap: int = 150
) -> list[Document]:
    """Split within sections, so that a chunk never straddles headings and keeps its exact page.

    900/150 fits a numbered runbook: long enough to hold a whole procedure step group, and the overlap
    keeps a step's context when a procedure crosses a boundary.
    """
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        add_start_index=True,
        separators=["\n\n", "\n", ". ", " ", ""],
    )
    chunks: list[Document] = []
    for section in sections:
        if not section.text:
            continue
        seed = Document(page_content=section.text)
        for piece in splitter.split_documents([seed]):
            offset = piece.metadata["start_index"]
            piece.page_content = f"{section.title}\n{piece.page_content}"
            piece.metadata = {
                "source": source,
                "section": section.title,
                "page": section.page_at(offset),
                "chunk": len(chunks),
            }
            chunks.append(piece)
    return chunks


class VectorRag:
    def __init__(
        self,
        document: Path,
        embeddings: Embeddings,
        model: BaseChatModel,
        index_dir: Path,
        threshold: float = 0.62,
        top_k: int = 5,
        chunk_size: int = 900,
        chunk_overlap: int = 150,
    ) -> None:
        self.document = document
        self.threshold = threshold
        self.top_k = top_k
        self._generator = model.with_structured_output(GroundedDraft)
        self._gateway = getattr(model, "gateway", None)
        self._chunk_args = (chunk_size, chunk_overlap)
        self.store = self._load_or_build(embeddings, index_dir)

    def _load_or_build(self, embeddings: Embeddings, index_dir: Path) -> FAISS:
        model = re.sub(r"\W+", "_", getattr(embeddings, "model_name", "embeddings"))
        tag = f"{fingerprint(self.document)}-{model}-{self._chunk_args[0]}-{self._chunk_args[1]}"
        path = index_dir / f"faiss-{tag}"
        options = {"distance_strategy": DistanceStrategy.MAX_INNER_PRODUCT, "relevance_score_fn": _identity}
        if path.exists():
            # Safe: this pickle is written only by the branch below, on this machine.
            return FAISS.load_local(str(path), embeddings, allow_dangerous_deserialization=True, **options)
        chunks = chunk_sections(parse_document(self.document), self.document.name, *self._chunk_args)
        # FastEmbed vectors are unit length, so inner product is cosine similarity and the score
        # is directly comparable to the threshold.
        store = FAISS.from_documents(chunks, embeddings, **options)
        store.save_local(str(path))
        return store

    def retrieve(self, question: str) -> list[tuple[Document, float]]:
        """Top-k chunks at or above the threshold, best first."""
        hits = self.store.similarity_search_with_relevance_scores(question, k=self.top_k)
        return [(doc, float(score)) for doc, score in hits if score >= self.threshold]

    def top_score(self, question: str) -> float:
        hits = self.store.similarity_search_with_relevance_scores(question, k=1)
        return float(hits[0][1]) if hits else 0.0

    def answer(self, question: str) -> RagResponse:
        started = time.perf_counter()
        mark = len(self._gateway.ledger.records) if self._gateway else 0
        hits = self.retrieve(question)
        if not hits:
            return RagResponse(
                method="vector",
                question=question,
                answer=INSUFFICIENT,
                similarity_score=round(self.top_score(question), 4),
                stats=RagStats(latency_ms=round((time.perf_counter() - started) * 1000, 2)),
            )

        texts = [doc.page_content for doc, _ in hits]
        labels = [f"{doc.metadata['section']}, page {doc.metadata['page']}" for doc, _ in hits]
        draft: GroundedDraft = self._generator.invoke(
            [
                SystemMessage(content=GROUNDED_SYSTEM),
                HumanMessage(content=f"Context:\n{format_blocks(texts, labels)}\n\nQuestion: {question}"),
            ]
        )
        valid = sorted({n for n in draft.cited_blocks if 1 <= n <= len(hits)})
        citations = [
            PageCitation(
                source=hits[n - 1][0].metadata["source"],
                page=hits[n - 1][0].metadata["page"],
                section=hits[n - 1][0].metadata["section"],
                quote=hits[n - 1][0].page_content[:200],
                similarity=round(hits[n - 1][1], 4),
            )
            for n in valid
        ]
        grounded = bool(citations) and INSUFFICIENT.lower() not in draft.answer.lower()
        records = self._gateway.ledger.records[mark:] if self._gateway else []
        return RagResponse(
            method="vector",
            question=question,
            answer=draft.answer if grounded else INSUFFICIENT,
            citations=citations if grounded else [],
            similarity_score=round(hits[0][1], 4),
            confidence=round(draft.confidence * hits[0][1], 4) if grounded else 0.0,
            grounded=grounded,
            generation_called=True,
            contexts=texts,
            stats=RagStats(
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
                input_tokens=sum(r.input_tokens for r in records),
                output_tokens=sum(r.output_tokens for r in records),
                llm_calls=len(records),
                retrieved=len(hits),
            ),
        )
