Run the gstack design plan review lane.

Use the plan-design-review posture: rate design completeness, identify what a
10 looks like, check reuse of existing design patterns, and call out missing
states, responsive behavior, accessibility, or visual evidence. For non-UI
work, explicitly mark this lane as not applicable and explain why.

Before reading, create and validate your fresh context:

```bash
.gc/scripts/checks/gstack-plan-review-context-valid.py --lane-inputs design
```

The command prints one JSON manifest. Read exactly its `plan_path` and
`review_context_path`; write only to its sole `permitted_output_paths` entry.
Do not infer paths from the repository or a prior attempt.
Start it with `root_bead_id`, `source_bead_id`, `attempt`, `scope_ref`, and
`context_path` binding lines, set the manifest output path as
`gstack.plan_review.output_path`, then run:

```bash
.gc/scripts/checks/gstack-plan-review-context-valid.py --lane design
```

Close with `gc.outcome=pass`,
`gstack.plan_review.design_verdict=approve|iterate`, and
`gstack.plan_review.output_path=<design review report path>`.

Do not invoke provider-native subagents. You are the design review lane.
