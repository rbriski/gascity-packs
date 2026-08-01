Prepare current-head external review reconciliation.

Read `delivery.repo`, `delivery.pr_number`, `delivery.pr_url`, and
`delivery.head_sha` from the workflow root. Confirm authenticated `gh` access
and locate `{{pack_root}}/assets/scripts/delivery_gate.py`. Ensure the artifact directory
`<artifact_root>/delivery/` exists. Do not mutate code in this setup lane.

Close with `gc.outcome=pass`. Do not invoke provider-native subagents.
