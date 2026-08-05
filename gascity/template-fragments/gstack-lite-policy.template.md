{{ define "gstack-lite-policy" -}}
# Gstack Lite Delivery Policy

For ordinary software work, use the lightweight path: one durable bead, one
implementation owner, repository-native checks, one independent review for
material changes, at most one repair cycle, protected publication, deployment,
smoke verification, and concise wall-clock/rework accounting.

- Keep the persistent Mayor on Sol/high for responsive intake and final
  adjudication. Route explicit research or planning deliverables to the
  scale-to-zero `sol-research` Sol/max lane with `gc sling ... --no-formula`;
  the user remains in the Mayor session. Cap the lane at one active session in
  the city and every rig; audit newly added rigs for the same singleton patch.
- Never install or launch Complete Delivery, `gstack-build`, `build-basic`,
  review fan-out, or another retired delivery graph.
- Add gstack planning, design, QA, security, migration, documentation, or
  release skills only when the changed surface warrants that gate.
- Keep at most two independent implementation writers and one reviewer.
  Escalation replaces a writer; it does not add concurrency.
- Record one exclusive write lease per bead/branch/worktree. Before rescue or
  repair, drain and verify the prior owner is stopped; reject late commits from
  a revoked lease.
- Bind checks and review to an immutable candidate head. Reuse green evidence
  only when both the head and check definition match.
- Use one structured review artifact and at most one focused repair. A rescue
  lane must reproduce or edit within four minutes or return the evidence.
- Preserve one durable `main`; delete any protection-required PR branch after
  merge.
- “Implemented,” “merged,” and “verified in production” are distinct states.
  Continue until the user's requested terminal state is proven.
{{- end }}
