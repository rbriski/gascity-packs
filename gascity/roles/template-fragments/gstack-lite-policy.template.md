{{ define "gstack-lite-policy" -}}
# Gstack Lite Delivery Policy

For ordinary software work, use one durable bead, one implementation owner,
repository-native checks, one independent review for material changes, at most
one repair owner, protected publication, deployment, smoke verification, and
concise wall-clock/rework accounting.

- Never install or launch Complete Delivery, `gstack-build`, `build-basic`,
  review fan-out, or another large GraphV2 delivery workflow by default.
- Record an exclusive write lease on the bead before editing: owner session,
  repository, branch/worktree, and source head. A rescue or repair replaces
  that lease; it never overlaps it.
- Before reassignment, drain the prior session, require `drain-check` success,
  and close or kill the exact session if it does not acknowledge promptly.
- Bind checks and review to an immutable candidate head. Reuse green evidence
  only when the head and check definition are identical.
- Use one structured review artifact and one focused repair. Escalate instead
  of starting serial retries or late competing writers.
- Add gstack planning, design, QA, security, migration, documentation, or
  release skills only when the changed surface warrants that gate.
- Preserve one durable `main`; delete a protection-required PR branch after
  merge.
- “Implemented,” “merged,” and “verified in production” are distinct states.
  Continue until the requested terminal state is proven.
{{- end }}
