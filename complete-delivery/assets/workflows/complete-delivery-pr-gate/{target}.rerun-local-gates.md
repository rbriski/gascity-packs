Rerun the repository-native quality gates after review resolution.

Read `<artifact_root>/delivery/external-review-handoff.json` and verify that
`HEAD` is the exact recorded fix commit before testing. Invoke
`{{pack_root}}/assets/scripts/checks/delivery-local-gates.sh` with this claimed
bead as `GC_BEAD_ID`. This is the complete nonterminal local-gate set: run only
this script and the repository-native commands it invokes. Do not invoke
`delivery_gate.py`, `delivery-pr-approved.sh`, or any remote PR, CI, CodeRabbit,
or human-review approval gate before publication. Fix any new regression and
repeat until every configured command passes, then record `tested_commit` and
the successful local-gate result in the same durable handoff artifact. Never
push or resolve a review thread in this lane. If no source changed because only
remote checks are pending, still record that the current commit passed the
local gate sequence.

Close with `gc.outcome=pass`. Do not invoke provider-native subagents.
