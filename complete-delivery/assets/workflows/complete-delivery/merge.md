Merge the externally green pull request without bypassing protection.

Re-run the PR gate against the current head immediately before merge. Read
`delivery.head_sha` from durable workflow-root metadata, require it to be a
nonempty full 40-character lowercase Git SHA, and assign it explicitly as
`DELIVERY_HEAD_SHA`. Require the PR-gate SHA to equal that value, then pass it as the atomic
expected-head guard: `gh pr merge <pr> --match-head-commit "$DELIVERY_HEAD_SHA"`
with the configured `merge_method` (`squash`, `merge`, or `rebase`). Never use
`--admin`, a force push, or a direct push to the protected base. If the head
moves, checks restart, approval is dismissed, or mergeability is unknown,
wait/reconcile through the prior gate rather than bypassing it. Preserve the
Formula check's existing three-attempt exhaustion evidence in `gc.attempt_log`:
each failed wait or reconcile attempt records its blocker, and exhaustion closes
with a non-pass outcome rather than resetting the count or merging.

After GitHub reports the PR merged, read `merge_commit_sha`, verify it is
reachable from `base_branch`, and record it as `delivery.merge_sha`. Record
merge time and method as evidence under `<artifact_root>/delivery/`.

Close with `gc.outcome=pass`; the graph check independently verifies GitHub
state and reachability. Do not invoke provider-native subagents.
