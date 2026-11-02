# Assessment matrix

**Status:** PASS means verified by a run or test, not just implemented. BLOCKED means an external dependency is missing.

**Evidence:** `evidence/*.log` are captured live runs against OpenAI on the final code. `tests/` names are automated tests. "Live" tests are in `tests/test_live.py`.

## Summary

| Gate | Result |
|---|---|
| Curriculum topics (Part A) | All PASS |
| Task checklists (Part B) | All PASS except one BLOCKED: the LangSmith dashboard view |
| Live mutations | 7 of 7 demonstrated |
| Anti-pattern check (Part C) | PASS |

**The one blocked item:** Task 6 asks for a LangSmith experiment dashboard. No `LANGSMITH_API_KEY` is available, so `evaluate()` ran for real with `upload_results=False`. The upload path passes a recording fake and a signature check against the installed SDK, but has never reached the LangSmith service. **Add a key and run `incident task6` before the review.** The Oral Defence gate itself still needs your team.

## Part A: curriculum

### 1. LangChain fundamentals

| Topic | Status | Code | Evidence |
|---|---|---|---|
| Provider-agnostic init | PASS | `extraction.py` `build_chat_model`, `gateway/core.py` `default_model_factory` | `evidence/task1.log` |
| Provider-specific wrappers | PASS | Installed `langchain-openai`, `-anthropic`, `-google-genai`, `-groq`, `-ollama`; `ChatOpenAI` etc. resolved by provider prefix. Only OpenAI was exercised live | `evidence/task1.log` |
| Environment isolation | PASS | `config.py`, `.env.example`, `.gitignore` | No keys in source |
| `.invoke()` | PASS | `extraction.py` `run_tool_loop` | `evidence/task1.log` |
| `.stream()` | PASS | `extraction.py` `stream_with_tools` | `evidence/task1.log` |
| `.astream_events()` | PASS | `api.py` `_sse_from_graph` | `tests/test_api.py`, `scripts/ui_e2e.py` |
| `.batch()` with `RunnableConfig(max_concurrency)` | PASS | `extraction.py` `extract_tickets` | `test_batch_extracts_each_message_and_respects_concurrency` measures peak 2 |
| System, Human, AI, Tool messages | PASS | `extraction.py` `run_tool_loop` | `test_manual_tool_loop_produces_all_message_types` |
| Pydantic structured output | PASS | `CustomerTicket` | `evidence/task1.log` |
| TypedDict structured output | PASS | `TicketDigest` | `evidence/task1.log` |
| Dataclass structured output | PASS | `AlertFacts`. Note: returns a dict, and can carry extra keys | `evidence/task1.log` |
| `@tool`, type hints, docstrings | PASS | `tools.py` | `test_tools_expose_docstring_and_types_to_the_llm` |
| `bind_tools`, inspecting `tool_calls` | PASS | `extraction.py`, `mutations.py` | `evidence/task1-mutation.log` |
| Tool-execution loop | PASS | `extraction.py` `run_tool_loop` | `evidence/task1.log` |
| `with_retry`, rate limits | PASS | `extraction.py`, `gateway/core.py` `TRANSIENT_ERRORS` | `evidence/task7-fallback-429.log` |
| Token counters | PASS | `llm_context.py` `count_tokens` | `test_sliding_window_summary_folds_old_messages` |
| `trim_messages` | PASS | `llm_context.py` `trim_for_model` | `test_model_sees_at_most_six_messages_but_checkpoint_keeps_all` |
| Sliding-window summary | PASS | `llm_context.py` `summarise_window`, `guarded.py` `compress_history_node` | `test_sliding_window_summary_folds_old_messages` (scripted model) |

### 2. LangGraph

| Topic | Status | Code | Evidence |
|---|---|---|---|
| Typed state | PASS | `AgentState`, `GuardedState`, `RcaState` | All graph tests |
| Nodes return deltas | PASS | `agents/` | `evidence/task2-updates.log` |
| Deterministic and conditional edges | PASS | `react.py`, `guarded.py`, `deep.py` | Graph tests |
| `add_messages` reducer | PASS | `react.py` | `test_edit_via_update_state_then_resume` shows replace by ID |
| Cyclic ReAct | PASS | `build_react_graph` | `evidence/task2-updates.log` |
| `ToolNode`, `tools_condition` | PASS | `build_react_graph` | `evidence/task2-updates.log` |
| Sequential tool calls | PASS | | `test_two_hop_runs_within_one_invoke_under_recursion_limit_6`, live |
| Parallel tool calls | PASS | `ToolNode` runs a multi-call message | `evidence/task2-updates.log` shows two calls in one step |
| `updates` and `values` | PASS | `cli.py` `task2` | `evidence/task2-updates.log`, `task2-values.log` |
| Checkpointers | PASS | `MemorySaver` in Task 2; `SqliteSaver`, `AsyncSqliteSaver` in the product | `test_paused_state_survives_a_new_graph_instance` |
| Thread isolation | PASS | `thread_config` | `test_threads_do_not_share_memory`, `evidence/task2-updates.log` |
| Time travel, inspection | PASS | `get_state_history`, `incident inspect` | `test_time_travel_replays_from_earlier_checkpoint` |

### 3. Vector RAG

| Topic | Status | Code | Evidence |
|---|---|---|---|
| PDF, HTML, Markdown loading | PASS | `rag/document.py` | `test_every_format_parses_to_the_same_outline` |
| Chunking and overlap | PASS | `chunk_sections` (900/150) | `test_chunks_keep_source_page_and_section` |
| Embeddings | PASS | `embeddings.py` | `evidence/task3-two-part.log` |
| Vector store | PASS | FAISS in `rag/vector.py` | Same |
| Source, page, section metadata | PASS | `chunk_sections` | Same test |
| Threshold retrieval | PASS | `VectorRag.retrieve` | `evidence/task3-off-topic.log` |
| Citation enforcement | PASS | `VectorRag.answer` validates block numbers | `test_answer_without_valid_citation_is_rejected` |
| Structured similarity and confidence | PASS | `RagResponse` | `test_relevant_question_returns_citations_scores_and_confidence` |

### 4. Vectorless RAG

| Topic | Status | Code | Evidence |
|---|---|---|---|
| No embeddings | PASS | `rag/tree.py` | `test_tree_module_has_no_vector_dependency` |
| Hierarchical tree, heading parse | PASS | `build_skeleton`, PDF outline parser | `test_skeleton_nests_headings_and_keeps_raw_text` |
| Recursive summaries | PASS | `summarise_tree` | `test_summaries_are_built_bottom_up`; 163 real nodes |
| LLM navigation | PASS | `TreeRag._descend` | `evidence/task3-backup-db.log` |
| Exact raw sections | PASS | Leaf `.text` | `test_navigation_reads_exact_raw_text_and_reports_the_path` |
| Traversal trace | PASS | `RagResponse.traversal`, `.trace` | `evidence/task3-backup-db.log` |

### 5. Deep agents

| Topic | Status | Code | Evidence |
|---|---|---|---|
| Plan and execute | PASS | `agents/deep.py` | `evidence/task4-normal.log` |
| Structured plan, DAG | PASS | `InvestigationPlan` | `test_plan_schema_rejects_cycles` |
| Re-planning | PASS | `RcaWorkflow.replanner` | `evidence/task4-delete-file.log` |
| Sub-agents with isolated prompts, tools | PASS | `WORKERS`, `create_agent` | `test_workers_have_isolated_toolsets` |
| Sequential and parallel delegation | PASS | `depends_on`, `Send` | `test_dependencies_run_in_order_and_files_are_shared`, `evidence/task4-normal.log` |
| Filesystem workspace | PASS | `agents/workspace.py` | `test_findings_stay_on_disk_not_in_graph_state` |
| Final synthesis | PASS | `RcaWorkflow.synthesizer` | `test_synthesizer_reads_the_files_from_disk` |
| `deepagents` library | PASS | `agents/deepagent_demo.py` | `evidence/deep-agent.log` |

### 6. Security and HITL

| Topic | Status | Code | Evidence |
|---|---|---|---|
| Prompt injection, jailbreak | PASS | `security/injection.py` | `test_project_attacks_are_blocked_without_an_llm_call` |
| Unauthorised execution | PASS | Rules, `tool_guard`, approval digest | `test_tool_refuses_when_called_without_approval` |
| Exfiltration | PASS | Canary, `output_guardrail_node` | `test_output_guard_redacts_and_stops_prompt_leaks` |
| Deterministic and semantic guards | PASS | `scan`, `SemanticGuard` | `test_semantic_layer_catches_what_regexes_miss` (scripted); the live copilot runs the classifier on each message |
| PII with REDACT, MASK, HASH, BLOCK | PASS | `security/pii.py` | `test_pii_policies` |
| Input, tool-argument, output checks | PASS | `guarded.py` nodes | `test_tool_arguments_with_secrets_are_refused` and neighbours |
| Native interrupt | PASS | `human_approval_node` | `evidence/task5-approve.log` |
| Approve, edit, reject | PASS | Same | `evidence/task5-*.log` |
| Guardrail nodes | PASS | `input_guardrail_node`, `output_guardrail_node` | Same |

### 7. Evaluation

| Topic | Status | Code | Evidence |
|---|---|---|---|
| Versioned dataset, created by code | BLOCKED (upload) | `evals/dataset.py` | `tests/test_langsmith_contract.py` against a fake |
| Structured judges | PASS | `evals/judges.py` | `evidence/task6.log` |
| Correctness, conciseness, tone | PASS | `RUBRICS` | Same |
| Context relevance, groundedness, answer relevance | PASS | `RUBRICS` | Same |
| Experiment comparison | PASS locally | `run_experiment`; UI Evaluation page | Same |
| Hallucination detection | PASS | `make_target`, `--inject-hallucination` | `evidence/task6-hallucination.log` |

### 8. Gateway

| Topic | Status | Code | Evidence |
|---|---|---|---|
| Unified interface | PASS | `ModelGateway`, `GatewayChatModel` | `test_gateway_chat_model_supports_structured_output_through_fallback` |
| Multi-provider | PASS | Any `provider:model` spec. Two OpenAI models were verified; other providers need their keys | `.env.example` |
| Semantic and intent routing | PASS | `gateway/router.py` (real embeddings) | `test_router_sends_*` |
| Fast vs reasoning model | PASS | Tiers | `evidence/task7.log` |
| Fallback | PASS | `_chain` | `evidence/task7-fallback-real-401.log` |
| Exact and semantic cache | PASS | `gateway/cache.py` | `evidence/task7.log` |
| Key rotation | PASS | `gateway/keys.py` | `test_key_pool_rotates_and_cools_down`, `test_second_key_is_tried_before_the_fallback_model` (offline) |
| Tokens, latency, cost, telemetry | PASS | `gateway/telemetry.py`, `pricing.py` | `evidence/task7.log` |

## Part B: task checklists

| Task | Requirement | Status | Evidence |
|---|---|---|---|
| Rules | Custom 15+ page document | PASS | Original 40-page runbook; `test_document_has_at_least_15_pages` |
| Rules | Git repo, `pyproject.toml`, `.env.example`, modular | PASS | Initialised; **not yet committed** |
| 1 | `init_chat_model`, `.with_structured_output(CustomerTicket)` | PASS | `extraction.py` |
| 1 | `.batch()` with `max_concurrency=2` via `RunnableConfig` | PASS | Measured peak 2 |
| 1 | `@tool` with hints and multi-line docstrings | PASS | `tools.py` |
| 1 | Two tools, streaming | PASS | `evidence/task1.log` |
| 1 | Live: rename, delete docstring | PASS | `evidence/task1-mutation.log`; literal source edit in `evidence/task1-literal-rename.log` |
| 2 | `Annotated[list[AnyMessage], add_messages]` | PASS | `react.py` |
| 2 | 2-hop in one `.invoke()` | PASS | `evidence/task2-updates.log`, live test |
| 2 | Two concurrent threads, no bleed | PASS | `evidence/task2-updates.log` |
| 2 | `recursion_limit=6` | PASS | `test_runaway_loop_stops_at_recursion_limit` |
| 2 | Trim to last 6 messages | PASS | Test named above |
| 2 | Live: stream modes | PASS | `evidence/task2-*.log` |
| 3A | Out-of-domain returns text with no generation | PASS | `evidence/task3-off-topic.log` |
| 3A | Structured `answer`, `citations`, `similarity_score` | PASS | `RagResponse` |
| 3B | Traversal trace | PASS | `evidence/task3-backup-db.log` |
| 3 | Comparison table | PASS | `evidence/rag_comparison.md` |
| 3 | Live: new two-section question | PASS | `evidence/task3-two-part.log` |
| 4 | Plan precedes research tools | PASS | `test_planner_runs_before_any_worker_and_report_is_written` |
| 4 | Workers write to disk | PASS | Test above |
| 4 | Synthesizer reads files, report in `./output/` | PASS | `evidence/task4-normal.log` |
| 4 | Live: delete file, tool exception | PASS | `evidence/task4-delete-file.log`, `task4-worker-exception.log` |
| 5 | Exact attack input masks email and ends the graph | PASS | `evidence/task5-approve.log`, `test_assessment_attack_masks_email_and_ends_at_guardrail` |
| 5 | Project attack with `sk-live-` key | PASS | `test_project_attacks_are_blocked_without_an_llm_call` |
| 5 | `interrupt()`, no `input()` | PASS | `human_approval_node`; no `input(` in `src/` |
| 5 | `get_state`, `update_state`, resume | PASS | `evidence/task5-edit-update-state.log` |
| 5 | Live: reject gives a `ToolMessage`, back to the LLM | PASS | `evidence/task5-reject.log` |
| 6 | 4 factual, 1 multi-hop, 1 adversarial | PASS | `test_golden_dataset_shape` |
| 6 | Groundedness, relevance, correctness judges | PASS | `evidence/task6.log` |
| 6 | Score plus `reasoning` | PASS | `JudgeResult`, `CountedResult` |
| 6 | Real `evaluate()` | PASS | Local mode |
| 6 | Upload to LangSmith | **BLOCKED** | No API key |
| 6 | LangSmith dashboard comparing 3A and 3B | **BLOCKED** | No API key; UI Evaluation page gives the local comparison |
| 6 | Live: hallucination drops the score | PASS | `evidence/task6-hallucination.log` |
| 7 | Simple to cheap, complex to frontier | PASS | `evidence/task7.log` |
| 7 | Primary failure falls back, no exception | PASS | Real 401, simulated 429 |
| 7 | Second identical prompt at most 50 ms | PASS | `evidence/task7.log` |
| 7 | Every call logs the required telemetry | PASS | `test_telemetry_record_has_every_required_key` |
| 7 | Live: invalid key, `fallback_triggered: true` | PASS | `evidence/task7-fallback-real-401.log` |

## Part C: anti-patterns

| Rule | Status | How checked |
|---|---|---|
| No hardcoded keys | PASS | Keys come from `Settings`. `.env` is gitignored. The word `sk-` in source is only in detector rules, test attacks and a deliberately invalid test key |
| No `input()` for HITL | PASS | No `input(` call in `src/` |
| No manual JSON parsing for structured output | PASS | All structured output uses `with_structured_output` |
| No video datasets | PASS | Original fictional company and document |

## Project story (Project 5)

| Item | Status | Where |
|---|---|---|
| Five alerts and emails become cards | PASS | `data/fixtures/inbox.json`, UI Incidents |
| "Why are payments slow?" with separate threads | PASS | `scripts/ui_e2e.py` |
| "How do we switch to the backup database?" | PASS | `evidence/task3-backup-db.log` |
| Root cause report: timeline, cause, prevention | PASS | `evidence/task4-normal.log` |
| Keys, passwords, IPs hidden; "wipe the logs" blocked | PASS | `test_pii_policies`, `test_project_attacks_are_blocked_without_an_llm_call` |
| Rollback: approve, change version, reject | PASS | `evidence/task5-*.log` |
| Trick, off-topic and two-part test messages | PASS | Tests and `evidence/task3-*.log` |

## Measured results

See [evidence/results.md](evidence/results.md).
