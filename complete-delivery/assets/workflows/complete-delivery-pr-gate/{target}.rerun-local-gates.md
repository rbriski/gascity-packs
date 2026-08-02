Rerun the repository-native quality gates after review resolution.

Before each regression-repair attempt and before starting the local-gate command,
run `.gc/scripts/checks/delivery-external-review-deadline.sh --validate`.
Do not repair or test when it fails.

Read `<artifact_root>/delivery/external-review-handoff.json` and, before every
test attempt, clear prior `tested_commit`, `local_gates`, `published_head`, and
`published_head_matches_tested_commit` success evidence. Require a clean
checkout (`git status --porcelain` has no output) before testing. Verify
that `HEAD` is its exact full-SHA `candidate_commit` before testing. The
resolver must set that candidate to `inspected_head` when no source changed, or
to the final committed `HEAD` after every valid source fix in the iteration
otherwise; individual thread `fix_commit` values remain thread evidence, not
the test candidate. Permit at most three complete regression-repair-and-rerun attempts per Formula iteration: count each regression repair, commit and record it as the new
`candidate_commit`, then rerun the complete sequence from the start. If a fourth repair is required, stop committing, replace the entire handoff with blocker-only retry-exhausted evidence containing no authority fields,
and close with a non-pass outcome. Invoke
`{{pack_root}}/assets/scripts/checks/delivery-local-gates.sh` with this claimed
bead as `GC_BEAD_ID`. The complete nonterminal local-gate set is the configured
`setup_command`, `lint_command`, `typecheck_command`, `test_command`,
`build_command`, `browser_test_command`, `security_command`, and
`extra_gate_command`, executed only through this script. Before invoking it,
inspect those configured commands. If any invokes `delivery_gate.py`,
`delivery-pr-approved.sh`, or a remote PR, CI, CodeRabbit, or human-review
approval gate, do not run it: record a blocker for the next terminal loop
check. The script parses each configured value as a restricted, shell-free argv
command: one executable followed by literal arguments, with ordinary quotes and
backslash escapes resolved. It rejects control/redirection and grouping
operators, command/process substitution, parameter or arithmetic expansion,
globs, brace expansion, `eval`, and nested-shell wrappers before execution.
The only interpolation is `${GC_SESSION_ID:-manual}` in a non-executable
argument, resolved by the script as a single literal value. The parsed
executable is checked against the canonical terminal/provider denylist (the
pack's terminal scripts, `gh`, CodeRabbit, and approval wrappers); provider API
URLs are rejected too. Inspection remains mandatory for a repository-local
wrapper with a different name. Never run such a gate before publication. Fix
any new regression and repeat until every configured command passes. Repository
gate configuration is trusted policy; the restricted argv boundary prevents it
from being interpreted as shell source. After every configured command passes, require the checkout to
remain clean and `HEAD` to still equal `candidate_commit`; otherwise do not
record passing evidence. Then record `candidate_commit` as a full-SHA
`tested_commit`, matching `local_gates.tested_commit`, and
`local_gates.status: "passed"` in the same durable handoff artifact. A failed,
blocked, skipped, unavailable, or mismatched local gate must leave all of those
success fields cleared or explicitly overwritten as failed; it is terminal
evidence of failure, never a passing result.
Never push or resolve a review thread in this lane. If no source changed
because only remote checks are pending, test and record the inspected-head
candidate through the same local gate sequence.

Close with `gc.outcome=pass` only after the full local-gate sequence passes; otherwise close with a non-pass outcome. Do not invoke provider-native subagents.
