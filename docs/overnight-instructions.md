You are implementing docs/hermes-agentic-gap-review.md against the current
`financebot` repo, working unattended overnight. The user will not be
reviewing your work in real time. Because of that, correctness and
legibility in the morning matter more than progress.

## Scope

Work through the ENTIRE roadmap: the "Concrete step-by-step implementation
plan" (Steps 0–6) in full, and then the remaining roadmap items (1–15) not
already covered by those steps, in the "Recommended implementation order"
sequence given in the doc. Do not stop early. Keep going until you are
genuinely out of safe, well-specified work to do, or the user's budget/
token limit ends the session.

## Ground rules

- Work on a new branch (e.g. `agentic-roadmap/full-run`). Never commit to
  or push `main`. Push the branch to origin after every commit, so
  progress is visible remotely — the branch itself must never be merged
  or force-pushed without the user reviewing it.
- Commit granularly — after each meaningful, buildable sub-piece, not
  just at step boundaries. Prefix every commit message with one of:
    [wip]  — in-progress, tests may not all pass yet
    [gate] — this commit is the one where a step/item's acceptance gate
             (or, for items without one, its stated Proposed Sol) was
             fully met; message states which step/item and how it was
             verified
    [log]  — docs/overnight-log.md updates, no code change
  Push after every commit, [wip] included.
- If a push fails, retry once, then [log] the failure and keep working
  locally rather than stall the run waiting on connectivity.
- Before touching anything financial or side-effecting, confirm the repo's
  test/dev config uses sandbox credentials (Plaid, Gmail, etc.), not real
  accounts. If you can't confirm that, [log] why and skip that piece
  rather than proceeding with it or blocking the whole run.
- Follow the doc's own dependency order within the step-by-step plan
  (Step 0 → 1 → 2 → 3 → 4 → 5 → 6), and the "Recommended implementation
  order" groupings for the remaining roadmap items. Do not skip ahead
  past an unmet dependency.
- A step/item's stated acceptance gate (or Proposed Sol, where no gate is
  given) is the definition of done — not "feels solid." If you cannot
  meet it after a reasonable number of attempts, stop that piece, [log]
  exactly what's blocking, and move to the next independent piece of work
  rather than stall the whole run on one thing.
- HIGHER BAR for Steps 3–6 and roadmap items involving scheduler fairness,
  concurrency/capacity limits, delegation budgets, or any financial side
  effect (this includes items 2, 3, 5, 6, 11, 14): these are judgment
  calls, not mechanical implementation. Use the doc's stated defaults
  exactly as written (one worker, one active child task, deterministic
  verification on, model reviewer off, browser automation off) rather
  than inventing your own tuning. Do not auto-retry any ambiguous
  side-effect outcome under any circumstances, at any step, regardless of
  how far into the run you are. If a specific tradeoff isn't settled by
  the doc's stated defaults, [log] it as needing a human decision and
  move on rather than pick one yourself.
- For genuine ambiguities the plan doesn't settle at all — [log] the
  ambiguity and your best-guess resolution, pick the more conservative
  interpretation (favor manual-recovery-only over auto-retry, favor
  stopping over guessing on financial side effects, favor the doc's
  stated safe defaults over inventing new ones), push it immediately, and
  flag that it needs human confirmation. Do not let an unresolved
  ambiguity block unrelated work — keep moving to other independent
  pieces.

## Subagent use

- Spawn as many implementation subagents as useful for a given
  step/item — one per file/module, one for tests, one to research a
  reuse point before writing new code.
- Constraints:
  - Implementation subagents for a piece of work only start once its
    doc-stated dependencies have actually landed (gate passed, committed)
    — not a guess at what they'll look like.
  - Only the main loop writes to git. Subagents work in parallel on
    separate files; commits happen after merging their output and running
    the full test suite against the combined result.
  - An implementation subagent is disposable and single-purpose: narrow
    task in, output out, folded in by you. It does not decide whether a
    gate is met or what to work on next — that's the main loop's job.
  - The critic subagent (loop step 5) stays fully separate: a fresh
    instance with no visibility into implementation subagents' reasoning,
    only the final diff and the gate/Proposed-Sol text. Self-review or
    sibling-review does not count as the adversarial check.

## Loop, per step/item

1. Read the full Problem/Proposed-Sol (roadmap items) or
   Work/Reuse/Dependency/Acceptance-gate (step-by-step plan) text before
   writing anything.
2. Implement the smallest change that satisfies it, using implementation
   subagents as needed.
3. Write or extend tests that directly exercise the stated
   criteria — not just happy path.
4. Run the full test suite, not just new tests.
5. Spawn a fresh critic subagent with NO memory of your implementation
   reasoning — diff, the relevant doc text, and the review brief below.
6. Any BLOCKING issue: fix, re-run the critic. Do not commit on a
   BLOCKING finding. If you disagree with one, [log] the disagreement
   and your reasoning rather than silently overriding it.
7. Once clean: commit [gate], push, [log] a short entry (what changed,
   how verified, deferred SUGGESTIONS), move to the next piece of work
   per the dependency order.

## Critic subagent brief (fill in <ITEM> and <CRITERIA_TEXT>)

  Review this diff against docs/hermes-agentic-gap-review.md, specifically
  <ITEM>'s stated acceptance criteria:

  <CRITERIA_TEXT>

  Be adversarial, not encouraging. This is unattended overnight work with
  no human review before commit — treat every claim of correctness as
  unproven until checked against the diff.

  Check, in order:
  1. Does anything here violate "no auto-retry after an ambiguous outcome"?
  2. Does any side-effecting call bypass authorization/grant checks or
     receipt recording that already exists in the codebase?
  3. Does new state duplicate something SessionStore/ReceiptStore already
     tracks, rather than reusing it?
  4. If this touches task/step transitions or concurrency: is it actually
     atomic/race-free, or is there a check-then-act gap?
  5. If this touches scheduler fairness, delegation budgets, or capacity
     limits: does it use the doc's stated defaults, or does it invent
     tuning that wasn't specified?
  6. Do the new/changed tests actually exercise the stated criteria, or
     just cover the happy path?
  7. Is anything here scoped beyond what this item's Proposed Sol/Work
     asked for (scope creep into a later item)?

  Output two lists:
  - BLOCKING: must be fixed before this diff can be committed. Only for
    things that actually violate the criteria, the doc, or a correctness/
    safety property — not style preferences.
  - SUGGESTIONS: worth noting, not worth blocking on tonight.

  A BLOCKING finding means fix-and-resubmit. Not optional, not deferrable.

## Morning summary

Whenever you stop — full completion, budget exhausted, or blocked on
something no further independent work can route around — write a final
section at the top of docs/overnight-log.md: every step/item's status
(done/in-progress/blocked, with exactly where), every SUGGESTIONS item
deferred, every ambiguity needing a human decision, and a one-line overall
recommendation for what to review first. Commit and push this as [log]
regardless of what else happened.
