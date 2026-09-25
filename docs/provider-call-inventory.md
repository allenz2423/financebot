# Provider Call-Site Inventory

Prepared as Step 3 prerequisite work for [`hermes-agentic-gap-review.md`](hermes-agentic-gap-review.md). This inventory is descriptive only: it does not claim that inference calls are scheduler-routed or concurrency-limited. Line numbers are approximate and should be rechecked as code moves.

## Main advisor service (`src/services/llm.py`)

All six paths below issue provider HTTP requests directly; response parsing is shared, but request admission is not. The main streaming path records successful `provider_rounds` with total latency, but the other paths do not use that record, and failed attempts are not consistently represented there.

| Function | Provider/workload | Current path and notes |
| --- | --- | --- |
| `stream_generator()` (~5886; request ~6227) | Interactive advisor turns and autonomous wakeups; OpenAI-compatible, OpenRouter/OpenCode Zen routes, or Ollama | Central turn loop, but it creates its own `httpx.AsyncClient`; no shared scheduler/client boundary. Successful rounds record total latency in `ReceiptStore.provider_rounds`. |
| `maybe_flag_spending_concern()` (~667; request ~692) | Ollama proactive transaction nudge | Direct one-shot HTTP request from transaction processing. |
| `maybe_flag_windfall()` (~1007; request ~1028) | Ollama large-deposit nudge | Direct one-shot HTTP request from transaction processing. |
| `classify_transaction_batch()` (~1218; request ~1256) | Ollama transaction classification | Direct non-streaming request; also used by a transaction audit command. |
| `auto_extract_and_persist_claims()` (~11851; request ~11897) | OpenAI-compatible or Ollama memory extraction | Scheduled background task; direct HTTP request. |
| `_summarize_history_block()` (~12019; request ~12046) | OpenAI-compatible or Ollama history compression | Background compression task; direct HTTP request. |

The `httpx` call used by `send_push_notification` is notification delivery, not inference. `parse_stream_line` parses responses; it is not a provider client.

## Inference outside `src/services/llm.py`

| File/function | Provider/workload | Capacity relationship and notes |
| --- | --- | --- |
| `src/services/local_transaction_classifier.py::classify_with_local_llm()` (~116; request ~130) | Ollama transaction enrichment | Uses `CLASSIFIER_MODEL`, defaulting to `ADVISOR_MODEL`; same Ollama endpoint, direct request, no common admission layer. |
| `src/services/analyst.py::run_autonomous_analyst()` (~21; request ~79) | Ollama autonomous analyst | Same Ollama endpoint/model; on-demand Discord command, direct request. |
| `src/services/kev_decision.py::DecisionProvider.decide()` (~173; request ~182) | Typed Kev decisions | Separate configurable endpoint/service by default, not the Ollama pool; no common scheduler is evident. It may share host compute in colocated deployments. |
| `src/services/kev_decision.py::health_via_decision()` (~219; request ~232) | Kev typed health probe | Same configured Kev endpoint; distinguish from user-facing model work. |
| `src/bot/commands.py::_ollama_warm_model()` (~2327; request ~2337) and `preload_advisor_model()` (~4772) | Ollama model warmup | Direct requests to the advisor's Ollama server; consumes endpoint/host capacity. |
| `src/bot/commands.py::_ollama_unload_model()` (~2322; request ~2324) | Ollama model lifecycle control | `keep_alive: 0` unload request, not substantive inference; must coordinate with active work if lifecycle controls are centralized. |

Gmail watch's `_autoparse` calls `chat_with_delilah`, so it is a background workload through the main turn loop rather than a separate raw provider request. The transaction audit command calls `classify_transaction_batch` and therefore uses the `llm.py` path above.

## Related workloads that are not chat inference

- `src/services/qdrant_client.py::get_embedding()` uses Ollama `/api/embed` or a cloud embedding endpoint. It is not chat generation, but local embedding work can contend for the same Ollama server or host compute.
- `src/services/stt.py::_transcribe_openai()` uses audio transcription and should remain separately classified from chat inference.
- Ollama model discovery, search/web requests, merchant lookup, and notification HTTP calls are not model inference.

## Instrumentation and routing implications

- Primary advisor inference already records successful provider-round latency, model, turn, and selected outcome fields in the existing `ReceiptStore.provider_rounds`; do not create a second inference ledger.
- That record currently omits queue wait, first-content latency as a durable field, cancellation/outcome for failed rounds, and the workload class. Other direct inference paths are not included.
- The hard-cap boundary must cover all requests sharing a provider endpoint, not only `stream_generator()`. In particular, Ollama classifier, analyst, warmup, and embedding requests can bypass a chat-only semaphore. Keep embeddings and model lifecycle operations explicitly classified so policy can decide whether they share the same pool.
- Kev should have separate provider-capacity accounting unless configured to share a host/model pool. STT is a separate workload.
- Scheduler fairness/routing remains deferred until Step 2 passes. This inventory is the independently permitted preparatory work; it does not introduce concurrency changes or claim a capacity guarantee.
