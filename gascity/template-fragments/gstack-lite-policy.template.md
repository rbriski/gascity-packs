{{ define "gstack-lite-policy" -}}
# Gstack Lite Delivery Policy

For ordinary software work, use the lightweight path: one durable bead, one
implementation owner, repository-native checks, one independent review for
material changes, at most one repair cycle, protected publication, deployment,
smoke verification, and concise wall-clock/rework accounting.

- Never install or launch the deprecated Complete Delivery pack.
- Do not default to `gstack-build`, `build-basic`, review fan-out, or a large
  GraphV2 workflow. Those require explicit user intent or a demonstrated risk
  that the lightweight path cannot cover.
- Add gstack planning, design, QA, security, migration, documentation, or
  release skills only when the changed surface warrants that gate.
- Keep at most two independent implementation writers and one reviewer.
  Escalation replaces a writer; it does not add concurrency.
- Preserve one durable `main`; delete any protection-required PR branch after
  merge.
- “Implemented,” “merged,” and “verified in production” are distinct states.
  Continue until the user's requested terminal state is proven.
{{- end }}
