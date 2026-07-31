# Complete Delivery Gas City Pack

Complete Delivery turns “complete this work” into one observable operation:

```sh
gc complete-delivery delivery start <bead-id> --rig <rig>
```

That run does not stop at generated code or an open pull request. It plans,
implements, tests, reviews, reconciles current-head CI and CodeRabbit, merges
through repository protection, deploys the reviewed merge SHA, verifies that
exact revision in production, and keeps one owner-facing HTML/CSS report
current throughout.

The pack imports [gstack](../gstack) instead of copying it. gstack supplies the
strongest existing planning, multi-perspective review, browser QA, security,
documentation, and release-readiness workflow in this registry. Complete
Delivery adds the terminal half that generic methodology packs intentionally
leave repository-specific: deterministic local commands, live GitHub review,
CodeRabbit, protected merge, deployment, exact-SHA attestation, and reporting.

## What “done” means

A successful run proves all applicable items below on one durable workflow:

1. requirements and a reviewed implementation plan;
2. implementation plus focused and repository-wide local gates;
3. independent code review, QA, security, and release readiness;
4. one non-draft PR whose current head passes the exact required checks;
5. CodeRabbit completion on that head and zero live unresolved threads;
6. zero unresolved human threads or current-head change requests;
7. a protected merge without admin bypass or force push;
8. deployment of the recorded merge SHA through the repository-owned path;
9. production smoke and revision attestation for that SHA; and
10. one published living report whose top-line state is **Live**.

An explicitly non-deployable artifact can use `deploy_mode=not-applicable`
with a durable reason. Missing configuration, authorization, CI, CodeRabbit,
credentials, or production evidence is a blocker—not an implicit exception.

## Install once

Import the workflow and the gstack worker binding at city scope. The
`complete-delivery` manifest already imports gstack's formulas; the explicit
top-level `gstack` binding makes its rig-scoped agent names resolvable too:

```sh
gc import add --name complete-delivery \
  https://github.com/gastownhall/gascity-packs.git//complete-delivery
gc import add --name gstack \
  https://github.com/gastownhall/gascity-packs.git//gstack
```

Import the shared build roles under the exact `gc` binding on each rig that
will run the lifecycle. The city imports above supply
`complete-delivery.*` and `gstack.*`; this rig import supplies
`gc.run-operator`, `gc.publisher`, and the shared build roles:

```toml
[[rigs]]
name = "your-project"

[rigs.imports.gc]
source = "https://github.com/gastownhall/gascity-packs.git//gascity/roles"
```

Run `gc import install` after editing `city.toml`. Contributors can replace
either URL with the corresponding local checkout path.

## Configure each repository once

Put the repository's durable delivery profile in its `[rigs.formula_vars]`.
The launch command then needs only the bead and rig; users do not have to
remember a checklist or retype commands.

```toml
[rigs.formula_vars]
setup_command = "npm ci"
lint_command = "npm run lint"
typecheck_command = "npm run typecheck"
test_command = "npm test"
build_command = "npm run build"
browser_test_command = "npm run test:browser"
security_command = "gitleaks detect --source . --no-banner"
extra_gate_command = "npm run smoke:compose"

required_checks = "verify"
coderabbit = "required"
merge_method = "squash"

deploy_mode = "command"
deploy_command = "./scripts/deploy.sh \"$DELIVERY_SHA\""
deploy_verify_command = "./scripts/deploy-status.sh --expect-sha \"$DELIVERY_SHA\""
smoke_command = "./scripts/smoke-production.sh --expect-sha \"$DELIVERY_SHA\""
production_url = "https://service.example.com"

report_publish_command = "gc complete-delivery report publish --source \"$DELIVERY_REPORT_DIR\" --destination-root /srv/reports/deliveries"
```

The early `delivery-preflight` check validates this profile, Git, `gh`
authentication, and readable protection on `base_branch` before planning
begins. A bad profile fails in minutes with a single list of missing settings
instead of surfacing after implementation. The PR gate evaluates the union of
explicitly named checks and every check required by branch protection.

Shell commands are trusted repository-owner configuration and run from the
workflow worktree via `bash -lc`. Do not put untrusted issue or PR text in a
formula variable.

## Run in one step

Create or select the durable work bead in the rig that owns the code, then:

```sh
gc complete-delivery delivery start fi-123 --rig finance
```

The ergonomic defaults are autonomous planning, agent-applied review fixes,
parallel isolated implementation lanes, branch publication, and PR creation.
Optional flags are deliberately small:

| Flag | Effect |
| --- | --- |
| `--interactive` | Preserve human planning and review checkpoints. |
| `--same-session` | Run implementation items serially in one shared session. |
| `--artifact-root <path>` | Override `plans/complete-delivery/<bead-id>`. |
| `--agent <target>` | Override the initial `gc.run-operator` role. |

Inspect the installed graph or current run with normal Gas City tools:

```sh
gc formula show complete-delivery --rig finance --json
gc run status <workflow-root-id> --json
```

## Lifecycle

```text
preflight -> requirements -> plan/review -> implementation -> internal review
          -> QA -> exact local gates -> release readiness -> publish PR
          -> [CI + CodeRabbit + review fix loop] -> protected merge
          -> deploy merge SHA -> production attestation -> final report
```

The external-review expansion is bounded to twelve attempts. Every iteration
snapshots the PR head, reads full live review threads, applies only valid
findings, adds regression coverage, reruns the same local profile, pushes
normally, and updates the report. A new commit deliberately invalidates old CI
and CodeRabbit evidence. The terminal gate evaluates the head twice and fails
if it moves during evaluation.

`delivery_gate.py` trusts a CodeRabbit signal only when it comes from the
CodeRabbit GitHub App/status identity or a CodeRabbit review tied to the
current head. A green bot signal does not override a live unresolved thread.
Likewise, a stale human change request is not confused with a current-head
request, while every non-outdated unresolved thread remains blocking.

## Deployment profiles

| Mode | Required proof |
| --- | --- |
| `command` | `deploy_command`, `deploy_verify_command`, and a smoke command (unless explicitly waived) run with `DELIVERY_SHA` set to the merge SHA. |
| `ci` | Evidence for the CI deployment plus `deploy_verify_command` and production smoke on the merge SHA. |
| `not-applicable` | A concrete `deploy_not_applicable_reason`; valid only for artifacts that cannot be deployed. |

Real deployments must record nonempty deploy and verification evidence, a
full merge SHA, `delivery.deploy_status=verified`, and a deployed SHA equal to
the merge SHA. The mechanical terminal check reruns verification and smoke
with that SHA. An HTTP 200 or local checkout alone is not revision
attestation.

## Living report

The workflow creates one report bundle under
`<artifact_root>/delivery-report/`:

- `state.json` — durable machine-readable milestone state;
- `index.html` — accessible, responsive, outcome-first owner view; and
- `styles.css` — self-contained presentation with no external assets.

Milestones update after plan approval, implementation, internal review/QA,
PR publication, every external-review pass, merge, deployment, and production
verification. `gc complete-delivery report publish` publishes only the
rendered HTML and CSS, rejects symlinks and unsafe path slugs, and never copies
state JSON.
The report leads with live/blocker status and the next action; technical
evidence remains available in the timeline.

## Variables

| Variable | Default | Contract |
| --- | --- | --- |
| `required_checks` | `auto` | Exact comma-separated names, or protected-branch discovery with current-head fallback. Branch protection is always mandatory. |
| `coderabbit` | `required` | `required`, `optional`, or `off`; required is the intended production profile. |
| `allow_no_ci` | `false` | Explicit exception for a repository with no CI. |
| `setup_command` … `extra_gate_command` | empty | Ordered local profile: setup, lint, typecheck, test, build, browser, security, extra. |
| `allow_no_local_gates` | `false` | Explicit exception when no local command can apply. |
| `merge_method` | `squash` | `squash`, `merge`, or `rebase`; never admin bypass. |
| `deploy_mode` | `command` | `command`, `ci`, or `not-applicable`. |
| `deploy_command` | empty | Repository-owned exact-SHA deployment command. |
| `deploy_verify_command` | empty | Exact-SHA deployment verification command. |
| `smoke_command` | empty | Production behavior/health check. |
| `allow_no_smoke` | `false` | Explicit smoke exception for a real deployment. |
| `report_publish_command` | empty | Optional publication command receiving `DELIVERY_REPORT_DIR`. |
| `production_url` | empty | Optional HTTPS link in the owner report. |

Inherited gstack/build variables such as `artifact_root`, `drain_policy`,
`implementation_target`, and the methodology selectors remain available.

## Test and audit

Run the pack suite from the repository root:

```sh
python3 -m pytest complete-delivery/tests -q
bash -n complete-delivery/commands/delivery/start/run.sh \
  complete-delivery/assets/scripts/checks/*.sh
gc formula show complete-delivery --rig <configured-rig> --json
```

The tests cover pack/agent/command wiring, graph dependencies, bounded review
loops, executable assets, fail-closed preflight behavior, current-head GitHub
and CodeRabbit evaluation, report escaping/rendering, and safe publication.
The compatibility and terminal evidence claims are recorded in
[REQUIREMENTS.md](./REQUIREMENTS.md).
