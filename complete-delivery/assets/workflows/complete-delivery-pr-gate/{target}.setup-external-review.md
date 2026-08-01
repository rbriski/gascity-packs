Prepare current-head external review reconciliation.

Read `delivery.repo`, `delivery.pr_number`, `delivery.pr_url`, and
`delivery.head_sha` from the workflow root. Prove all three prerequisites before
recording success: authenticated `gh` access must succeed, the exact
`{{pack_root}}/assets/scripts/delivery_gate.py` file must exist and be readable,
and `<artifact_root>/delivery/` must be created and writable. Do not treat a
configured path, a stale directory, or an unauthenticated `gh` binary as proof.

Before checking prerequisites, invalidate any prior handoff success evidence:
clear `tested_commit`, `local_gates`, `published_head`, and
`published_head_matches_tested_commit` (or overwrite it with explicit failed
evidence). If any prerequisite is failed, blocked, skipped, or unavailable,
perform the same invalidation, record the blocker, and fail closed; never set
`gc.outcome=pass`. Only after all three proofs succeed may this lane close with
`gc.outcome=pass`. Do not mutate code or invoke provider-native subagents.
