# Kev integration

Kev is Delilah's typed decision sidecar. It is not a chat model, a tool
executor, or evidence. Delilah sends application state plus a closed set of
typed questions to `POST /v1/systemone`; Kev returns probabilities and a
selection. The controller still owns presentation, authorization, execution,
receipts, and user-visible claims.

## Current implementation

- `src/services/kev_decision.py` is the isolated Jev/System-One client.
- `src/services/llm.py` can run Kev in shadow mode or in bounded enforcing mode
  for tool selection.
- The routing request contains the user task and bounded tool descriptions,
  not only opaque function names.
- Choices are closed over the offered tools plus `none` and `clarify`.
- Invalid choices, extra probability keys, malformed scores, invalid Noul
  values, and invalid confidence values are rejected.
- A low-confidence, ambiguous, unavailable, or incomplete decision fails open
  to the existing controller schema and is recorded as an abstention/fallback.
- Enforcement can only narrow tools already offered by Delilah. It cannot
  introduce a tool, bypass a grant, approve a mutation, or support a claim.
- `ReceiptStore.decision_records` correlates every accepted, abstained, or
  failed Kev decision with the turn and controller round.

The client also supports `noul` and `score` questions for later ambiguity,
risk, approval-advice, and progress shadow evaluations. Those decisions are
not yet allowed to replace the deterministic approval, receipt, or progress
policies; enabling that would weaken the safety boundary.

## Local deployment

The optional Compose service is pinned to a reviewed Kev repository commit and
loads `jaredpalmer/kev-4b` by default. The service binds its API on the
internal network and exposes `/v1/models` for health checks.

```bash
docker compose --profile kev build kev
docker compose --profile kev up -d kev
```

For a Delilah process running on the host:

```dotenv
KEV_MODE=local
KEV_MODEL=kev-latest
KEV_LOCAL_URL=http://127.0.0.1:8009/v1/systemone
KEV_SHADOW=1
KEV_ENFORCING=0
```

Under Compose, Delilah uses the service-network URL automatically. Use
`KEV_DOCKER_LOCAL_URL` only when deliberately changing that address.

## Cloud deployment

Cloud is an explicit alternative, not an automatic fallback:

```dotenv
KEV_MODE=cloud
KEV_MODEL=kev-latest
KEV_CLOUD_URL=https://your-deployment.example/v1/systemone
KEV_CLOUD_API_KEY=...
```

If the cloud endpoint fails, Delilah records the failure and preserves the
existing tool offering. It does not silently switch between paid and local
inference, and it never treats a failed decision as authorization.

## Rollout order

1. Run the client contract tests and sidecar `/v1/models` health check.
2. Enable `KEV_SHADOW=1`, leaving `KEV_ENFORCING=0`; compare choices,
   confidence, abstentions, latency, and downstream receipts.
3. Label representative Delilah tasks and measure routing accuracy and
   calibration against the actual tool that solved each task.
4. Enable enforcement only for a narrow canary after the candidate set is
   complete and the confidence/margin thresholds have passed that evaluation.
5. Keep receipt-backed claim validation and deterministic policy gates enabled
   permanently. Kev is advisory/routing infrastructure, never proof that an
   action occurred.

The operational invariant remains:

> No successful tool receipt means no claim that the tool action happened.
