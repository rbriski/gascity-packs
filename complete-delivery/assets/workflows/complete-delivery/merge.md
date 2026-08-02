Merge the externally green pull request without bypassing protection.

Re-run the PR gate against the current head immediately before merge. Require
its SHA to equal `delivery.head_sha`. Use `gh pr merge` with configured
`merge_method` (`squash`, `merge`, or `rebase`), never `--admin`, never a force
push, and never direct-push to the protected base. If the head moves, checks
restart, approval is dismissed, or mergeability is unknown, wait/reconcile
through the prior gate rather than bypassing it.

After GitHub reports the PR merged, read `merge_commit_sha`, verify it is
reachable from `base_branch`, and record it as `delivery.merge_sha`. Record
merge time and method as evidence under `<artifact_root>/delivery/`.

Close with `gc.outcome=pass`; the graph check independently verifies GitHub
state and reachability. Do not invoke provider-native subagents.
