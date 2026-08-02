Publish this iteration's verified review fixes.

Read `<artifact_root>/delivery/external-review-handoff.json`; before every
attempt, replace stale prior-attempt state while retaining and validating the
current attempt's `tested_commit` and passed `local_gates`. Clear only stale
`published_head` and equality success evidence before push or refresh. Require
passed local gates for its exact commit, a clean checkout, and
`HEAD == tested_commit`; do not mutate after testing. A newly discovered
source fix returns to `rerun-local-gates` for a fresh committed candidate and
complete retest. Acquire one shared repository-scoped publication lock before
any push, no-push refresh, final head check, or `resolveReviewThread` call;
every path that can push this PR must acquire that same lock. After acquiring
it, recheck the clean tree and canonical `HEAD == tested_commit` while holding
it; unavailable lock, dirty tree, or mismatch fails closed before push,
refresh, or resolution. Push exactly `tested_commit` normally (never
force-push), or make no empty commit/push when source did not change; refresh
the PR, persist its full-SHA `published_head`, `tested_commit`, and boolean
`published_head_matches_tested_commit`, then perform all permitted thread
resolutions before releasing the lock. No push may occur after that final head
check and before all resolution calls finish. Record the current head on the
workflow root as `delivery.head_sha`; a new head invalidates old CI and
CodeRabbit evidence for the next loop check.
For every current-attempt mapped finding, resolve a valid thread only when its
fix evidence passed, and resolve an invalid, superseded, or otherwise
non-actionable thread only when its disposition evidence was published. Still
under that lock, every resolution requires `published_head` is exactly equal to
the artifact's `tested_commit` (`published_head == tested_commit`) and
`published_head_matches_tested_commit` is true. Commit containment alone is not sufficient.
On failed, blocked, skipped, unavailable, malformed, or stale push/refresh that
invalidates the whole handoff, clear `tested_commit` and `local_gates`, record a
publication failure, write blocker-only state, keep every mapped thread open,
and do not record passing publication evidence, then reacquire a current PR head that is a full SHA before
another inspection or gate run. Only when a successful refresh returns a
different full-SHA may that `published_head` be next-iteration state, not a
publication failure; it keeps every mapped thread open and cannot authorize
resolution.

Close with `gc.outcome=pass`. Do not merge or invoke provider-native
subagents.
