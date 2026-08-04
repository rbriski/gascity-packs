---
plan_slug: gstack-lite-standardization
phase: requirements
rig: gascity-packs
rig_root: /home/nvidia/gascity/city/rigs/gascity-packs
artifact_root: /home/nvidia/gascity/city/rigs/gascity-packs/gc-plans
status: approved
created_at: 2026-08-04T22:42:41Z
updated_at: 2026-08-04T22:42:41Z
---

# Requirements: Standardize Gstack Lite across Gas City

## Problem Statement

Complete Delivery was removed from active city imports after its production
canary failed, but the rollback is not mechanically complete. Stale Codex skill
links still advertise it, the old living report still claims it is live, and
some existing rigs do not materialize the lightweight `gc` roles they were
expected to inherit. That drift caused the Mayor to consider reinstalling the
deprecated pack.

## Solution

Make Gstack Lite the single default software-delivery policy. Keep the normal
path small: one durable bead, one implementation owner, repository-native
checks, one independent gstack review for material changes, at most one repair
cycle, protected publication, deployment, smoke verification, and measured
accounting. Invoke design, security, migration, browser QA, or broader gstack
skills only when the change's risk requires them.

Preserve Complete Delivery source and evidence for audit, but mark it
deprecated everywhere current users or agents could mistake it for an active
default. Remove stale active skill projections rather than deleting historical
records.

## User Stories

### As the operator, I see one coherent current setup

- The pragmatic report, no-go accounting, report library, city config, and pack
  documentation identify Gstack Lite as current.
- The historical Complete Delivery page is unmistakably archived and links to
  the no-go decision and replacement.
- “Gstack Lite” is described as a policy plus a small role pack, not another
  mega-formula.

### As the Mayor, I select the lightweight path automatically

- A concise installed skill triggers on ordinary build/fix/finish/ship/deploy
  requests and owns the end-to-end lightweight policy.
- The Mayor skill explicitly defaults to that policy and does not install or
  launch Complete Delivery.
- Full graph workflows remain explicit exceptions, never inferred defaults.

### As a current or future rig, I receive the same routing surface

- City-level gstack skills remain imported.
- The small `gascity/roles` pack is present for every current rig and inherited
  by future rigs.
- Every registered rig starts suspended, caps total sessions at five, and
  explicitly caps its polecat pool at one or two; the audit rejects drift after
  a future rig is added.
- Normal implementation, economy, review, and rescue lanes remain scale-to-zero
  and bounded by the pragmatic concurrency policy.
- One independent reviewer uses the Claude family by default where the
  specialized review role is available.

### As an operator debugging drift, I have a deterministic audit

- A script validates imports, required aliases, policy fragments, rig role and
  concurrency coverage, and stale Complete Delivery skill projections.
- Its cleanup mode removes only exact stale Complete Delivery skill symlinks.
- Pack tests, city validation, formula discovery, and live canaries establish
  that the intended routes actually work.

## Out Of Scope

- Rebuilding Complete Delivery or its 146-step graph.
- Deleting immutable releases, historical reports, beads, usage logs, or failed
  canary evidence.
- Making every gstack review, QA, security, and release formula mandatory.
- Adding new paid providers or changing repository product code.

## Other Notes

- Keep one durable `main`. A temporary PR branch is permitted only because the
  protected `gascity-packs` main requires reviews and checks; delete it after
  merge.
- Existing unrelated rig working-tree changes are user-owned and must remain
  untouched.
- Bead `gp-v1s6` is the durable delivery record.
