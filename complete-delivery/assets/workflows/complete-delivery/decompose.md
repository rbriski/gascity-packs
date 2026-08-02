Create the Complete Delivery implementation convoy from the approved,
source-grounded plan.

Read `gc.var.source_bead_id`, its durable intent, and the approved plan before
creating any implementation work. Each implementation bead must state how it
advances the source acceptance criteria; do not turn an unrelated repository
HEAD change into the delivery outcome. Record the source ID in the convoy and
link source-anchor beads back to the workflow root so the final trace remains
auditable.

In the decomposition artifact YAML front matter, record `source.id`,
`source.title`, and `source.anchor: gc:<source-id>` using the exact durable
values. Start its body with a `Source Intent` section naming that same source.

Each bead must map to one vertical slice and include acceptance criteria, likely
files/modules, a first verification command, and expected proof command. Do
not copy review procedure or sensitive source notes into implementation beads.

Record `gc.input_convoy_id` on the current step, create the implementation
convoy, and ensure it is discoverable from the workflow root. Close with
`gc.outcome=pass`. Do not invoke provider-native subagents.

Artifact validation: this stage is gated by
`.gc/scripts/checks/delivery-source-artifact-valid.sh`, which validates the
artifact recorded at `gc.build.decomposition_path` (fallback
`gc.var.decomposition_path`) against schema `gc.build.decomposition.v1` and
the exact durable source fields.
