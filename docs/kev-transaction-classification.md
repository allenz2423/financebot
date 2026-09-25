# KEV transaction classification

Delilah now has an opt-in, batched KEV fast path for transaction decisions. It
does not replace the conversational advisor and it does not give KEV direct
write access to financial truth.

## Runtime flow

```text
Plaid/manual transaction
  -> existing normalization + known merchant/alias lookup
  -> bounded transaction context
  -> dimensions first: recurring/subscription/transfer/enrichment/review
  -> constrained main -> subgroup -> leaf category cascade
  -> distribution-based policy
       -> deterministic known merchant or high-confidence KEV
            -> guarded classification update + correction audit
       -> abstained or execution error
            -> existing LLM classifier path or review
```

`src/services/kev_transactions.py` owns this flow. The existing
`src/services/llm.py::classify_transaction_batch` remains the language-model
fallback. `ADVISOR_MODEL` is not changed. A new optional `CLASSIFIER_MODEL`
defaults to `ADVISOR_MODEL` for deployments that want to separate the two.

The KEV API is used as a typed `/v1/systemone` request. Classification first
collects independent dimensions so the controller can use them as routing
evidence. It then chooses a main category, only the subgroups under that main
category, and only the leaves under the selected subgroup. The full taxonomy
is never presented as one 100+ choice space. `Uncategorized Purchase` is not a
normal main-category choice; it represents the safe output of an abstention or
an unresolved fallback. Category choices are closed to the hierarchical taxonomy in
`src/services/transaction_taxonomy.py`: income/inflows, essential needs,
discretionary wants, maintenance/education/life events, financial
management/fees/savings, and neutral transfers. Each choice is a validated
`main → subgroup → leaf` path. Transfers are explicitly neutral and are not
treated as spending. No JSON text is generated or parsed as a classification
result. Existing historical flat labels remain readable and are not silently
rewritten.

## Safe rollout

The defaults are disabled:

```dotenv
KEV_TX_ENABLED=0
KEV_TX_SHADOW=0
KEV_TX_MODE=local
KEV_TX_MODEL=kev-latest
KEV_TX_LOCAL_URL=http://kev:8009/v1/systemone
KEV_TX_BATCH_SIZE=8
KEV_TX_AUTO_ACCEPT_THRESHOLD=0.95
KEV_TX_REVIEW_THRESHOLD=0.80
KEV_TX_MIN_MARGIN=0.20
KEV_TX_FALLBACK_ENABLED=1
KEV_TX_RETRIES=1
```

Recommended rollout:

1. Set `KEV_TX_SHADOW=1` while `KEV_TX_ENABLED=0`. Predictions and batch
   metrics are stored, but the existing search/LLM route still decides the
   ledger result.
2. Use the evaluation script against human-confirmed correction history.
3. Enable `KEV_TX_ENABLED=1` with shadow off only after held-out accuracy and
   calibration support the thresholds.
4. Keep `KEV_TX_FALLBACK_ENABLED=1`. Review/low-confidence/invalid/error
   results return to the existing classifier path. `abstained` means KEV
   answered but policy rejected the result; `invalid_response` and
   `inference_error` represent actual execution failures.

Uncertain explicit pending passes may perform one bounded merchant search and
retry the KEV cascade with the returned evidence. High-confidence transactions
never trigger this search. Control it with `KEV_TX_QUICK_SEARCH_ENABLED`,
`KEV_TX_QUICK_SEARCH_MAX_ITEMS`, and `KEV_TX_QUICK_SEARCH_MAX_CHARS`; failed
searches leave the original prediction intact and do not alter the ledger.

For an explicit operational pass, an administrator can use `!kevclassify 100
shadow` first. `!kevclassify 100 apply` applies only qualifying predictions;
rows that need review stay pending and remain available to the existing review
flow. Plaid ingestion itself does not automatically classify rows merely
because KEV is running.

High-confidence automatic application requires the category distribution and
all boolean decisions to clear the configured threshold, the category margin
to clear `KEV_TX_MIN_MARGIN`, and KEV to say that review and external
enrichment are unnecessary. Raw model `confidence` fields are not used as
authority; policy derives confidence from the returned distributions. Until a
larger labeled set is available, the default policy is intentionally strict.

Known registry/alias matches with a valid existing category are handled
deterministically and do not spend a KEV call. Historical categories are only
provided as bounded aggregate context from other transactions; the target's
own corrected label is excluded.

## Audit and accounting safety

`transaction_classification_batches` stores counts, latency, model, mode, and
confidence summary. `transaction_classification_predictions` stores each
prediction and its typed dimensions. These are prediction records, not ledger
truth.

`apply_auto_predictions` is the only KEV write path. It can update only
`category`, `status`, and `judgment` on still-pending, unlocked rows. It never
updates amount, date, merchant, account, Plaid transaction IDs, or the Plaid
baseline. Every automatic update adds a `kev_auto` correction-log record.
Existing human correction records take precedence and block automatic apply.

## Evaluation and training export

Human-confirmed categories are read from `transaction_correction_log`. KEV and
model-generated labels are excluded. A stable SHA-256 split makes the test
holdout reproducible and prevents the same correction from being both training
and evaluation data.

```bash
python scripts/evaluate_kev_transactions.py \
  --db data/finances.db \
  --user YOUR_USER_ID \
  --export-training /tmp/kev-train.jsonl

python scripts/evaluate_kev_transactions.py \
  --db data/finances.db \
  --user YOUR_USER_ID \
  --run-model
```

The report includes accuracy, per-category confusion counts, coverage and
accuracy at confidence thresholds, and expected calibration error. Training
export is explicit; there is no automatic fine-tuning on predictions.

## Runtime notes and limitations

The local Kev-4B sidecar is currently running on CPU in this environment. A
small direct request measured roughly 0.9–1.0 seconds warm on CPU, while a
multi-question transaction request can be materially slower. GPU execution
was tested but the current 8 GiB GPU does not fit the configured bf16 Kev-4B
checkpoint without an appropriate quantized/runtime configuration. Batch size
should therefore be measured with the actual deployment hardware.

The existing automatic queue worker remains disabled by design. The KEV path
is integrated into the established transaction batch function when explicitly
enabled; no production-wide mutation happens merely because the sidecar is
healthy.
