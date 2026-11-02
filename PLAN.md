# Plan and status

All eight stages are complete, except the LangSmith upload (see Open items).

| Stage | Outcome |
|---|---|
| 1 Requirements and architecture | Both briefs analysed. Requirement list in `docs/ASSESSMENT_MATRIX.md`. |
| 2 Working core | Simulated estate, tools, Task 1 extraction, Task 2 graph, context management, gateway. |
| 3 Retrieval | 40-page runbook, PDF/HTML/Markdown parsers, vector RAG, tree RAG, comparison. |
| 4 Agentic capabilities | Plan-and-execute RCA with `Send`, artifact gate, replanner, synthesizer; `deepagents` variant. |
| 5 Security | PII policies, rule and LLM threat detection, guardrail nodes, native HITL. |
| 6 Evaluation and operations | Golden dataset, structured judges, `evaluate()`, routing, fallback, cache, FinOps. |
| 7 Product integration | FastAPI backend and no-build web UI sharing the CLI's services. |
| 8 Full verification | Offline and live tests, all mutations live, browser journey. |

## Key decisions

- **Stack:** Python 3.13 via uv; LangChain 1.x, LangGraph 1.x; FAISS; FastEmbed local embeddings; FastAPI; a no-build ES-module UI, so a live defence never depends on a frontend toolchain.
- **Models:** OpenAI because that is the provided credential. `gpt-4o-mini` is the fast tier, `gpt-4o` the reasoning tier. Any `init_chat_model` provider can be swapped in through `.env`.
- **The Gateway is a `BaseChatModel`,** so agents get fallback and telemetry with no special handling.
- **Deterministic control around LLM reasoning:** models propose; code enforces policy, authorisation and arguments.
- **`langchain-community`** is being sunset but is still the home of the FAISS store and `SQLiteCache`, so it is used deliberately.

## Test seams

The `/tdd` skill asks for agreed seams. The brief asked to run through to completion without stopping, so these were chosen from its requirements:

| Seam | Catches | Misses |
|---|---|---|
| `OpsStore` public methods | State, rollback policy, audit | Agent decisions |
| `ModelGateway.ask` and `chat_model` with scripted models | Routing, fallback, cache, telemetry | Real provider behaviour |
| Compiled graphs with scripted models | Routing, interrupts, reducers, recovery | Real model choices |
| RAG on the real document and real embeddings | Parsing, threshold, citations | Generated text quality |
| HTTP API over a real graph | Approval flow and status codes | Browser rendering |
| Live tests and `scripts/ui_e2e.py` | Real model behaviour, full journey | Nondeterminism beyond one run |

The scripted model lives only in `tests/`. Production code has no fake LLM path.

## Open items

- **LangSmith:** no `LANGSMITH_API_KEY` is available. `evaluate()` runs for real in local mode. Add a key and run `incident task6` to create the dashboard experiments that Task 6 asks to show.
- **Runbook:** a practice document. If the reviewer assigns one, swap it per the README.
- **Git:** the repository is initialised but nothing has been committed.
