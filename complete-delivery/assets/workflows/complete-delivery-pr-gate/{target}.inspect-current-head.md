Snapshot the pull request's current-head delivery gate.

Read and canonicalize workflow-root `delivery.head_sha` as a full 40-character
SHA before invoking the gate. Remove any pre-existing
`<artifact_root>/delivery/pr-gate.json` first, then run
`{{pack_root}}/assets/scripts/delivery_gate.py` with workflow-root repo/PR,
`required_checks`, and `coderabbit`, writing that path. Never consume a
pre-existing artifact after a command failure. A blocked gate exit is expected
while work remains only when this invocation produced fresh, well-formed JSON
whose canonical full `head_sha` exactly equals workflow-root
`delivery.head_sha`; preserve that fresh blocked snapshot and close this
inspection lane with `gc.outcome=pass` so repair children can act.

If invocation fails without fresh valid JSON, the JSON is malformed, is missing
`head_sha`, or its full canonical head differs from `delivery.head_sha`, invalidate stale
`tested_commit`, `local_gates`, `published_head`, and
`published_head_matches_tested_commit`, record the failure, and fail closed.
An API/input error is not a finding. Record the inspected head SHA and concise
blocker list. Do not mutate code or invoke provider-native subagents.
