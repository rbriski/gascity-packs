Start one observable lifecycle from intent through verified production.

Usage:
  gc <binding> delivery start <bead-id> --rig <rig> [flags]

The rig's durable `formula_vars` supply its setup, lint, typecheck, test,
build, browser, security, required-check, optional CodeRabbit, deploy, verification,
smoke, and report-publication contract. You provide the work bead once. Its
durable ID and title become the delivery's source-intent record; later stages
read that bead's title, description, acceptance criteria, and relevant notes
before using repository state as context.

Flags:
  --rig <name>          Owning rig (defaults to `$GC_RIG`).
  --agent <target>      Initial run operator (default: `gc.run-operator`).
  --artifact-root <p>   Artifact root (default: `plans/complete-delivery/<id>`).
  --interactive         Preserve planning/review questions.
  --same-session        Drain implementation items through one worker session.

Example:
  gc complete-delivery delivery start fi-123 --rig finance

The command does not stop at a branch or pull request. Terminal success means
required CI is green, authoritative review findings are resolved, the PR is
merged, the merge SHA is deployed and verified, and the living report is
current. CodeRabbit defaults off and is never requested or awaited in that
mode.
