Run garrytan/gstack office-hours intake for this preflighted delivery.

Before reading source intent or writing, run
`.gc/scripts/checks/delivery-source-artifact-valid.sh --context requirements`.
It prints the exact canonical input/output paths and, on a Ralph retry, the
validated logical control, structured `attempt_log`, and exact
`prior_failure_reason`. Use only those paths and repair that exact failure in
place. A missing or mismatched control, attempt log, artifact root, stage, or
path authority is a fail-closed blocker.

Before inspecting repository HEAD or inferring an outcome from existing code,
resolve `gc.var.source_bead_id` on the workflow root. It is required for a
launcher-created delivery. Read that durable bead or convoy with `gc bd show
<source_bead_id> --json`, including its title, description, acceptance
criteria, and relevant notes. Treat it as the source of truth for the requested
outcome. If it is missing, unreadable, or ambiguous, fail closed and ask for
repair; do not substitute the workflow-root title or a checkout change.

Use the gstack sprint model: Think -> Plan -> Build -> Review -> Test -> Ship
-> Reflect. Current `interaction_mode` is {{interaction_mode}}. In interactive
mode, ask one focused question at a time when demand, status quo, user
specificity, narrowest wedge, observation, or future-fit is missing. In
autonomous mode, write the best requirements artifact from available context
and record assumptions. Never ask questions in headless mode; record
unresolved ambiguity in the artifact.

Write requirements to `{{requirements_path}}` and record that exact path on the
workflow root as `gc.build.requirements_path`. Include goal, demand evidence,
current workaround, target user, narrowest wedge, future-fit, constraints,
acceptance criteria, non-goals, and open questions. Its YAML front matter must
set `schema: gc.build.requirements.v1` and `status: approved`.
In YAML front matter, record `source.id`, `source.title`,
`source.anchor: gc:<source-id>`, and `source.acceptance_criteria_sha256` using
the exact durable values resolved above. The hash is `sha256:` plus the SHA-256
digest of the exact JSON `acceptance_criteria` string returned by
`gc bd show <source-id> --json`, byte-for-byte without trimming or
reformatting; do not copy the raw criteria into that source mapping.
Use safe YAML string serialization for every string-valued source field; never
interpolate raw source values where quotes, newlines, or YAML syntax could
change the artifact structure.
Start the body with the exact unfenced H2 heading `## Source Intent` and place
the resolved source ID and title immediately below it, then trace each
requested description/acceptance point into the requirements. Use
repository state only to validate or scope that intent. Do not copy raw source
notes into an owner-facing report; summarize only non-sensitive constraints
needed to make the plan executable.
Preserve the terminal delivery contract established by preflight: internally
reviewed code, current-head required CI, any explicitly configured optional
external-review evidence, protected merge, exact-SHA deployment, production
verification, and a current living report are part of done. CodeRabbit is off
by default and must not be requested or awaited unless the workflow explicitly
opts in.

Close with `gc.outcome=pass` and the requirements artifact path. The graph's
source-artifact check validates schema `gc.build.requirements.v1` and requires
the exact durable source fields; missing or mismatched grounding is non-pass.
On a repair attempt, read `gc.attempt_log` and repair in place.

Do not invoke provider-native subagents. This Gas City lane is the
office-hours worker for the build.
