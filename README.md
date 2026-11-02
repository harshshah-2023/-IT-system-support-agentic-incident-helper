# IT Incident Response Helper

An agentic assistant for production incidents. It triages alerts and complaints, investigates with live tools, answers from the runbook two ways, plans a multi-agent root cause analysis, pauses risky rollbacks for a human, grades itself, and routes every model call through a gateway.

Built for the Agentic AI team assessment (Project 5, IT operations). All seven tasks run from the CLI independently of the UI. The company, services and data are fictional, and all write actions affect a simulated estate only.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12 or 3.13.

```powershell
uv sync
copy .env.example .env      # then set OPENAI_API_KEY and OPENAI_FALLBACK_API_KEY
uv run incident index       # builds the vector index and the summarised tree (uses the LLM)
uv run incident serve       # http://127.0.0.1:8000
```

`requirements.txt` is exported from `uv.lock` for pip users. The first run downloads a small local embedding model (BAAI/bge-small-en-v1.5).

## Using the app

Open the UI and press **Triage 5 messages**. Open INC-1042 and ask "Why are payments slow after last night's release?". If the agent proposes a rollback, approve, edit or reject it from the banner, then run the root cause investigation. **Reset simulation** restores the estate.

## Assessment commands

| Task | Command | Live mutation |
|---|---|---|
| 1 Extraction | `incident task1` | `incident tool-mutation` |
| 2 ReAct graph | `incident task2` | `incident task2 --stream-mode values` |
| 3 Dual RAG | `incident task3 "<question>"`, `incident task3-table` | any new question |
| 4 Deep agent | `incident task4` | `--delete-file timeline.md`, `--fail root_cause` |
| 5 Guardrails and HITL | `incident task5 --decision approve` | `--decision edit`, `edit-state`, `reject` |
| 6 Evaluation | `incident task6` | `--inject-hallucination` |
| 7 Gateway | `incident task7` | `incident fallback-test`, or corrupt the key in `.env` |

Prefix with `uv run`. Exact defence steps are in [docs/LIVE_DEFENSE.md](docs/LIVE_DEFENSE.md).

Other commands: `reset`, `ask`, `inspect <thread>` (checkpoint history), `render-runbook`, `eval-upload --tag v1`, `deep-agent`.

## Using a reviewer's document

Set `RUNBOOK_PATH` in `.env` to a `.pdf`, `.md` or `.html` file, then run `incident index`. Indexes are keyed by file hash, so both retrieval modules rebuild from the same source. Rewrite `evals/dataset.py` for the new document and re-check `RAG_SCORE_THRESHOLD`, which was calibrated on this runbook.

## Tests

```powershell
uv run pytest -m "not live"   # offline, scripted models
uv run pytest -m live         # real models, costs a few cents
uv run --with playwright python scripts/ui_e2e.py   # browser journey against a running server
```

## Documentation

- [docs/ASSESSMENT_MATRIX.md](docs/ASSESSMENT_MATRIX.md): every requirement with status, code and evidence
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): components, graphs and data flow
- [docs/CODE_WALKTHROUGH.md](docs/CODE_WALKTHROUGH.md): how the important code works
- [docs/DOCS_MAPPING.md](docs/DOCS_MAPPING.md): official documentation patterns used
- [docs/REBUILD_GUIDE.md](docs/REBUILD_GUIDE.md): recreating the system step by step
- [docs/LIVE_DEFENSE.md](docs/LIVE_DEFENSE.md): the seven mutations
- [docs/evidence/](docs/evidence/): captured logs from real runs
