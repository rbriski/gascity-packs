---
name: complete-delivery
description: Carry authorized software work from product intent through implementation, repository-native tests, authoritative internal review, GitHub checks, bounded fixes, protected merge, deployment, production verification, and a living HTML report. Use when the user says “complete this work,” “take this all the way,” “ship it,” “deploy it,” or otherwise defines done as more than producing code or a pull request.
---

# Complete Delivery

Treat the requested production outcome—not code generation—as the terminal
condition. Use the pack's Formula v2 lifecycle so every gate is durable,
observable, retryable, and recoverable.

## Launch

1. Resolve the rig that owns the code and the durable work bead. Create a bead
   in that rig only when none exists.
2. Confirm that the rig has a Complete Delivery profile for its exact local
   gates, required checks, optional CodeRabbit posture, deployment, smoke test, and
   report publication. Add missing durable configuration before launch; do not
   supply recurring settings ad hoc on every run.
3. Start the one-step lifecycle:

   ```sh
   gc complete-delivery delivery start <bead-id> --rig <rig>
   ```

   Use `--interactive` only when the user wants planning checkpoints. The
   default is autonomous execution with agent review and bounded fix loops.

4. Monitor the workflow, acting on real failures. Do not ask the user to run a
   routine test, recheck a PR, merge, deploy, or refresh the report.

## Terminal contract

Do not call the work complete until all applicable evidence exists:

- approved requirements and implementation plan;
- implementation plus focused and repository-wide local gates;
- independent code review, QA, and release-readiness approval;
- one non-draft PR whose current head passes required CI;
- no unresolved human review threads or change requests;
- protected merge verified on the base branch;
- the merge SHA deployed through the repository-owned path;
- production smoke and exact-SHA revision attestation pass for that SHA;
- the single living HTML/CSS report says Live and links the evidence.

An explicit, justified non-deployable artifact may use the pack's
`not-applicable` deployment contract. Missing config, credentials, or external
authorization is a blocker, never an implicit waiver.

Internal review is authoritative. Keep `coderabbit=off` unless durable
repository policy explicitly selects optional or required evidence. Off mode
must not request, poll, or wait for CodeRabbit. Freeze each candidate and use at
most two consolidated review/repair cycles before escalating an architectural
blocker.

## Safety and recovery

Never force-push, bypass branch protection, merge with stale checks, resolve a
thread without a pushed fix or evidence, expose secrets in logs/reports, or
infer production success from a local build. Preserve the last known good
deployment on failure. Continue in place after repair so the living report and
workflow root remain the canonical record.
