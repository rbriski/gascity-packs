# Gstack Lite Contract

- The pack root exposes skills only: no active `agents/`, `commands/`, or
  `formulas/` directories.
- `gstack-lite` is the default end-to-end delivery skill.
- The persistent Mayor remains Sol/high; explicit research and planning
  deliverables route to a work-item-affine, scale-to-zero Sol/max
  `sol-research` lane through `mol-polecat-report` without moving the user.
- One bead has one implementation owner and one immutable source-head lease.
- Material changes receive one different-family review against an immutable
  candidate head.
- At most one repair owner may write after review. The prior owner must be
  drain-acknowledged or forcibly closed before reassignment.
- Repository-native checks precede model review; a green baseline may be
  inherited only when bound to the same immutable head and check definition.
- Protected publication, deployment, and a behavior canary remain distinct
  terminal states.
- Complete Delivery and the former gstack GraphV2 formula fleet under
  `../deprecated/` are historical artifacts only and must not appear in the
  active city catalog.
- Final accounting records wall clock, provider lanes, queue time, checks,
  review, repair, deployment, rejected attempts, and human intervention.

Run the contract checks from the repository root:

```sh
python3 -m pytest tests/test_gstack_lite_pack_contract.py tests/test_gstack_review_contract.py -q
python3 gstack/skills/gstack-lite/scripts/audit_city.py --city /path/to/city
```
