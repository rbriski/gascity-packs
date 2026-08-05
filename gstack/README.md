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

Use `gstack-lite` by default. Add `review`, `qa`, `cso`, planning, design,
migration, documentation, or release skills only when the changed surface
justifies that gate. The former `gstack-build` GraphV2 formulas and their
pack-local agents are retained under `../deprecated/gstack-graph/` for
historical audit only; Gas City does not discover or route them.

Import the pack at city scope:

```toml
[imports.gstack]
source = "https://github.com/rbriski/gascity-packs.git//gstack"
```

Import the standalone shared roles on each rig:

```toml
[rigs.imports.gc]
source = "https://github.com/rbriski/gascity-packs.git//gascity/roles"
```

Do not import `deprecated/complete-delivery` or route ordinary work through
`build-basic` or any archived gstack formula.
