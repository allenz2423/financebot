# Tool-Router POC Handoff — PRELIMINARY, SUPERSEDED

> **Superseded 2026-09-19.** The evaluation below used an incomplete catalog
> (`CAPABILITY_REGISTRY`, 86 of 178 callable tools — it omits every
> concierge/browser tool) and had no ground-truth labels, so its "results" are
> not decision-grade. See **[tool-router-eval-corrected.md](tool-router-eval-corrected.md)**
> for the corrected, labelled evaluation (hybrid retrieval 54% top-1 / 96% top-8;
> browser domain gate 9/9 recall, 0/65 FP). The Jev verdict and the
> retrieval-only architecture notes below remain valid.

Date: 2026-09-18

## Objective

Evaluate whether `browser-use/jev-ultrafast` could help Delilah choose appropriate tool calls, then build a small proof of concept using Delilah's existing local embedding and Qdrant infrastructure.

## Jev research conclusion

Jev should not be wired into Delilah's concierge or used as a second hosted agent.

Reasons:

- Its decision-maker is a hosted TypeSafe API. Each browser step sends page text, elements, recent actions, and task context to `api.typesafe.ai`.
- Its chooser is not pluggable in the current implementation; adopting Jev means adopting TypeSafe as the planner.
- Browser Harness attaches to an existing Chrome profile through CDP. Delilah's concierge deliberately uses disposable, isolated, per-tenant browser sessions.
- Jev's MVP explicitly does not support frames, shadow roots, canvas, uploads, pop-up tabs, nested scrolling, or arbitrary keyboard widgets.
- Jev has no native support for Delilah's vault, tenant isolation, approvals, screenshot redaction, audit chain, or VLM verification.
- Its benchmark is narrow: a simple Google Flights task, not adversarial or credentialed flows.

The useful ideas worth borrowing are:

1. Indexed action candidates and code-owned element IDs.
2. A freshness/staleness check immediately before mutation.
3. Center-point `elementFromPoint` occlusion validation before clicking.
4. Recording the action before observing the post-action page.
5. Keeping model output from becoming selectors, coordinates, JavaScript, or shell commands.

## Proposed Delilah architecture

Use embeddings and Qdrant only to retrieve candidate tools. The existing Delilah advisor remains authoritative for:

- final tool selection;
- argument generation;
- sequencing;
- risk and permission decisions;
- execution and validation.

Flow:

```text
user request
    -> local embedding
    -> Qdrant tool-catalog search
    -> candidate tool names
    -> load only candidate schemas
    -> existing Delilah advisor chooses and calls the tool
```

This avoids adding TypeSafe, avoids sending browser or financial content to a new hosted planner, and keeps existing controller gates intact.

## Proof of concept implementation

The standalone script is:

- [`scripts/tool_router_poc.py`](../scripts/tool_router_poc.py)

It does not modify the advisor loop or execute any tools. It:

- reads Delilah's existing capability registry;
- creates one natural-language card per registered tool;
- removes duplicate tool names;
- embeds the cards using Delilah's existing embedding helper;
- writes vectors to a separate Qdrant collection named `delilah_tool_router_poc`;
- embeds each request;
- retrieves the top eight candidate tools;
- writes ranked scores to CSV and TXT.

The collection is intentionally separate from Delilah's financial/world-model collection.

## Evaluation battery

The script now contains 114 natural-language requests across:

- spending and budgets;
- balances and net worth;
- transactions and dashboards;
- cash flow, planned transactions, and income;
- subscriptions, savings buckets, and sinking funds;
- debt and payoff simulations;
- investments and portfolio drift;
- credit and cash-drag analysis;
- web research and merchant intelligence;
- Gmail and receipts;
- world-model claims, dossiers, and context;
- vector memory;
- Python/simulation/report tools;
- reminders, monitoring rules, and alerts;
- concierge order, tracking, and price-watch reads.

Each request retrieves eight candidates, producing 912 CSV result rows.

## Generated outputs

- [`data/tool-router-poc/tool_router_results.txt`](../data/tool-router-poc/tool_router_results.txt)
- [`data/tool-router-poc/tool_router_results.csv`](../data/tool-router-poc/tool_router_results.csv)

The TXT file is easier for qualitative review. The CSV is suitable for labeling expected top-1/top-3 tools and calculating retrieval metrics.

## Execution

The POC was copied to and run inside the `delilah_bot` container so it could reach the internal Qdrant and Ollama services:

```bash
docker cp scripts/tool_router_poc.py delilah_bot:/app/scripts/tool_router_poc.py
docker exec delilah_bot env PYTHONPATH=/app \
  python3 /app/scripts/tool_router_poc.py
```

The live run indexed 86 unique tools and evaluated all 114 requests successfully.

## Initial observations

Strong examples:

- Restaurant spending ranked `query_spending` first.
- Spending-category breakdown ranked `get_spending_breakdown` first.
- Budget-status questions ranked `check_budget_status` first.
- Reminder creation ranked `schedule_reminder` first.
- Subscription questions ranked `get_subscriptions` first.

Weak examples:

- Current net-worth queries sometimes ranked historical net-worth tools above current-position tools.
- Debt snowball/avalanche queries did not consistently rank the simulation tool first.
- Order tracking and price-watch requests produced noisy candidates.
- Gmail queries sometimes ranked a refund-analysis tool above basic Gmail search.

These weaknesses are expected from the first catalog: its descriptions are generated mostly from tool name, registry domain, and capability label. The next improvement should use the real function descriptions and parameter metadata from `BOT_TOOLS_SCHEMA`, plus explicit risk/read-write metadata.

## Recommended next step

Keep the POC as a retrieval layer, then improve it in this order:

1. Build tool cards from the real tool schemas rather than registry labels.
2. Add payload fields for `risk`, `read_or_mutate`, required arguments, and aliases.
3. Add lexical/rule-based boosts for exact concepts such as `net worth`, `tracking`, `refund`, and `snowball`.
4. Label this 114-request battery with expected tools.
5. Measure top-1, top-3, and top-8 recall before putting candidate retrieval into the advisor loop.
6. Fall back to normal progressive discovery when confidence is low.

The router must never grant execution permission. It should only reduce the candidate schema set presented to Delilah's existing advisor.
