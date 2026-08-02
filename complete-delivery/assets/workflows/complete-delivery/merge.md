Merge the externally green pull request without bypassing protection.

Re-run the PR gate against the current head immediately before merge. Read
`delivery.head_sha` from durable workflow-root metadata, require it to be a
nonempty full 40-character lowercase Git SHA, and assign it explicitly as
`DELIVERY_HEAD_SHA`. Require the PR-gate SHA to equal that value, then pass it as the atomic
expected-head guard. Resolve the previously validated durable `delivery.pr_url`
into a nonempty `DELIVERY_PR_URL`. Merge only with the canonical guarded
command below after selecting its method flag. Read
`gc.var.merge_method`, require one of those three exact values, and assign it
explicitly as `MERGE_METHOD`. Map that validated value before merging: `squash`
to `--squash`, `merge` to `--merge`, and `rebase` to `--rebase`; reject every
other value. Use the
selected flag together with `--match-head-commit`. Never use
`--admin`, a force push, or a direct push to the protected base. If the head
moves, checks restart, approval is dismissed, or mergeability is unknown,
wait/reconcile through the prior gate rather than bypassing it. Preserve the
Formula check's existing three-attempt exhaustion evidence in `gc.attempt_log`:
each failed wait or reconcile attempt records its blocker, and exhaustion closes
with a non-pass outcome rather than resetting the count or merging.

Immediately before selecting the merge flag and invoking the guarded command,
re-read the PR's `base.ref` from GitHub. Require configured
`gc.var.base_branch` to be nonempty and require the freshly read `base.ref` to
equal it exactly. On a missing value or mismatch, fail closed and return to the
prior gate; do not rely on the earlier base or invoke `gh pr merge`.

Use this explicit selection immediately before the guarded merge command:

```bash
case "$MERGE_METHOD" in
  squash) MERGE_FLAG=--squash ;;
  merge) MERGE_FLAG=--merge ;;
  rebase) MERGE_FLAG=--rebase ;;
  *) echo "unsupported merge_method: $MERGE_METHOD" >&2; exit 1 ;;
esac
gh pr merge "$DELIVERY_PR_URL" "$MERGE_FLAG" --match-head-commit "$DELIVERY_HEAD_SHA"
```

After GitHub reports the PR merged, read `merge_commit_sha`, verify it is
reachable from the configured `gc.var.base_branch`, and record it as
`delivery.merge_sha`. Record
merge time and method as evidence under `<artifact_root>/delivery/`.

Close with `gc.outcome=pass`; the graph check independently verifies GitHub
state and reachability. Do not invoke provider-native subagents.
