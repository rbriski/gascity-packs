---
plan_slug: gstack-lite-standardization
phase: implementation-plan
rig: gascity-packs
rig_root: /home/nvidia/gascity/city/rigs/gascity-packs
artifact_root: /home/nvidia/gascity/city/rigs/gascity-packs/gc-plans
requirements_file: /home/nvidia/gascity/city/rigs/gascity-packs/gc-plans/gstack-lite-standardization/requirements.md
status: approved
created_at: 2026-08-04T22:42:41Z
updated_at: 2026-08-04T22:42:41Z
---

# Implementation Plan: Standardize Gstack Lite across Gas City

## Summary

Establish a single concise Gstack Lite policy in the `gascity`/`gstack` source
packs, project it into every agent prompt and skill sink, make current and
future rig imports consistent, remove exact stale Complete Delivery skill
links, reconcile the public reports, and validate the result with deterministic
checks plus read-only routing canaries.

## Current System

- `/home/nvidia/gascity/city/pack.toml` imports gstack city-wide and no longer
  imports Complete Delivery.
- `/home/nvidia/gascity/city/city.toml` defines the pragmatic provider aliases
  and future-rig `gc` import, but existing rig import tables are inconsistent.
- `gascity/formulas/do-work.formula.toml` is the small implementation lifecycle;
  `gstack-work` specializes it. Neither is a complete release train.
- `gascity/skills/mayor/SKILL.md` has no durable Gstack Lite default.
- Provider-specific `.codex/skills` directories retain stale unmanaged
  `complete-delivery.complete-delivery` symlinks even though current `.agents`
  projections no longer own that skill.
- The no-go report records the rollback, while the older pack report and its
  library card still claim Complete Delivery is live.

## Proposed Implementation

### Pack contract

- Add `gstack/skills/gstack-lite/SKILL.md` and generated
  `agents/openai.yaml`. Keep it concise and use it as the default end-to-end
  delivery policy for Gas City work.
- Add `gascity/template-fragments/gstack-lite-policy.template.md` so all agent
  prompts receive a short invariant: lightweight path by default, risk-based
  gates, bounded rework, and no Complete Delivery.
- Update `gascity/skills/mayor/SKILL.md` to select Gstack Lite for normal
  delivery and require explicit user intent before any heavier formula.
- Update `gascity` and `gstack` READMEs/requirements to distinguish the direct
  skill policy from large GraphV2 workflows.
- Deprecate `complete-delivery` in its README, skill metadata/body, root pack
  index, and registry description while preserving immutable releases.
- Add `gstack/skills/gstack-lite/scripts/audit_city.py` with read-only audit and
  an exact-symlink cleanup option. Keeping it inside the skill makes the audit
  available in every projected skill directory. It must never delete caches,
  evidence, or non-symlink files.
- Add focused tests for the skill, policy fragment, deprecation contract, and
  audit script.

### Live city

- Pin the merged gstack/gascity source in `pack.toml`, `city.toml`, and
  `packs.lock` through `gc import install`.
- Append the Gstack Lite fragment globally.
- Add the `gc` roles import explicitly to every current rig and retain it under
  `[defaults.rig.imports]` for future rigs.
- Make the specialized implementation reviewer a global Claude/Opus patch and
  remove redundant per-rig patches.
- Bound ordinary polecat pools to at most two writers per rig; retain smaller
  limits where already stricter. Because the current defaults schema inherits
  imports but not rig pool patches, make the audit reject any newly registered
  rig missing suspension, total-session, or polecat caps.
- Update the local `mol-do-work` text to identify itself as the implementation
  leg of Gstack Lite and require verification/accounting evidence.
- Run the audit cleanup to remove only stale Complete Delivery skill links,
  reinstall imports, and restart only sessions needed to consume the new
  prompt/skill projection.

### Reports

- Expand `pragmatic-city-setup` into the canonical current-state report with an
  explicit Gstack Lite component map and supersession notice.
- Keep `complete-delivery-system-accounting` as the historical decision record
  and cross-link it to the current report.
- Mark `complete-delivery-pack` archived at the top, replace live status copy,
  and point readers to the no-go/current reports.
- Update the report library cards so only the pragmatic setup is presented as
  current and the two Complete Delivery reports are clearly historical.

## Testing

- Validate the new skill with the system skill validator.
- Run focused pack tests for Gstack Lite and existing skill/frontmatter tests.
- Run the complete repository test command used by GitHub CI.
- Run `validate_registry.py`, `gc import install`, `gc doctor --json`, resolved
  config checks, formula catalog/show checks, and the Gstack Lite audit.
- Validate edited HTML and CSS, verify internal links, and render desktop/mobile
  screenshots when the local report tooling is available.
- Dispatch read-only primary, economy, and independent-review canaries; record
  claim, provider/model, elapsed time, outcome, and repository cleanliness.

## Rollout

1. Merge the one protected pack PR and delete its temporary branch.
2. Pin/install the merged source into the city.
3. Apply and validate live city/report changes.
4. Clean exact stale skill links and refresh agent projections.
5. Run canaries without changing product repositories.
6. Close `gp-v1s6` only after current and future configuration, documentation,
   and routing evidence agree.

## Open Questions

None. The user's autonomous instruction approves this plan and the historical
no-go report defines the lightweight policy.
