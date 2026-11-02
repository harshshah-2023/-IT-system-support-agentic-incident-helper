"""Runs `langsmith.evaluate()` on both RAG modules and writes results the UI and CLI can show.

With a LANGSMITH_API_KEY the run uploads a dataset and an experiment per method. Without one, the same
`evaluate()` call runs on local examples with `upload_results=False`, and the result is labelled local.
"""

from __future__ import annotations

import json
import os
import statistics
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from langsmith import Client
from langsmith.evaluation import evaluate
from langsmith.run_helpers import tracing_context
from langsmith.schemas import Example

from incident_helper.config import Settings, get_settings
from incident_helper.evals.dataset import DATASET_NAME, GOLDEN, upload_dataset
from incident_helper.evals.judges import JudgePanel
from incident_helper.rag.schemas import RagResponse

HALLUCINATION = " Additionally, every failover must be authorised by the CFO, who enters code 7741 on the Mumbai mainframe."

Target = Callable[[str], RagResponse]


def make_target(
    answer: Target, inject_hallucination: bool = False
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def rag_output(inputs: dict[str, Any]) -> dict[str, Any]:
        response = answer(inputs["question"])
        text = response.answer + (HALLUCINATION if inject_hallucination else "")
        return {
            "answer": text,
            "contexts": response.contexts,
            "citations": [c.model_dump() for c in response.citations],
        }

    rag_output.__name__ = f"{'tampered_' if inject_hallucination else ''}rag"
    return rag_output


def _local_examples() -> list[Example]:
    now = datetime.now(UTC)
    return [
        Example(
            id=uuid4(),
            dataset_id=uuid4(),
            created_at=now,
            inputs={"question": r["question"]},
            outputs={"answer": r["answer"]},
            metadata={"kind": r["kind"]},
        )
        for r in GOLDEN
    ]


def _collect(results: Any) -> dict[str, Any]:
    rows, scores = [], {}
    for item in results:
        run, example = item["run"], item["example"]
        row = {
            "question": example.inputs["question"],
            "kind": (example.metadata or {}).get("kind"),
            "answer": (run.outputs or {}).get("answer", ""),
            "error": run.error,
            "scores": {},
        }
        for evaluation in item["evaluation_results"]["results"]:
            if evaluation.score is None:
                continue
            row["scores"][evaluation.key] = {
                "score": float(evaluation.score),
                "reasoning": evaluation.comment,
            }
            scores.setdefault(evaluation.key, []).append(float(evaluation.score))
        rows.append(row)
    return {"rows": rows, "means": {k: round(statistics.fmean(v), 3) for k, v in scores.items()}}


def run_experiment(
    method: str,
    answer: Target,
    panel: JudgePanel,
    settings: Settings | None = None,
    inject_hallucination: bool = False,
    only: list[str] | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    uploaded = bool(settings.langsmith_api_key)
    evaluators = [panel.evaluator(k) for k in only] if only else panel.all()
    prefix = f"{method}-rag{'-hallucination' if inject_hallucination else ''}"
    target = make_target(answer, inject_hallucination)

    if uploaded:
        os.environ.setdefault("LANGSMITH_API_KEY", settings.langsmith_api_key.get_secret_value())
        client = Client(api_key=settings.langsmith_api_key.get_secret_value())
        upload_dataset(client)
        results = evaluate(
            target,
            data=DATASET_NAME,
            evaluators=evaluators,
            experiment_prefix=prefix,
            max_concurrency=2,
            client=client,
            metadata={"method": method, "tampered": inject_hallucination},
        )
    else:
        os.environ["LANGSMITH_TRACING"] = "false"
        client = Client(api_key="local-unused", auto_batch_tracing=False)
        with tracing_context(enabled=False):
            results = evaluate(
                target,
                data=_local_examples(),
                evaluators=evaluators,
                experiment_prefix=prefix,
                max_concurrency=2,
                client=client,
                upload_results=False,
            )
    summary = {
        "method": method,
        "experiment": getattr(results, "experiment_name", prefix) if uploaded else prefix,
        "tampered": inject_hallucination,
        "destination": "langsmith" if uploaded else "local (no LANGSMITH_API_KEY)",
        "ran_at": datetime.now(UTC).isoformat(timespec="seconds"),
        **_collect(results),
    }
    _persist(summary, settings.var_dir / "evals")
    return summary


def _persist(summary: dict[str, Any], folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{summary['method']}{'-tampered' if summary['tampered'] else ''}.json"
    (folder / name).write_text(json.dumps(summary, indent=1), encoding="utf-8")


def load_results(settings: Settings | None = None) -> list[dict[str, Any]]:
    folder = (settings or get_settings()).var_dir / "evals"
    return (
        [json.loads(p.read_text(encoding="utf-8")) for p in sorted(folder.glob("*.json"))]
        if folder.exists()
        else []
    )
