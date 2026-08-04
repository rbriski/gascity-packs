Draft a Complete Delivery plan grounded in the durable source intent.

Before reading source intent or writing, run
`.gc/scripts/checks/delivery-source-artifact-valid.sh --context plan`.
It prints the exact canonical input/output paths and, on a Ralph retry, the
validated logical control and its exact `gc.attempt_log` failure reason. Use
only those paths and repair in place. A missing or mismatched control, attempt
log, artifact root, or path authority is a fail-closed blocker.

Before examining repository HEAD as a possible solution, resolve
`gc.var.source_bead_id` and read that source bead or convoy's title,
description, acceptance criteria, and relevant notes. The source intent is the
requested outcome; code already present in the checkout is evidence only and
must not replace it. If the source is missing or ambiguous, fail closed rather
than planning from the checkout.

Write the plan to `{{plan_path}}` and record that exact resolved path on the
workflow root as `gc.build.plan_path`. In YAML front matter, set
`schema: gc.build.plan.v1`, `status: approved`, and record `source.id`,
`source.title`, `source.anchor: gc:<source-id>`, and
`source.acceptance_criteria_sha256` using the exact durable values resolved
above. The hash is `sha256:` plus the SHA-256 digest of the exact JSON
`acceptance_criteria` string returned by `gc bd show <source-id> --json`,
byte-for-byte without trimming or reformatting; do not copy the raw criteria
into that source mapping. Use safe YAML string serialization for every string-valued source field; never interpolate raw source values where quotes,
newlines, or YAML syntax could change the artifact structure. Start the body with the exact unfenced H2 heading `## Source Intent`
and place the resolved source ID and title immediately below it, then map every
requested acceptance criterion to a planned change or
explicit verification. Include at least two implementation approaches, the
recommendation, task boundaries, tests, release risks, and out-of-scope work.
Preserve the source ID in the plan's traceability section. Do not copy raw
source notes into public-report inputs.

Close with `gc.outcome=pass` and the plan artifact path. Do not invoke
provider-native subagents.

Artifact validation: this stage is gated by
`.gc/scripts/checks/delivery-source-artifact-valid.sh`, which validates the
artifact recorded at `gc.build.plan_path` (fallback `gc.var.plan_path`) against
schema `gc.build.plan.v1` and the exact durable source fields.
