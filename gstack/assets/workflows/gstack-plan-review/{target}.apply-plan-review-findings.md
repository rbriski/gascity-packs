Apply gstack plan-review findings.

Before reading or editing, run:

```bash
.gc/scripts/checks/gstack-plan-review-context-valid.py --apply-inputs
```

It accepts only the current attempt's root-bound synthesis and lane outputs.
Read that synthesis and update only the bound plan artifact in place when
required fixes remain. Keep optional ambition clearly separated from accepted scope. In
interactive mode, only add new scope after explicit approval is recorded; in
autonomous mode, preserve optional scope as deferred follow-up.

Write the remediation summary exactly to
`<artifact_root>/plan-review/<root-bead-id>/attempt-<N>/remediation.md`, include
the five binding lines, set `design_review.report_path` and
`gstack.plan_review.output_path` to that exact path, then run `--apply` again
before closing.

Set `design_review.verdict=done` only when founder, design, engineering, and
developer-experience lanes approve. Set `design_review.verdict=iterate` when
required plan fixes remain.

Close with `gc.outcome=pass`,
`design_review.verdict=done|iterate`,
`design_review.report_path=<plan review summary path>`, and
`gstack.plan_review.output_path=<plan review summary path>`.

Do not invoke provider-native subagents. This Gas City graph lane is the plan
fix delegation mechanism.
