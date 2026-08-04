---
name: gstack-lite
description: Deliver software pragmatically from a durable work item through implementation, repository-native checks, one independent review, protected publication, deployment, smoke verification, and concise accounting. Use by default for Gas City requests to build, fix, finish, test and deploy, ship, land, or take work end to end; use heavier gstack planning, QA, security, or migration skills only when the actual risk warrants them.
---

# Gstack Lite

Use one accountable owner and the smallest set of controls that can prove the
change is correct in production. This is a delivery policy, not a large formula.

## Invariants

- Never install or launch the deprecated Complete Delivery pack.
- Do not default to `gstack-build`, `build-basic`, review fan-out, model voting,
  or serial repair graphs.
- Keep one durable `main`. Use one short-lived branch only when repository
  protection requires a pull request, then delete it after merge.
- Cap implementation at two genuinely independent writers and review at one
  reviewer. Rescue replaces a writer; it never adds a seat.
- Fail upward after one failed attempt and one targeted repair. Carry the failed
  diff and exact test evidence to the stronger lane.
- Never call work complete before its requested terminal state. “Implemented,”
  “merged,” and “live in production” are different states.

## Route by risk

Use the configured city aliases:

- `sol-fast`: normal features and fixes.
- `luna-economy`: small, atomic, well-specified or mechanical work.
- `claude-careful`: context-heavy refactors when a second implementation family
  is useful.
- `claude-review`: independent review of Codex-built material changes.
- `sol-rescue`: failed work, difficult debugging, auth, permissions, destructive
  operations, and migrations.

For Claude-built changes, review with Sol/high. Do not start the rescue lane or
an alternate builder concurrently with two existing writers.

## Deliver in six stages

### 1. Anchor and measure

Create or identify one durable bead in the rig that owns the code. Record the
authorized start time, source base, intended outcome, acceptance criteria, and
canary. Separate active work, queue/provider wait, tests/CI, review/fix, deploy,
and manual intervention in the final accounting.

Inspect the repository and its instructions before asking questions answerable
from source. When the user authorizes autonomous completion, make reasonable
in-scope decisions without adding approval ceremonies.

### 2. Implement one complete slice

Give one owner a small deployable slice. Use a second writer only for work with
independent files and acceptance criteria. Preserve unrelated working-tree
changes. Run the narrowest useful check during implementation.

### 3. Run deterministic gates

Run repository-native format, lint, type, test, build, and browser checks in the
order justified by the change. Run cheap failures before model review. Use the
full suite when it is cheap or the blast radius demands it; otherwise rely on
targeted local checks plus required CI and state that boundary explicitly.

### 4. Review once, independently

For material code, run one direct `gstack.review` pass over the exact diff with
a different model family. Documentation-only or harmless test-only changes may
use deterministic checks alone. Add `gstack.qa`, `gstack.cso`, design review, or
migration review only when the changed surface triggers that risk.

Apply actionable findings once and rerun affected checks. If the repaired diff
materially changes, perform one focused re-review. Escalate rather than loop.

### 5. Publish and deploy through repository controls

Use the repository's normal protected path. Confirm the PR head is current,
required CI is green, review findings are resolved, and the merge result is on
the protected base. Prefer repository-owned CI/CD credentials. Local cloud
authentication is not required when GitHub workload identity owns deployment;
request it only when no authoritative CI or public verification path exists.

Verify the exact merged revision when the platform exposes it, then run a public
smoke or feature canary. Do not substitute “workflow succeeded” for a requested
production behavior check.

### 6. Close and account

Close the bead only after the requested terminal state is proven. Report:

- final commit, PR, merge SHA, deployment revision, and canary;
- every command and result that matters;
- total wall clock and stage breakdown;
- provider/model lane used for each intelligent pass;
- retries, rejected attempts, rework cause, and human intervention;
- residual risk or unavailable evidence.

## City configuration audit

For changes to Gas City itself, run `scripts/audit_city.py --city <city-root>`
from this skill directory. Use `--fix-stale-skills` only to remove exact stale
`complete-delivery.complete-delivery` symlinks after the active import is gone.
The script must pass before calling the city configuration coherent.
