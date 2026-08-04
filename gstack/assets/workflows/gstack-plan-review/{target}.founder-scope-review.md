Run the gstack founder scope review lane.

Use the plan-ceo-review posture: challenge the premise, compare minimal viable
and ideal architecture approaches, check whether the plan solves the real
outcome, and name any 10-star product opportunity that is simple enough to be
worth surfacing.

Before reading, create and validate your fresh context:

```bash
.gc/scripts/checks/gstack-plan-review-context-valid.py --lane-inputs founder
```

Read only that attempt-local `context.json`. Write the report
exactly to `<artifact_root>/plan-review/<root-bead-id>/attempt-<N>/founder.md`.
Start it with `root_bead_id`, `source_bead_id`, `attempt`, `scope_ref`, and
`context_path` binding lines, then set that exact path as
`gstack.plan_review.output_path`. Before closing, run:

```bash
.gc/scripts/checks/gstack-plan-review-context-valid.py --lane founder
```

Distinguish required plan fixes from optional ambition and deferred ideas.

Close with `gc.outcome=pass`,
`gstack.plan_review.founder_verdict=approve|iterate`, and
`gstack.plan_review.output_path=<founder review report path>`.

Do not invoke provider-native subagents. You are the founder review lane.
