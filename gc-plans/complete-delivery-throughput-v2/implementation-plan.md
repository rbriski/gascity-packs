---
plan_slug: complete-delivery-throughput-v2
phase: implementation-plan
rig: gascity-packs
rig_root: /home/nvidia/gascity/city/rigs/gascity-packs
artifact_root: /home/nvidia/gascity/city/rigs/gascity-packs/gc-plans
requirements_file: /home/nvidia/gascity/city/rigs/gascity-packs/gc-plans/complete-delivery-throughput-v2/requirements.md
status: approved
created_at: 2026-08-03T14:28:19Z
updated_at: 2026-08-03T14:28:19Z
---

# Implementation Plan: Complete Delivery Throughput v2

## Summary

Amend the existing S5 PR once to make the already-present internal gstack
review path authoritative, turn CodeRabbit off by default, reduce both internal
and external repair loops to two attempts, freeze candidate identity, and keep
all terminal decisions mechanical and exact-head. Do not add a new runtime or
resume polecats until the Mayor-only change passes conformance tests and one
self-review.

Durable coordination remains on city bead `ci-0d03`; source scope remains in
rig feature `gp-dwl.11`, S5 bead `gp-dwl.11.5`, and convoy `gp-g84`.

## Current System

- `complete-delivery/formulas/complete-delivery.formula.toml` extends
  `gstack-build`, which already runs an independent agent review before PR
  publication. Its inherited `max_iterations` default is ten.
- The `coderabbit` workflow variable already accepts `required`, `optional`, or
  `off`, but defaults to `required`. Shell fallbacks also assume `required`.
- `complete-delivery-pr-gate.formula.toml` permits twelve exact-head
  inspect/repair/test/publish attempts.
- The external-review expansion has an immutable two-hour deadline and exact
  candidate/tested/published SHA checks, but its instructions are
  provider-specific and allow source repair after publication.
- Formula compilation already materializes durable attempt beads containing
  `gc.attempt`, `gc.max_attempts`, step identity, dependencies, timestamps, and
  outcomes. Hooks, Witness, Deacon, and refinery already supply continuation,
  health judgment, recovery, and protected merge serialization.
- `delivery_gate.py` already supports `--coderabbit off`; no replacement
  provider integration is required.

## Proposed Implementation

### 1. Safe defaults and bounded loops

- Change the Complete Delivery `coderabbit` default from `required` to `off`.
- Change shell-level fallback defaults in preflight and terminal approval to
  `off`, so missing optional configuration cannot silently re-enable a
  provider.
- Override the inherited `max_iterations` default to two for Complete Delivery.
- Reduce the PR-gate Formula check from twelve attempts to two.
- Update pack catalog text and workflow descriptions to describe internal
  review plus optional external evidence rather than mandatory CodeRabbit.

### 2. Frozen candidate contract

- Treat the canonical published `delivery.head_sha` as the review candidate.
- Require a clean worktree and exact `HEAD == inspected_head` before any repair.
- Batch all findings from one inspection into one repair commit/batch.
- Continue requiring `candidate_commit == tested_commit == published_head`
  before terminal approval.
- Do not wait for or invoke CodeRabbit when `coderabbit=off`.
- Preserve the existing fail-closed handling for CI, human change requests,
  malformed evidence, head movement, publication failure, and deadline expiry.

### 3. Native durability and timing

- Use Formula attempt beads and their native timestamps/outcomes as the stage
  event ledger; do not create a parallel event store.
- Preserve finite command timeouts and the immutable external-review deadline.
- Record exhausted attempt/deadline outcomes in native root metadata and the
  living report as explicit architectural blockers.
- Keep `ci-0d03` as the durable city-scoped completion owner and the convoy as
  visibility only.

### 4. Cross-rig rollout

- Ship the policy in the versioned pack and registry release.
- Keep broad model-routing changes disabled during this PR.
- Run the released pack first in `gascity-packs`; after the canary, remove
  rig-local `coderabbit=required` overrides and promote the approved role
  bindings through city configuration.
- Validate installation in a fresh rig before marking the pack report Live.

## Testing

Extend repository-native Complete Delivery tests to prove:

- Formula and shell defaults are `coderabbit=off`.
- Explicit `required` and `optional` modes remain supported for opt-in rigs.
- Both internal and external review loops compile with a two-attempt cap.
- A third review/repair attempt cannot be materialized as a successful path.
- Off mode performs no CodeRabbit invocation or wait and can pass with green
  required checks and zero human blockers.
- Candidate, tested, evaluated, and published SHAs must remain identical.
- Stale, malformed, moved-head, expired-deadline, and missing-local-gate
  evidence continue to fail closed.
- Formula compilation exposes native attempt identity, maximum attempts,
  dependencies, and terminal outcomes needed for restart recovery.

Run the focused Complete Delivery suite, formula compiler checks, shell syntax,
diff checks, and the repository suite used by PR #14. Then perform one Mayor
self-review and at most one consolidated repair pass.

## Rollout

1. Keep every rig suspended and work Mayor-only.
2. Commit the policy and implementation on a Mayor branch created from exact
   PR #14 head `825d08f815fbb51e33774ec64e8071aeba88b012`.
3. Push the verified commit to the existing PR #14 branch without force.
4. Run fresh GitHub checks and one internal exact-head review; do not request
   CodeRabbit.
5. Merge S5 through normal protected controls.
6. Resume the rig and allow dependency-gated S7 and registry work to proceed
   under the new defaults.
7. Install the released pack in a fresh rig, run the canary, then update the
   existing reports and promote city-level routing only if the canary passes.

The remediation itself targets four to six hours and stops for architectural
review at eight hours.

## Open Questions

None. The user explicitly approved the immediate plan on 2026-08-03 and asked
the Mayor to start work while retaining the existing pause on worker agents.
