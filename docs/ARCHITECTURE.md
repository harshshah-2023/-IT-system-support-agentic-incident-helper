# Architecture

## Overview

```
 Web UI (static ES modules)        CLI (typer)
          |  REST + SSE                |
          v                            v
       api.py  ------>  services.py  <------
                  (composition root)
     .-----------+-----------+-----------+------------.
     v           v           v           v            v
 extraction   agents/      agents/      rag/        evals/
  (Task 1)   react, guarded  deep    vector, tree   judges, runner
             (Tasks 2, 5)  (Task 4)  (Task 3)       (Task 6)
     |           |           |           |            |
     '-----------+-----+-----+-----------+------------'
                       v
              gateway/ (Task 7)              security/   tools.py -> ops/store.py
       routing, fallback, cache, FinOps      PII, threats     simulated estate (SQLite)
                       v
              OpenAI (any init_chat_model provider)
```

## Components

| Module | Responsibility |
|---|---|
| `config.py` | Typed settings. Secrets come only from the environment or `.env`. |
| `domain.py` | Shared Pydantic models and enums. |
| `ops/store.py` | Simulated estate in SQLite. Health derives from the running version, so a rollback visibly changes later readings. Holds rollback policy and the audit log. |
| `tools.py` | The `@tool` functions, including the high-risk `rollback_deployment`. |
| `extraction.py` | Task 1: `CustomerTicket` extraction, batching, streaming, manual tool loop. |
| `mutations.py` | Task 1 defence: tool name and docstring experiments. |
| `llm_context.py` | Message trimming with orphan repair, token counting, sliding-window summaries. |
| `agents/react.py` | Task 2 graph and the reusable LLM node. |
| `agents/guarded.py` | Task 5 graph, used by the product copilot. |
| `agents/deep.py`, `workspace.py` | Task 4 plan-and-execute RCA and the file workspace. |
| `agents/deepagent_demo.py` | The same pattern on the `deepagents` harness. |
| `rag/` | Rendering, parsing, chunking, vector RAG, tree RAG, shared schemas. |
| `security/` | PII sanitiser and dual-layer threat detection. |
| `evals/` | Golden dataset, judges, `evaluate()` runner. |
| `gateway/` | Model selection, router, key pool, cache, pricing, telemetry. |
| `services.py` | Wires everything once for both CLI and API. |
| `api.py`, `static/` | HTTP surface and UI. |

## Guarded copilot graph (Tasks 2 and 5)

```
START -> input_guardrail --blocked--> END
              |
        [compress_history]
              v
            agent <---------------------------------------------.
              |-- tool calls --> tool_guard -- refused ---------|
              |                    |-- safe ------> tools --> tool_output_guard
              |                    '-- high risk -> human_approval
              |                                       |-- reject --> agent
              |                                       '-- approve/edit --> tools
              '-- no calls --> action_check -- corrective turn --> agent
                                    '--> output_guardrail --> END
```

`GuardedState` extends `AgentState`:

| Key | Reducer | Purpose |
|---|---|---|
| `messages` | `add_messages` | History; same id replaces, which enables redaction and edits |
| `incident_id`, `summary` | replace | Thread context |
| `blocked`, `block_reason` | replace | Guardrail outcome |
| `action_nudged`, `citation_nudged` | replace | One corrective turn each, per user turn |
| `approvals` | replace | Tool-call id to approver and argument digest |
| `approval_log`, `security_events` | `operator.add` | Append-only history |

Three layers protect a rollback: `tool_guard` validates arguments and policy before a human is asked, `human_approval` calls `interrupt()` and checks the approver, and the tool itself refuses unless an approval digest matches the exact service and version.

Each incident ID is one `thread_id`, so sessions are isolated and persist in SQLite across restarts.

## RCA graph (Task 4)

```
START -> planner -> Send(worker) x N -> artifact_gate -+-> Send(worker)   next ready subtasks
                                                       +-> replanner -> Send(worker)   failed, retries left
                                                       '-> synthesizer -> END
```

The planner returns a validated `InvestigationPlan`: exactly three subtasks, one per role, with an acyclic dependency graph. Workers are `create_agent` sub-agents with isolated prompts and toolsets. They share only `.md` files in `output/<run>/`. State carries status records, never findings. The gate checks the disk, because a worker claiming success is not proof, and this has caught a real worker that skipped saving. When retries run out the synthesizer still writes a report, labelled incomplete, and status becomes `degraded`.

## RAG (Task 3)

Both modules start from `rag.document.parse_document`, which turns PDF outlines, Markdown headings or HTML headings into one `Section` list with page marks.

| | Vector (3A) | Vectorless (3B) |
|---|---|---|
| Index | 140 chunks in FAISS, 384-dimension local embeddings | 163-node JSON tree with LLM summaries |
| Query | Top 8 by cosine, gated at 0.62 | LLM descends child summaries, up to 2 extra passes |
| Reject | No chunk at threshold: no generation call | Navigator selects nothing: no generation call |
| Evidence | Page, section, similarity | Page, section, traversal path, reasoning |

The 0.62 threshold is calibrated, not guessed: on eight in-domain and eight off-topic questions, off-topic top scores peaked at 0.607 and in-domain scores bottomed out at 0.634.

## Gateway (Task 7)

```
ask(): exact cache -> semantic cache -> route -> primary[key 1..n] -> fallback[key 1..n] -> telemetry
```

- **Routing:** nearest labelled utterance by local embedding, so routing has no LLM cost.
- **Fallback:** standard `.with_fallbacks()`. `_AttemptTracker` is a callback that observes which candidate served.
- **Retry:** only transient errors retry. Auth failures go straight to the fallback.
- **Cache:** exact lookup comes first, using LangChain `InMemoryCache` over `SQLiteCache`. Semantic matches must share entities, so "Is login up?" cannot answer "Is payments up?". The namespace includes the system context, so a change in estate state cannot serve a stale answer. Tool-bound calls are never cached.
- **Keys:** comma-separated keys rotate round-robin with a cool-down, logged only as a hash.

## Data stores

All under `var/`, gitignored: `ops.sqlite` (estate, incidents, audit), `checkpoints.sqlite` (LangGraph), `llm_cache.sqlite`, `index/` (FAISS and tree), `telemetry.jsonl`, `evals/`.

## Security model

Logs, alerts, retrieved text and user input are all untrusted. Sanitisation happens at input, tool arguments, tool output and final output. A canary in the system prompt detects leaks. The fixture logs contain a real injection attempt and credentials, so these paths are exercised on ordinary investigations. Nothing runs arbitrary commands; the only write is a validated change to simulated state.

## Known limits

- Rule-based threat detection is a first layer: it can have false positives on unusual phrasing, and the semantic layer depends on the model.
- Model behaviour varies between runs, so scores move a little.
- `orders-api` and similar services have one version profile, so only three services can be rolled back.
- `pricing.py` has list prices for two models. Others report an unknown cost rather than a guess.
