Synthesize the gstack plan review.

Before reading any report, run:

```bash
.gc/scripts/checks/gstack-plan-review-context-valid.py --synthesis-inputs
```

It prints one JSON manifest. Read exactly the four lane reports in
`permitted_input_paths`, then write only to its sole `permitted_output_paths`
entry. Do not infer paths from the repository or a prior attempt. Include the
five manifest binding lines (`root_bead_id`, `source_bead_id`, `attempt`,
`scope_ref`, and `context_path`), and set both
`gstack.plan_review.synthesis_path` and `gstack.plan_review.output_path` to
that exact path.
Deduplicate findings, preserve lane attribution, and classify each item as
required fix, optional expansion, deferred follow-up, or residual risk.

Write one synthesis with the final recommendation and the exact plan edits
needed before implementation, then run `--synthesis` before closing.

Close with `gc.outcome=pass`,
`gstack.plan_review.synthesis_path=<plan review synthesis path>`, and
`gstack.plan_review.output_path=<plan review synthesis path>`.

Do not invoke provider-native subagents. Synthesis happens in this Gas City
fan-in lane.
