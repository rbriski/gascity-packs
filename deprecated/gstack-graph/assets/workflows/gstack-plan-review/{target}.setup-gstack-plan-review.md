Prepare the gstack plan-review context.

Before reading or writing any review material, run:

```bash
.gc/scripts/checks/gstack-plan-review-context-valid.py --prepare
```

This resolves `gc.root_bead_id` and, **only from that durable root**, the
exact `gc.var.source_bead_id`, `gc.var.artifact_root`,
`gc.build.plan_review_context_path`, and `gc.build.plan_path`. It creates an
checksummed root binding at
`<artifact_root>/plan-review/<root-bead-id>/binding.json` and records its path
on the root. Each fresh lane derives its own attempt-local context from that
binding. Do not copy input paths from a prior lane, retry, or unrelated
workflow; blank, stale, cross-source, missing, or escaping paths are a hard
failure.

Use that binding to collect requirements, plan, acceptance criteria, test
commands, release risks, and unresolved decisions. The next fanout may only
review the exact paths named by the binding.

Current interaction_mode is {{interaction_mode}}. The adapted upstream skills
are plan-ceo-review, plan-design-review, plan-eng-review, and plan-devex-review.

Close with `gc.outcome=pass`.

Do not invoke provider-native subagents. Gas City graph lanes are the
delegation mechanism.
