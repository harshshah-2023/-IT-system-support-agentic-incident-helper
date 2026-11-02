# Documentation Mapping

This maps each major pattern in the codebase to the official documentation page that teaches it, the file where we adapt it, and how and why we differ.

## How URLs were checked

Each docs.langchain.com URL below was fetched on 2026-11-02 and returned a real page; the "Confirmed" column states what the fetched page actually covers. URLs that were only seen in a web-search result, or that returned 404 when fetched, are marked **not confirmed** and no claim is made about their content beyond what the search snippet said.

Docs change quickly. The LangChain v1 docs now steer some patterns (for example `ToolRuntime`, `HumanInTheLoopMiddleware`) away from the lower-level APIs this project uses. That is a useful oral-defence point: say why we chose the lower-level API, do not pretend we did not know.

## Installed versions (from `uv.lock`)

| Package | Version |
|---|---|
| langchain | 1.4.7 |
| langchain-core | 1.7.3 |
| langchain-community | 0.4.2 |
| langchain-openai | 1.7.3 |
| langchain-text-splitters | 1.1.3 |
| langgraph | 1.2.19 |
| langgraph-prebuilt | 1.1.1 |
| langgraph-checkpoint | 4.3.1 |
| langgraph-checkpoint-sqlite | 3.1.3 |
| langgraph-sdk | 0.4.11 |
| langsmith | 0.14.17 |
| deepagents | 0.7.32 |
| faiss-cpu | 1.15.1 |
| fastembed | 0.9.1 |
| pydantic | 2.14.2 |
| fastapi | 0.162.0 |
| sse-starlette | 3.6.0 |
| typer | 0.27.3 |
| rich | 15.0.0 |
| pypdf | 6.21.4 |
| numpy | 2.5.4 |
| pytest | 9.2.0 |

(`pyproject.toml` only sets lower bounds such as `langchain>=1.0`, `langgraph>=1.0`, `deepagents>=0.2`; the lock file is the truth. `uv.lock` lists `openai` at 3.42.1; that is what the lock file says.)

Python requirement: `>=3.12,<3.14`.

## Main mapping table

URL prefix for all rows marked `docs`: `https://docs.langchain.com`.

| # | Pattern | Official doc page | Status | Where adapted | How and why we differ |
|---|---|---|---|---|---|
| 1 | `init_chat_model`, `bind_tools`, `with_structured_output`, `.batch(max_concurrency)` | docs `/oss/python/langchain/models` | Confirmed: covers init_chat_model, bind_tools, with_structured_output (Pydantic/TypedDict/JSON Schema), and `max_concurrency` in `RunnableConfig` | `extraction.py:57-69`, `78-84` | Follows the docs. We add `.with_retry(stop_after_attempt=3)` to the structured runnable so one failing item does not fail a batch. Dataclass schema (`AlertFacts`) is included because the curriculum names it. |
| 2 | Structured output as a concept | docs `/oss/python/langchain/structured-output` | Confirmed (page is about agents returning JSON, Pydantic or dataclasses via provider-native or tool strategies) | `rag/schemas.py` (`GroundedDraft`), `evals/judges.py`, `agents/deep.py` (`InvestigationPlan`, `Replan`, `Report`) | We call `with_structured_output` on a model, not the agent-level `response_format`. Field `description`s act as the prompt; Pydantic validators (`_one_per_role`, cycle check) add rules the model alone cannot guarantee. |
| 3 | `@tool`, docstring becomes the tool description | docs `/oss/python/langchain/tools` | Confirmed: docstrings become descriptions; page says `ToolRuntime` replaces `InjectedState` (with a migration section); tool error handling is via `wrap_tool_call` middleware | `tools.py`, `mutations.py` | Plain `@tool` as documented. For the write tool we use `InjectedState` + `InjectedToolCallId` (older, still working in langgraph-prebuilt 1.1.1) instead of the newer `ToolRuntime`. We use them to verify an approval digest inside the tool itself. |
| 4 | State, reducers, `add_messages`, conditional edges, `Command`, `Send`, recursion limit | docs `/oss/python/langgraph/graph-api` | Confirmed: Reducers, "Working with messages in graph state", `Send`, `Command` (goto), recursion limit (default 10,007 per the page) | `agents/react.py:28`, `agents/guarded.py:65-74`, `agents/deep.py:134-146` | Same primitives. We set `recursion_limit` explicitly: 6 for Task 2 (the assessment rule) and 60 for the guarded graph because it spends four steps per tool round. |
| 5 | Using the graph API in practice (Send map-reduce, Command, add_messages) | docs `/oss/python/langgraph/use-graph-api` | Confirmed (ToolNode only briefly) | `agents/deep.py:222-227` (`dispatch`), `guarded.py:166-195` (`Command(goto)`) | Docs show `Send` for map-reduce over a list; we use it for a fixed three-role fan-out with dependency gating (`_ready`) and always return at least one `Send` so the run cannot end by accident. |
| 6 | ReAct loop from scratch; orchestrator-worker with `Send` | docs `/oss/python/langgraph/workflows-agents` | Confirmed: builds a tool-calling agent loop and the orchestrator-worker pattern; it does not use the word "ReAct" | `agents/react.py:61-73`, `agents/deep.py` | We hand-build `agent -> tools -> agent` with `tools_condition` and `ToolNode`, because the assessment requires a visible `StateGraph`. Production-style code would call `create_agent` (we do exactly that inside RCA workers). |
| 7 | Designing a graph (nodes, shared state, edges) | docs `/oss/python/langgraph/thinking-in-langgraph` | Confirmed (customer-support-email example) | whole `agents/` package | Background reading for why nodes return state deltas and why deterministic checks are nodes, not prompts. |
| 8 | Checkpointers and threads | docs `/oss/python/langgraph/persistence` | Confirmed: checkpointers (thread-scoped short-term memory) and stores; says nothing about `update_state` or `as_node` | `agents/react.py:76` (`thread_config`), `services.py:96` (`SqliteSaver`), `api.py:35` (`AsyncSqliteSaver`) | We use SQLite savers from `langgraph-checkpoint-sqlite` (sync for CLI, async for the API). The Memory page (`/oss/python/langgraph/add-memory`, confirmed) lists Postgres, MongoDB, Redis and Oracle savers and uses an in-memory saver in its examples; it does not mention SQLite. SQLite is a local/dev choice: single file, no server. |
| 9 | `interrupt()` and `Command(resume=...)` | docs `/oss/python/langgraph/interrupts` | Confirmed: "The node restarts from the beginning of the node" on resume | `agents/guarded.py:198-263` | We write our **own** approval node and own decision schema (`approve / edit / reject`, approver identity, unauthorised retry loop, policy re-validation after edit). See row 10. |
| 10 | Human approval of tool calls | docs `/oss/python/langchain/human-in-the-loop` | Confirmed: `HumanInTheLoopMiddleware` pauses tool calls for approve, edit, reject or respond decisions | `agents/guarded.py`, `tools.py:141-163` | **Own interrupt node instead of `HumanInTheLoopMiddleware`.** Reasons: (a) the graph is a hand-built `StateGraph`, not `create_agent`, so middleware does not attach; (b) we need approver authorisation against an on-call list; (c) we re-run deterministic policy after an edit; (d) we bind the approval to the exact arguments with a digest the tool re-checks. The assessment also asks explicitly for native `interrupt()`. |
| 11 | `update_state`, `as_node`, resuming with `invoke(None)` | docs `/oss/python/langgraph/use-time-travel` | Confirmed: Fork section covers `update_state` on a checkpoint then `invoke(None)`; "From a specific node" covers `as_node` | `cli.py:300-307`, `tests/test_task5.py::test_edit_via_update_state_then_resume` | Docs use it for forking history; we use it to edit a paused tool call and force re-validation at `tool_guard` by declaring the edit came from `agent`. |
| 12 | Streaming modes | docs `/oss/python/langgraph/streaming` | Confirmed: `updates` (changes returned by each node) and `values` (full state after each step) | `cli.py` `task2` | As documented. The API uses `astream_events(version="v2")` (`api.py:164`), which is a LangChain event stream; that call is not on the fetched page. |
| 13 | LangChain-level streaming | docs `/oss/python/langchain/streaming` | Confirmed (supported modes there: `updates`, `messages`, `custom`) | `gateway/core.py:303-324`, `api.py:156-187` | We implement `_stream` and `_astream` on `GatewayChatModel` so token events exist at all. |
| 14 | Trimming, deleting and summarising messages | docs `/oss/python/langchain/short-term-memory` | Confirmed: trim messages, `RemoveMessage`, summarisation with `SummarizationMiddleware` | `llm_context.py` | **Own `trim_for_model` + `summarise_window` instead of `SummarizationMiddleware`.** Trimming is per model call (state untouched), counts messages rather than tokens (assessment says "last 6 messages"), omits `start_on` to avoid emptying context, and repairs tool-call pairs with `_drop_orphans`. The summary is a compress node using `RemoveMessage` deltas. |
| 15 | Prebuilt middleware list | docs `/oss/python/langchain/middleware/built-in` | Confirmed (title "Prebuilt middleware"): documents Model fallback, PII detection, Tool call limit, Summarization | referenced by rows 10, 14, 16, 17 | Used directly only once: `ToolCallLimitMiddleware(run_limit=12)` and a custom `@wrap_tool_call` in `agents/deep.py:243`. |
| 16 | Model fallback | docs `/oss/python/langchain/middleware/built-in` ("Model fallback" section) | Confirmed to exist | `gateway/core.py:206-232` | **Custom gateway instead of `ModelFallbackMiddleware`.** Middleware attaches to `create_agent` only; our RAG, extraction, judges and guards are not agents. The gateway is a `BaseChatModel`, so everything gets it. It also adds routing, multi-key rotation with cool-down, `with_retry` for transient errors only, caching and cost telemetry. Underneath it uses the standard `.with_fallbacks()` runnable method. |
| 17 | PII detection | docs `/oss/python/langchain/middleware/built-in` ("PII detection" section) and `/oss/python/langchain/guardrails` | Confirmed: guardrails page covers built-in PII detection and HITL middleware plus custom deterministic and model-based checks | `security/pii.py`, `security/injection.py`, `guarded.py` input/output nodes | **Own PII engine instead of `PIIMiddleware`.** Reasons: Luhn validation for cards, ordered detectors (keys before generic numbers), REDACT/MASK/HASH/BLOCK per type, stable hash tokens, and use in nodes outside `create_agent` (extraction prompts, tool arguments, RCA reports). Pattern is the docs' own "custom deterministic and model-based guardrails": rules first, LLM classifier second. |
| 18 | Agents and middleware (`create_agent`) | docs `/oss/python/langchain/agents` | Confirmed: `create_agent` and middleware; tool-error handling is only briefly mentioned (`ToolRetryMiddleware`) | `agents/deep.py:239-245` | Used for the three RCA workers. The page does not state that a raised tool exception propagates out of `invoke`; that behaviour was observed in this build (`worker` wraps `agent.invoke` in try/except). |
| 19 | Deep agents | docs `/oss/python/deepagents/overview`, `/oss/python/deepagents/subagents` | Confirmed: harness with planning, filesystem and subagent capabilities; subagents page says they keep the main agent's context clean | `agents/deepagent_demo.py`; main RCA in `agents/deep.py` | **Custom plan-and-execute `StateGraph` instead of `deepagents` for the real RCA.** The assessment needs inspectable planner, gate and synthesiser nodes, a deterministic artifact gate, replanning counters and degraded reports. `deepagents.create_deep_agent` is shown in `deepagent_demo.py` as the high-level alternative. |
| 20 | Chat model integrations list | docs `/oss/python/integrations/chat/index` | Confirmed (catalog of providers and capabilities) | `gateway/core.py:58-68` | Background for provider-agnostic `init_chat_model`. |
| 21 | Custom chat model (`BaseChatModel`: `_generate`, `_llm_type`, optional `_stream`, `_agenerate`) | `python.langchain.com/docs/how_to/custom_chat_model` and `reference.langchain.com/python/langchain-core/language_models/chat_models/BaseChatModel` | **Not confirmed.** Both appeared in a web search; neither was fetched. The Models page on docs.langchain.com (confirmed) states it does not explain how to write your own chat model class. | `gateway/core.py:370-415` (`GatewayChatModel`), `ChaosChatModel` (`core.py:85`), `tests/support.py` (`ScriptedChatModel`) | Adapter pattern: implements `_generate/_agenerate/_stream/_astream`, stores `bind_tools` arguments and applies them per real candidate. |
| 22 | LLM prompt caching (`InMemoryCache`, `SQLiteCache`) | `python.langchain.com/docs/how_to/llm_caching` and `/chat_model_caching` | **Not confirmed.** Seen in search results only (not on docs.langchain.com). | `gateway/cache.py` | We use the two cache classes as a storage layer but build our own key (hash of normalised prompt), namespace, TTL and a semantic tier with an entity guard. Stock `set_llm_cache` is not used; the cache only serves `ModelGateway.ask`. |
| 23 | FAISS vector store | `https://docs.langchain.com/oss/python/integrations/vectorstores/faiss` | **Not confirmed.** A search result listed this URL, but fetching it returned HTTP 404. A reference-site FAISS URL also returned 404. | `rag/vector.py:80-92` | We pass `DistanceStrategy.MAX_INNER_PRODUCT` and `relevance_score_fn=_identity` so scores are cosine similarities on unit vectors. Persist with `save_local`/`load_local(allow_dangerous_deserialization=True)`. |
| 24 | RAG | docs `/oss/python/langchain/rag` | Fetched, but the page that came back is "Retrieval Augmented Generation (RAG) with Deep Agents" (a docs Q&A agent with subagents), not a classic chunk-embed-retrieve tutorial. | `rag/*` | Our RAG is a hand-built pipeline, not an agent: parse -> chunk by section -> FAISS -> hard threshold -> structured grounded answer with enforced citations. |
| 25 | Text splitters (`RecursiveCharacterTextSplitter`) | not fetched | **Not confirmed.** | `rag/vector.py:32-57` | Used within each section, with `add_start_index=True` to recover exact pages. |
| 26 | LangSmith `evaluate()` and LLM judge | docs `/langsmith/evaluation-quickstart` | Confirmed: starter evaluation that uses an LLM judge, via UI or SDK | `evals/runner.py`, `evals/judges.py`, `evals/dataset.py` | We use the SDK path with custom evaluators that return `{"key", "score", "comment"}`. Judges use structured output (reasoning then score), counts instead of free scores, and a local mode with `upload_results=False`. |
| 27 | LangSmith RAG evaluation tutorial | `https://docs.langchain.com/langsmith/evaluate-rag-tutorial/index.html` | **Not confirmed.** Appeared in a web search only; the snippet described correctness and groundedness evaluators. | `evals/judges.py` | Our "RAG quad" rubrics: correctness, groundedness, answer relevance, context relevance (plus conciseness, tone). |
| 28 | SQLite checkpointer package | `pypi.org/project/langgraph-checkpoint-sqlite` | **Not confirmed on docs.langchain.com.** Search results (PyPI listing) described sync and async support via aiosqlite and `AsyncSqliteSaver`. | `services.py:96`, `api.py:35` | Matches the usage. |
| 29 | Semantic router (nearest labelled utterance) | no official LangChain page | n/a | `gateway/router.py` | Local embeddings, no LLM call, ties go to the reasoning model. The curriculum's "semantic routing" is implemented from first principles. |
| 30 | Key pool, FinOps ledger, price table | no official LangChain page | n/a | `gateway/keys.py`, `gateway/telemetry.py`, `gateway/pricing.py` | Production-ops code; telemetry keys follow the assessment's dictionary. |
| 31 | Vectorless / reasoning-tree RAG (PageIndex style) | no official LangChain page (the assessment cites a video) | n/a | `rag/tree.py` | Built from first principles: skeleton from headings, bottom-up summaries, LLM navigator with structured output, self-correcting retry. |
| 32 | Prompt-injection / canary / action-claim checks | `/oss/python/langchain/guardrails` is the closest page (confirmed) | Confirmed page, but these specific checks are not described there | `guarded.py:125-163`, `292-306` | Canary string, `action_check` (announced action without tool call) and citation nudge are project-specific responses to observed model behaviour. |

## Where our design intentionally departs from the "current best practice" in the docs

Be ready to defend each of these as a conscious choice, not an oversight.

1. **Hand-built `StateGraph` instead of `create_agent` for the main copilot.** The assessment grades visible state, reducers, edges and checkpointing. The cost: we re-implement tool routing and lose middleware (HITL, PII, fallback, summarisation).
2. **`InjectedState` instead of `ToolRuntime`.** The Tools page says `ToolRuntime` replaces it. It still works in langgraph-prebuilt 1.1.1.
3. **SQLite checkpointer.** Fine for one process; the Memory page lists server-backed savers for production.
4. **In-process `app.state.rca_runs` and a background thread for RCA.** Lost on restart; acceptable for a demo, not for production.
5. **Own gateway `BaseChatModel`** instead of provider-specific middleware, so non-agent code gets fallback and telemetry.
6. **Own PII and injection engine** instead of `PIIMiddleware`, for validators, policies and reuse outside agents.
7. **`deepagents` is a demo only** (`agents/deepagent_demo.py`); the real RCA is an explicit graph.

## Suggested reading order for the team (all confirmed URLs)

1. `/oss/python/langchain/models`
2. `/oss/python/langchain/tools`
3. `/oss/python/langgraph/graph-api`
4. `/oss/python/langgraph/use-graph-api`
5. `/oss/python/langgraph/workflows-agents`
6. `/oss/python/langgraph/persistence`
7. `/oss/python/langchain/short-term-memory`
8. `/oss/python/langgraph/interrupts`
9. `/oss/python/langgraph/use-time-travel`
10. `/oss/python/langchain/human-in-the-loop`
11. `/oss/python/langchain/middleware/built-in`
12. `/oss/python/langchain/guardrails`
13. `/oss/python/deepagents/overview` and `/subagents`
14. `/langsmith/evaluation-quickstart`

(All prefixed with `https://docs.langchain.com`.)
