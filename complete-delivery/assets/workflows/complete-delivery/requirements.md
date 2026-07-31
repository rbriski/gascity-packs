Run garrytan/gstack office-hours intake for this preflighted delivery.

Use the gstack sprint model: Think -> Plan -> Build -> Review -> Test -> Ship
-> Reflect. Current `interaction_mode` is {{interaction_mode}}. In interactive
mode, ask one focused question at a time when demand, status quo, user
specificity, narrowest wedge, observation, or future-fit is missing. In
autonomous mode, write the best requirements artifact from available context
and record assumptions. Never ask questions in headless mode; record
unresolved ambiguity in the artifact.

Write requirements to the requested requirements path when present. Include
goal, demand evidence, current workaround, target user, narrowest wedge,
future-fit, constraints, acceptance criteria, non-goals, and open questions.
Preserve the terminal delivery contract established by preflight: reviewed
code, current-head CI and CodeRabbit, protected merge, exact-SHA deployment,
production verification, and a current living report are part of done.

Close with `gc.outcome=pass` and the requirements artifact path. The graph's
`build-artifact-valid.sh` check validates schema `gc.build.requirements.v1`.
On a repair attempt, read `gc.attempt_log` and repair in place.

Do not invoke provider-native subagents. This Gas City lane is the
office-hours worker for the build.
