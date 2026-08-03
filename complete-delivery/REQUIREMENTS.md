# Complete Delivery Compatibility and Evidence Ledger

This ledger defines the public contract of the `complete-delivery` pack and
points to reproducible evidence. The pack is a terminal lifecycle wrapper over
`gstack-build`, not a competing planning methodology.

| ID | Requirement | Implementation evidence | Automated evidence |
| --- | --- | --- | --- |
| CD-001 | Compose with the strongest existing methodology rather than fork it. | `pack.toml` imports `../gstack`; `complete-delivery` extends `gstack-build`. | `test_pack_contract.py::PackContractTests::test_pack_imports_and_formula_identity` |
| CD-002 | One user action launches the full lifecycle with ergonomic autonomous defaults. | `commands/delivery/start/run.sh`; `skills/complete-delivery/SKILL.md`. | `test_pack_contract.py::CommandContractTests` |
| CD-003 | Invalid repository profiles fail before requirements/planning. | `delivery-preflight` depends on `prepare`; the overridden `requirements` and report initialization depend on preflight. | `test_pack_contract.py::FormulaContractTests::test_preflight_blocks_requirements_and_reporting`; `test_preflight.py` |
| CD-004 | Repository-native gates execute mechanically in a fixed order and fail closed when absent. | `delivery-local-gates.sh`; local-gates graph check. | `test_pack_contract.py::FormulaContractTests::test_mechanical_checks_are_wired` |
| CD-005 | Required CI is evaluated only on the PR's current head and the gate detects a moving head. | `delivery_gate.py` fetches current-head checks/statuses and refreshes the PR before returning. | `test_delivery_gate.py` current-head, missing-check, inference, and moving-head cases. |
| CD-006 | Internal review is authoritative; CodeRabbit is explicit optional evidence and off performs no request, poll, or wait. | `coderabbit=off`; typed disabled evaluator state; trusted App/status/review matching only when enabled. | `test_delivery_gate.py` off-mode, trusted-signal, and unresolved-thread cases; `test_pack_contract.py` defaults. |
| CD-007 | CI and review failures route through at most two frozen-candidate fix/test/push/report cycles. | `complete-delivery-pr-gate` expansion; maximum 2 attempts; one consolidated repair per cycle; ordered children. | `test_pack_contract.py::FormulaContractTests::test_external_review_loop_is_bounded_and_ordered` |
| CD-008 | Merge uses the protected PR path and recorded merge SHA, with no admin bypass. | preflight protection probe, fail-closed PR gate, merge prompt, and `delivery-merged.sh`. | Live `GhClient`, unprotected-branch, preflight, and prompt/command tests. |
| CD-009 | Real deployment proves the reviewed merge SHA in production. | deploy/verify prompts; `delivery-release-verified.sh`; preflight requires deploy and verify commands for command mode. | SHA/config assertions in `test_pack_contract.py`; pack fixture/E2E run. |
| CD-010 | One source-bound HTML/CSS report updates throughout and reaches Live only after every stage, exact-SHA/deploy proof, any no-smoke reason, and exact rendering validate. | report milestone steps; `delivery_report.py`; `delivery-report-valid.sh`; safe publisher. | `test_delivery_report.py`; report security regressions in `test_pack_contract.py`; `test_publish_delivery_report.py`. |
| CD-011 | Unsafe shortcuts remain prohibited. | role and stage prompts prohibit force push, admin bypass, invented proof, unreviewed roll-forward, and provider-native subagents. | text and route assertions in `test_pack_contract.py`. |
| CD-012 | The pack installs through Pack V2 conventions and its skill is discoverable. | `pack.toml`, conventional agents/commands/formulas/skills/assets, documented top-level `gstack` and rig-level `gc` bindings. | pack structure tests, skill quick validation, `gc formula show`, resolved agent-catalog check. |

## Reproduce the evidence

From the `gascity-packs` repository root:

```sh
python3 -m pytest complete-delivery/tests -q
python3 -m py_compile complete-delivery/assets/scripts/*.py
bash -n complete-delivery/commands/delivery/start/run.sh \
  complete-delivery/assets/scripts/checks/*.sh
python3 /home/nvidia/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  complete-delivery/skills/complete-delivery
gc formula show complete-delivery --rig finance --json
```

For a deterministic external-gate smoke test without GitHub mutation:

```sh
python3 complete-delivery/assets/scripts/delivery_gate.py \
  --repo owner/repo --pr 1 --fixture complete-delivery/tests/fixtures/green-pr.json
```

The final acceptance proof is a safe end-to-end run on a configured rig: its
workflow root, PR, merge SHA, deployed SHA, production response, and published
report must agree. A fixture run proves mechanics but does not substitute for
that production proof.
