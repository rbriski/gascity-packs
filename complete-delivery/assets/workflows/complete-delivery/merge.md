Merge the externally green pull request without bypassing protection.

Re-run the PR gate against the current head immediately before merge. Read
`delivery.head_sha` from durable workflow-root metadata, require it to be a
nonempty full 40-character lowercase Git SHA, and assign it explicitly as
`DELIVERY_HEAD_SHA`. Require the PR-gate SHA to equal that value, then pass it as the atomic
expected-head guard. Resolve the previously validated durable `delivery.pr_url`
into a nonempty `DELIVERY_PR_URL`. Merge only with the canonical guarded
command below after selecting its method flag. Read `gc.var.merge_method` and
require exactly one of `squash`, `merge`, or `rebase`. Assign it explicitly as
`MERGE_METHOD`. Map that validated value before merging: `squash` to
`--squash`, `merge` to `--merge`, and `rebase` to `--rebase`; reject every
other value. Never use
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

Before rerunning the open-PR gate after any interrupted attempt, reconcile the
recorded `delivery.pr_url`, `delivery.repo`, `delivery.pr_number`,
`delivery.head_sha`, and configured `gc.var.base_branch` against a fresh
`gh api repos/<repo>/pulls/<number>` response. Require the response number and
URL to equal the recorded identity, its base repository and `base.ref` to equal
the configured repository and base branch. Require `merged` to be a JSON
Boolean. If it is `true`,
require a nonempty full merge SHA, verify that SHA is reachable from the
configured base branch (for example with `gh api repos/<repo>/compare/<merge-sha>...<base-branch>` and only `identical` or `ahead`), then persist that exact SHA as
`delivery.merge_sha` before continuing. This recovery is idempotent: the same
validated SHA may be recorded again, but no merge command may run. Record the
GitHub `merged_at` value and reconciliation result in the normal merge evidence
under `<artifact_root>/delivery/`, then close with `gc.outcome=pass`.

Only invoke `gh pr merge` when that fresh response has `state=open`,
`merged=false`, and the exact recorded head SHA. For that open, unmerged state,
require the fresh head SHA to equal `delivery.head_sha`; an already-merged
recovery validates its durable identity, base, Boolean, merge SHA, and
reachability without requiring a mutable current head. Any other state is a
non-pass reconciliation outcome; do not send it through the open-PR gate as if
it were still mergeable.

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
