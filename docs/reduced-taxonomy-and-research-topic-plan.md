# Reduced Transaction Taxonomy and Generic Research Packets

## Objective

Keep Delilah's accounting taxonomy expressive without making KEV or the local
classifier choose between semantically indistinguishable labels. Add a generic
research primitive that can gather bounded, traceable evidence for any topic.

## Active taxonomy

The model-facing tree has seven domains:

1. Income & Inflows
2. Essential / Fixed Living Expenses
3. Discretionary Spending
4. Maintenance, Education & Business
5. Financial Management & Wealth
6. Transfers & Non-Expense Movements
7. Adjustments & Unresolved

Each domain contains compact groups and leaves. The active tree currently has
100 paths. Details that are orthogonal to economic purpose remain dimensions:
transaction type, recurring status, subscription status, essentiality,
business/tax status, reimbursement/gift status, payment mechanism, account
role, merchant identity, purchase channel, and review/confidence state.

`Uncategorized Purchase` is an abstention representation, not a model choice.
The controller can select it only after insufficient or conflicting evidence.

## Compatibility strategy

- `TAXONOMY`, `CATEGORY_PATHS`, and KEV choice sets expose only the compact tree.
- Historical hierarchical paths are accepted and canonicalized to the compact
  tree at the boundary.
- Existing flat UI/budget labels remain valid and are produced by
  `legacy_category_for_path()`.
- No migration rewrites existing ledger rows or accounting amounts.
- Human corrections remain authoritative over any model or search result.

## Descriptor strategy

Descriptor normalization removes only anchored payment-rail prefixes. A
standalone `Google` or `Apple` is preserved because it may be the merchant
identity (`Google One`, `Apple Store`); `Google Pay` and `Apple Pay` are removed
as processors. The parser returns raw descriptor, processor tokens, candidate,
identity tokens, and normalization version for evidence lineage.

## Generic `research_topic` capability

`research_topic` is a read-only composite around the existing search/fetch
services:

```text
up to five query variants
  -> parallel search
  -> canonical URL deduplication
  -> score-ranked source cards
  -> fetch only the top bounded set
  -> source IDs + URLs + snippets + excerpts + failure statuses
```

The tool does not generate a hidden conclusion, persist a claim, or make a
search result authoritative. It returns an evidence packet so the controller
or model can compare source quality and disclose conflicts. Limits are enforced
in code: eight sources, eight fetched pages, twelve-thousand characters per
excerpt, and five query variants.

## Implementation order

1. Introduce the compact taxonomy and compatibility canonicalization.
2. Normalize processor prefixes without destroying legitimate merchant names.
3. Keep KEV dimensions-first and constrain each taxonomy stage to the active
   branch.
4. Keep uncertain transactions on the evidence-controlled local-LLM/human
   review cascade.
5. Add and expose `research_topic` through the capability registry and native
   tool schema.
6. Test taxonomy boundaries, parser regressions, evidence packets, caps,
   partial failures, and existing classifier behavior.
7. Run the full suite; report unrelated pre-existing failures separately.

## Safety and rollout

This change does not mutate transaction rows, balances, Plaid IDs, locks, or
correction history. KEV remains optional/shadowable. The research tool is
read-only and uses the existing fetch URL authorization path. Any future
automatic category application must still produce an execution receipt and
must not turn a source packet into accounting truth without the existing policy
gate.

## Verification

- Compact taxonomy and historical alias tests.
- Google One / Apple Store / Apple Pay parser tests.
- KEV hierarchical routing regression tests.
- Research packet tests for deduplication, bounded work, concurrent fetches,
  no-query validation, source fetch failures, and unrequested sources.
- Full repository regression suite.
