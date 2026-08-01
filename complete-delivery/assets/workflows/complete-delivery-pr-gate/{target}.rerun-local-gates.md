Rerun the repository-native quality gates after review resolution.

Read `<artifact_root>/delivery/external-review-handoff.json` and verify that
`HEAD` is the exact recorded fix commit before testing. Invoke
`{{pack_root}}/assets/scripts/checks/delivery-local-gates.sh` with this claimed
bead as `GC_BEAD_ID`. The complete nonterminal local-gate set is the configured
`build_command`, `browser_test_command`, `security_command`, and
`extra_gate_command`, executed only through this script. Before invoking it,
inspect those configured commands. If any invokes `delivery_gate.py`,
`delivery-pr-approved.sh`, or a remote PR, CI, CodeRabbit, or human-review
approval gate, do not run it: record a blocker for the next terminal loop
check. Never run such a gate before publication. Fix any new regression and
repeat until every configured command passes, then record `tested_commit` and
the successful local-gate result in the same durable handoff artifact. Never
push or resolve a review thread in this lane. If no source changed because only
remote checks are pending, still record that the current commit passed the
local gate sequence.

Close with `gc.outcome=pass`. Do not invoke provider-native subagents.
