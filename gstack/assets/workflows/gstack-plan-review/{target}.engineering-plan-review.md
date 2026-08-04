Run the gstack engineering plan review lane.

Use the plan-eng-review posture: check architecture, data flow, edge cases,
test coverage, performance, observability, distribution, and scope complexity.
Flag any plan that introduces unnecessary moving parts or skips verification
that is cheap to add with an automated factory.

Before reading, create and validate your fresh context:

```bash
.gc/scripts/checks/gstack-plan-review-context-valid.py --lane-inputs engineering
```

Read only that attempt-local `context.json`. Write the report
exactly to `<artifact_root>/plan-review/<root-bead-id>/attempt-<N>/engineering.md`.
Start it with `root_bead_id`, `source_bead_id`, `attempt`, `scope_ref`, and
`context_path` binding lines, set that exact path as
`gstack.plan_review.output_path`, then run:

```bash
.gc/scripts/checks/gstack-plan-review-context-valid.py --lane engineering
```

Close with `gc.outcome=pass`,
`gstack.plan_review.engineering_verdict=approve|iterate`, and
`gstack.plan_review.output_path=<engineering review report path>`.

Do not invoke provider-native subagents. You are the engineering review lane.
