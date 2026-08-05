Prepare the gstack code-review context.

Collect implementation summaries, changed files, test evidence, requirements,
plan, decomposition, and any prior review reports into one context under the
artifact root. Record on the workflow root:

- `gc.build.code_review_context_path=<context path>`
- `gc.build.code_review_report_path=<planned synthesis report path>`
- `gc.build.code_review_status=ready`

Current review_mode is {{review_mode}}. The adapted upstream skills are review,
qa, cso, and investigate-style gap analysis.

Close with `gc.outcome=pass`.

Do not invoke provider-native subagents. Gas City graph lanes are the
delegation mechanism.
