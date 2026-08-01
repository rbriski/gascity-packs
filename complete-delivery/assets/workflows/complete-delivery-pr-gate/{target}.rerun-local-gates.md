Rerun the repository-native quality gates after review resolution.

Read `<artifact_root>/delivery/external-review-handoff.json` and require a
clean checkout (`git status --porcelain` has no output) before testing. Verify
that `HEAD` is its exact full-SHA `candidate_commit` before testing. The
resolver must set that candidate to `inspected_head` when no source changed, or
to the final committed `HEAD` after every valid source fix in the iteration
otherwise; individual thread `fix_commit` values remain thread evidence, not
the test candidate. A regression fix discovered while running this sequence
must be committed, recorded as the new `candidate_commit`, and the complete
sequence rerun from the start. Invoke
`{{pack_root}}/assets/scripts/checks/delivery-local-gates.sh` with this claimed
bead as `GC_BEAD_ID`. The complete nonterminal local-gate set is the configured
`setup_command`, `lint_command`, `typecheck_command`, `test_command`,
`build_command`, `browser_test_command`, `security_command`, and
`extra_gate_command`, executed only through this script. Before invoking it,
inspect those configured commands. If any invokes `delivery_gate.py`,
`delivery-pr-approved.sh`, or a remote PR, CI, CodeRabbit, or human-review
approval gate, do not run it: record a blocker for the next terminal loop
check. The script mechanically rejects the pack's terminal scripts, `gh`,
CodeRabbit, GitHub provider-API URLs, and clearly named remote-approval or
approval-gate wrappers; inspection remains mandatory for a repository-local
wrapper with a different name. Never run such a gate before publication. Fix
any new regression and repeat until every configured command passes. Repository
gate configuration is trusted policy; this validation is not an adversarial
shell sandbox. After every configured command passes, require the checkout to
remain clean and `HEAD` to still equal `candidate_commit`; otherwise do not
record passing evidence. Then record `candidate_commit` as a full-SHA
`tested_commit`, matching `local_gates.tested_commit`, and
`local_gates.status: "passed"` in the same durable handoff artifact. A blocked
or skipped local gate is terminal evidence of failure, never a passing result.
Never push or resolve a review thread in this lane. If no source changed
because only remote checks are pending, test and record the inspected-head
candidate through the same local gate sequence.

Close with `gc.outcome=pass`. Do not invoke provider-native subagents.
