Run the gstack design plan review lane.

Use the plan-design-review posture: rate design completeness, identify what a
10 looks like, check reuse of existing design patterns, and call out missing
states, responsive behavior, accessibility, or visual evidence. For non-UI
work, explicitly mark this lane as not applicable and explain why.

Before reading, create and validate your fresh context:

```bash
.gc/scripts/checks/gstack-plan-review-context-valid.py --lane-inputs design
```

Read only that attempt-local `context.json`. Write the report
exactly to `<artifact_root>/plan-review/<root-bead-id>/attempt-<N>/design.md`.
Start it with `root_bead_id`, `source_bead_id`, `attempt`, `scope_ref`, and
`context_path` binding lines, set that exact path as
`gstack.plan_review.output_path`, then run:

```bash
.gc/scripts/checks/gstack-plan-review-context-valid.py --lane design
```

Close with `gc.outcome=pass`,
`gstack.plan_review.design_verdict=approve|iterate`, and
`gstack.plan_review.output_path=<design review report path>`.

Do not invoke provider-native subagents. You are the design review lane.
