Validate the rig's durable Complete Delivery profile before substantive work.

Run `.gc/scripts/checks/delivery-preflight.sh` with this
claimed bead as `GC_BEAD_ID`. It validates the one-step contract: repository-native gates,
exact required-check policy, optional CodeRabbit posture, merge method, deployment
mode, exact-SHA verification, production smoke, and safe report publication.
On the first worker attempt, the mechanical check itself runs `gh auth status`,
repository resolution, and protected-base access without printing credentials.
Only after all three pass does it persist a versioned, root-bound worker
attestation. The launcher performs the same checks before pouring the workflow
and writes durable `launcher_github_preflight=github-v1` evidence. Ralph retry
conditions run in a deliberately sanitized ConditionEnv, so they must never
read `gh` credentials; they require both launcher and exact root-bound worker
evidence along with all credential-free profile/worktree checks.

Repair durable rig `formula_vars` when configuration is incomplete; do not
weaken a gate for this run or ask the user to re-enter routine settings. If an
external capability such as a GitHub App or deployment credential is truly
missing, record that exact blocker instead of pretending the lifecycle can
finish.

Close with `gc.outcome=pass` only after the mechanical check passes. Do not
invoke provider-native subagents.
