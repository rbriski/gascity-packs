# Gstack Lite Gas City Pack

This is the canonical lightweight delivery pack for this city. It publishes
focused gstack skills and no delivery formula graph or pack-local worker fleet.

Ordinary delivery is deliberately direct:

1. create one durable bead in the code-owning rig;
2. give one implementation owner an exclusive write lease;
3. run repository-native checks;
4. run one independent review for material changes;
5. allow at most one focused repair owner;
6. publish through the repository's protected path;
7. deploy, smoke-test, and account for wall time and rework.

Explicit research and planning deliverables use a separate lightweight route:
the Sol/high Mayor creates and seeds a persistent, attachable `sol-research`
Sol/max conversation, then returns its exact attach command. The session owns
the discussion until the plan, source artifacts, HTML/CSS report, reports-list
entry, and live-link verification are complete. Raw `gc sling ... --no-formula`
work is reserved for explicitly background research. The city and every current
rig cap the lane at one active session, and the audit forces newly added rigs
to receive the same singleton patch.

Use `gstack-lite` by default. Add `review`, `qa`, `cso`, planning, design,
migration, documentation, or release skills only when the changed surface
justifies that gate. The former `gstack-build` GraphV2 formulas and their
pack-local agents are retained under `../deprecated/gstack-graph/` for
historical audit only; Gas City does not discover or route them.

Import the pack at city scope:

```toml
[imports.gstack]
source = "https://github.com/gastownhall/gascity-packs.git//gstack"
```

Import the standalone shared roles on each rig:

```toml
[rigs.imports.gc]
source = "https://github.com/gastownhall/gascity-packs.git//gascity/roles"
```

Do not import `deprecated/complete-delivery` or route ordinary work through
`build-basic` or any archived gstack formula.
