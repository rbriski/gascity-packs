Apply gstack plan-review findings.

Before reading or editing, run:

```bash
.gc/scripts/checks/gstack-plan-review-context-valid.py --apply-inputs
```

It prints one JSON manifest. Read exactly its synthesis and plan paths in
`permitted_input_paths`. Write only to the bound plan and remediation paths in
`permitted_output_paths`; do not infer paths from the repository or a prior
attempt. Update the bound plan artifact in place when required fixes remain.
Keep optional ambition clearly separated from accepted scope. In interactive
mode, only add new scope after explicit approval is recorded; in autonomous
mode, preserve optional scope as deferred follow-up.

Include the five manifest binding lines, set `design_review.report_path` and
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
