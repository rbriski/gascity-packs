Run one bounded current-head external-review iteration.

Before admitting any child action or running the terminal check, require
`.gc/scripts/checks/delivery-external-review-deadline.sh --validate`.
The terminal checker independently repeats this validation before its provider
action; no expired or rewritten deadline may authorize another iteration.

The children inspect evidence, resolve valid findings, rerun exact local gates,
publish fixes, and update the living report. The loop check then evaluates
required CI, CodeRabbit completion, all live unresolved review threads, human
change requests, PR/draft state, and head stability.

Keep the child report pre-terminal: it must leave `external-review` `active`
and must not publish `passed` or a protected-merge next action. Only this
terminal mechanical check, followed by the post-check `{target}.md` finalizer,
may authorize that passed report and protected-merge publication.

Never weaken a gate or resolve a thread unless the durable handoff proves
`published_head` is exactly equal to `tested_commit`; commit containment or a
pushed fix alone is not sufficient. The mechanical check owns the terminal
decision. Treat any failed, blocked, skipped, unavailable, stale, malformed,
or head-mismatched child evidence as fail-closed: invalidate stale
`tested_commit`, `local_gates`, `published_head`, and
`published_head_matches_tested_commit` before the next child can consume it.
Do not invoke provider-native subagents.
