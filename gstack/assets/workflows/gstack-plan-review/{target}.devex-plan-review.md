Run the gstack developer-experience plan review lane.

Use the plan-devex-review posture when the work affects APIs, CLIs, SDKs,
docs, onboarding, install paths, or operational workflows. Check time to first
happy path, error messages, docs coverage, upgrade path, and the user's magical
moment. For product-only work, mark the lane not applicable.

Before reading, create and validate your fresh context:

```bash
.gc/scripts/checks/gstack-plan-review-context-valid.py --lane-inputs devex
```

Read only that attempt-local `context.json`. Write the report
exactly to `<artifact_root>/plan-review/<root-bead-id>/attempt-<N>/devex.md`.
Start it with `root_bead_id`, `source_bead_id`, `attempt`, `scope_ref`, and
`context_path` binding lines, set that exact path as
`gstack.plan_review.output_path`, then run:

```bash
.gc/scripts/checks/gstack-plan-review-context-valid.py --lane devex
```

Close with `gc.outcome=pass`,
`gstack.plan_review.devex_verdict=approve|iterate`, and
`gstack.plan_review.output_path=<devex review report path>`.

Do not invoke provider-native subagents. You are the developer-experience lane.
