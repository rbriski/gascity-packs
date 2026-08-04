Synthesize the gstack plan review.

Before reading any report, run:

```bash
.gc/scripts/checks/gstack-plan-review-context-valid.py --synthesis-inputs
```

It requires all four current-attempt, root-bound lane outputs. Read only those
validated founder, design, engineering, and developer-experience reports. Write
the synthesis exactly to
`<artifact_root>/plan-review/<root-bead-id>/attempt-<N>/synthesis.md`, include
the five binding lines (`root_bead_id`, `source_bead_id`, `attempt`,
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
