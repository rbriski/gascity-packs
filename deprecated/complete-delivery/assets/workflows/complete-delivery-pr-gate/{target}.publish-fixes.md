Publish this iteration's verified review fixes.

Before every lock acquisition, push, refresh, provider poll, or thread-resolution
call, run `.gc/scripts/checks/delivery-external-review-deadline.sh --validate`.
Do not publish, poll, or resolve when it fails.

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
it, immediately run `.gc/scripts/checks/delivery-external-review-deadline.sh
--validate` before final checks or any mutation. After acquiring it, recheck
the clean tree and canonical `HEAD == tested_commit` while holding it. Bound lock acquisition
by the deadline's remaining time; if the lock wait expires or the post-lock
validation fails, write blocker-only state and perform no push, refresh, or
resolution. Specifically, unavailable lock, dirty tree, or mismatch fails closed before push,
refresh, or resolution; an expired deadline fails closed the same way. Push exactly `tested_commit` normally (never
force-push), or make no empty commit/push when source did not change; refresh
the PR, then immediately run `.gc/scripts/checks/delivery-external-review-deadline.sh
--validate` again before accepting or persisting any refreshed identity. An
expired post-refresh validation is a publication blocker. At that point,
immediately persist all refreshed workflow-root identity fields before any thread resolution:
exact full-SHA `delivery.head_sha`,
`delivery.repo`, `delivery.branch`, `delivery.pr_number`, and `delivery.pr_url`.
Also persist the handoff's exact full-SHA `published_head`, `tested_commit`, and
boolean `published_head_matches_tested_commit`. Immediately re-read and exactly
verify every just-written workflow-root and handoff identity field before any
thread resolution; persistence that is failed, partial, malformed, or
head-mismatched is a distinct publication blocker. On either blocker, clear all
authority fields (`tested_commit`, `local_gates`, `published_head`, and
`published_head_matches_tested_commit`), write blocker-only state, and keep
every mapped thread open. A deadline validation failure
is a deadline blocker, a refresh failure is a publication blocker, and a
thread-resolution failure is a resolution blocker; record those three failures
distinctly and never reuse one as success evidence for another. Only then
perform all permitted thread resolutions before releasing the lock. Immediately
after the final permitted resolution and before recording any pass outcome, run
`.gc/scripts/checks/delivery-external-review-deadline.sh --validate` again. If
that final validation fails, clear all authority fields (`tested_commit`,
`local_gates`, `published_head`, and `published_head_matches_tested_commit`),
write blocker-only expiry evidence, preserve the truth of any already-resolved
threads, and close with a non-pass outcome. An already-resolved thread state
cannot authorize continuation, and this lane must not claim to reopen a thread
without a supported reopening operation. No push may occur after that final
head check and before all resolution calls finish. A new head invalidates old
CI and any configured optional-provider evidence for the next loop check. When
`coderabbit` is `off`, never request, poll, wait for, or resolve CodeRabbit
threads.
For every current-attempt mapped finding, resolve a valid thread only when its
fix evidence passed, and resolve an invalid, superseded, or otherwise
non-actionable thread only when its disposition evidence was published. Still
under that lock, every resolution requires `published_head` is exactly equal to
the artifact's `tested_commit` (`published_head == tested_commit`) and
`published_head_matches_tested_commit` is true. Commit containment alone is not sufficient.
On failed, blocked, skipped, unavailable, malformed, or stale push/refresh that
invalidates the whole handoff, clear `tested_commit` and `local_gates`, record a
publication failure, write blocker-only state, keep every mapped thread open,
and do not record passing publication evidence. That blocker-only state must
also clear `published_head` and `published_head_matches_tested_commit` before
it can reacquire a current PR head that is a full SHA before another inspection or
gate run. Only when a successful refresh returns a
different full-SHA may that `published_head` be next-iteration state, not a
publication failure; it keeps every mapped thread open and cannot authorize
resolution.

Close with `gc.outcome=pass` only after successful publication, or after a
recoverable publication failure that has another bounded iteration remaining.
An exhausted deadline, attempt budget, or recovery budget is a non-pass
outcome: write blocker-only evidence and do not publish `gc.outcome=pass`.
Do not merge or invoke provider-native subagents.
