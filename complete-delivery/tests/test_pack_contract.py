from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import tempfile
import tomllib
import types
import unittest
from unittest import mock


PACK_ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO_ROOT = PACK_ROOT.parent
FORMULA_DIR = PACK_ROOT / "formulas"
REPORT_SCRIPT = PACK_ROOT / "assets" / "scripts" / "delivery_report.py"
REPORT_CHECK = PACK_ROOT / "assets" / "scripts" / "checks" / "delivery-report-valid.sh"
REPORT_SPEC = importlib.util.spec_from_file_location("pack_contract_delivery_report", REPORT_SCRIPT)
assert REPORT_SPEC and REPORT_SPEC.loader
delivery_report = importlib.util.module_from_spec(REPORT_SPEC)
REPORT_SPEC.loader.exec_module(delivery_report)
PREPARE_SCRIPT = PACK_ROOT / "assets" / "scripts" / "prepare_delivery_launch.py"
PREPARE_SPEC = importlib.util.spec_from_file_location("pack_contract_prepare_delivery", PREPARE_SCRIPT)
assert PREPARE_SPEC and PREPARE_SPEC.loader
prepare_delivery = importlib.util.module_from_spec(PREPARE_SPEC)
PREPARE_SPEC.loader.exec_module(prepare_delivery)


def load_toml(path: pathlib.Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def formula_nodes(formula: dict):
    for node in formula.get("steps", []):
        yield node
    for node in formula.get("template", []):
        yield node
        yield from node.get("children", [])


def execute_release_failure_path(delivery: dict, gate: dict, failed_control: str) -> dict:
    """Execute the fail-stop contract encoded by the release Formula.

    This deliberately models graph.v2 terminal state, rather than treating a
    closed dependency as a pass.  It uses the Formula's actual scopes and
    member metadata so a topology change that detaches the external-review
    target from its body fails these regression tests.
    """
    steps = {step["id"]: step for step in delivery["steps"]}
    templates = {template["id"]: template for template in gate["template"]}
    release_scope = steps["release-body"]
    release_members = release_scope["needs"]
    external_scope = templates["{target}"]
    external_members = [member.replace("{target}", "external-review") for member in external_scope["needs"]]
    state = {member: {"status": "pending"} for member in release_members}
    state.update({member: {"status": "pending"} for member in external_members})
    state["release-body"] = {"status": "pending"}
    audit = []

    def abort(scope: str, members: list[str], failed_member: str) -> None:
        state[failed_member] = {"status": "closed", "gc.outcome": "fail"}
        audit.append({"scope": scope, "member": failed_member, "gc.outcome": "fail"})
        state[scope] = {"status": "closed", "gc.outcome": "fail"}
        for member in members:
            if state[member]["status"] == "pending":
                state[member] = {"status": "skipped"}

    if failed_control.startswith("external-review."):
        template_id = "{target}." + failed_control.removeprefix("external-review.")
        control = templates[template_id]
        assert control["metadata"]["gc.scope_ref"] == "{target}"
        assert control["metadata"]["gc.on_fail"] == "abort_scope"
        abort("external-review", external_members, failed_control)
        # Expansion replaces the source step metadata, so this separate,
        # compiler-visible outer member is the semantic bridge. Its check
        # rejects the failed nested scope before report-green can be released.
        bridge = steps["external-review-result"]
        assert bridge["needs"] == ["external-review"]
        assert bridge["metadata"]["gc.scope_ref"] == "release-body"
        assert bridge["metadata"]["gc.on_fail"] == "abort_scope"
        abort("release-body", release_members, "external-review-result")
    else:
        control = steps[failed_control]
        assert control["metadata"]["gc.scope_ref"] == "release-body"
        assert control["metadata"]["gc.on_fail"] == "abort_scope"
        abort("release-body", release_members, failed_control)
    return {"state": state, "audit": audit}


class PackContractTests(unittest.TestCase):
    def test_pack_imports_and_formula_identity(self) -> None:
        pack = load_toml(PACK_ROOT / "pack.toml")
        formula = load_toml(FORMULA_DIR / "complete-delivery.formula.toml")
        self.assertEqual(pack["pack"]["name"], "complete-delivery")
        self.assertEqual(pack["pack"]["schema"], 2)
        self.assertEqual(pack["imports"]["gstack"]["source"], "../gstack")
        self.assertEqual(formula["formula"], "complete-delivery")
        self.assertEqual(formula["extends"], ["gstack-build"])
        self.assertEqual(formula["requires"]["formula_compiler"], ">=2.0.0")
        self.assertTrue(formula["target_required"])

    def test_agent_namespaces_and_shared_worker_binding_are_documented(self) -> None:
        readme = (PACK_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("gc import add --name gstack", readme)
        self.assertIn("[rigs.imports.gc]", readme)
        self.assertIn("gascity/roles", readme)
        for role in ("delivery-engineer", "external-review-resolver", "report-editor"):
            data = load_toml(PACK_ROOT / "agents" / role / "agent.toml")
            self.assertEqual(data["scope"], "rig")
            self.assertTrue(data["fallback"])
            prompt = (PACK_ROOT / "agents" / role / "prompt.template.md").read_text(
                encoding="utf-8"
            )
            self.assertIn('{{ template "gc-role-worker" . }}', prompt)
            self.assertIn("Do not invoke", prompt)
            self.assertIn("provider-native subagents", prompt)

    def test_all_local_description_assets_exist(self) -> None:
        for path in sorted(FORMULA_DIR.glob("*.formula.toml")):
            formula = load_toml(path)
            for node in formula_nodes(formula):
                description = node.get("description_file")
                with self.subTest(formula=path.name, node=node.get("id")):
                    self.assertIsInstance(description, str)
                    self.assertTrue((path.parent / description).resolve().is_file())

    def test_scripts_are_executable_and_compile_or_parse(self) -> None:
        scripts = [
            PACK_ROOT / "commands" / "delivery" / "start" / "run.sh",
            PACK_ROOT / "commands" / "report" / "publish" / "run.sh",
            *sorted((PACK_ROOT / "assets" / "scripts").glob("*.py")),
            *sorted((PACK_ROOT / "assets" / "scripts" / "checks").glob("*.sh")),
        ]
        self.assertTrue(scripts)
        for script in scripts:
            with self.subTest(script=script.relative_to(PACK_ROOT)):
                self.assertTrue(script.stat().st_mode & stat.S_IXUSR, "script is not executable")
                if script.suffix == ".py":
                    result = subprocess.run(
                        ["python3", "-m", "py_compile", str(script)],
                        capture_output=True,
                        text=True,
                    )
                else:
                    shell = "bash" if script.parent.name == "checks" else "sh"
                    result = subprocess.run(
                        [shell, "-n", str(script)], capture_output=True, text=True
                    )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_installed_validator_uses_schemas_beside_the_managed_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            validator = root / ".gc/scripts/validate_build_artifact.py"
            schema = root / ".gc/schemas/build/requirements.v1.yaml"
            artifact = root / "requirements.md"
            validator.parent.mkdir(parents=True)
            schema.parent.mkdir(parents=True)
            shutil.copy2(REPO_ROOT / "gascity/assets/scripts/validate_build_artifact.py", validator)
            shutil.copy2(REPO_ROOT / "gascity/schemas/build/requirements.v1.yaml", schema)
            artifact.write_text(
                "---\n"
                "schema: gc.build.requirements.v1\n"
                "workflow:\n  id: workflow-1\n  formula: complete-delivery\n"
                "methodology:\n  pack: complete-delivery\n  name: complete-delivery\n"
                "producer:\n  formula: complete-delivery\n  stage: requirements\n  attempt: 1\n"
                "status: approved\n"
                "trace:\n  upstream: []\n  coverage: []\n"
                "---\n\n"
                + "\n\n".join(
                    f"## {section}\n\ncontent"
                    for section in (
                        "Problem Statement", "W6H", "User Stories", "Technical Stories",
                        "Behavior Requirements", "Example Mapping", "Acceptance Criteria",
                        "Out Of Scope", "Open Questions",
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                ["python3", str(validator), "--schema", "gc.build.requirements.v1", "--path", str(artifact)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_docs_define_terminal_not_pr_only_contract(self) -> None:
        readme = (PACK_ROOT / "README.md").read_text(encoding="utf-8")
        skill = (PACK_ROOT / "skills" / "complete-delivery" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        ledger = (PACK_ROOT / "REQUIREMENTS.md").read_text(encoding="utf-8")
        for token in ("CodeRabbit", "protected merge", "exact-SHA", "living report"):
            self.assertIn(token, readme)
            self.assertIn(token, skill)
        for requirement in range(1, 13):
            self.assertIn(f"CD-{requirement:03d}", ledger)


class FormulaContractBaseTests:
    @classmethod
    def setUpClass(cls) -> None:
        cls.delivery = load_toml(FORMULA_DIR / "complete-delivery.formula.toml")
        cls.gate = load_toml(FORMULA_DIR / "complete-delivery-pr-gate.formula.toml")
        cls.steps = {step["id"]: step for step in cls.delivery["steps"]}

    def test_formulas_require_supported_compiler(self) -> None:
        for formula in (self.delivery, self.gate):
            with self.subTest(formula=formula["formula"]):
                self.assertEqual(formula["requires"]["formula_compiler"], ">=2.0.0")
        self.assertEqual(self.delivery["contract"], "graph.v2")
        self.assertEqual(self.gate["contract"], "graph.v2")

    def test_preflight_blocks_requirements_and_reporting(self) -> None:
        self.assertEqual(self.steps["delivery-preflight"]["needs"], ["prepare"])
        self.assertEqual(self.steps["requirements"]["needs"], ["delivery-preflight"])
        self.assertEqual(self.steps["report-initialize"]["needs"], ["delivery-preflight"])
        self.assertEqual(
            self.steps["delivery-preflight"]["check"]["check"]["path"],
            ".gc/scripts/checks/delivery-preflight.sh",
        )

    def test_terminal_stage_chain_is_explicit(self) -> None:
        expected_needs = {
            "publish": ["finalize"],
            "report-pull-request": ["publish"],
            "external-review": ["report-pull-request"],
            "external-review-result": ["external-review"],
            "report-green": ["external-review-result"],
            "merge": ["report-green"],
            "report-merged": ["merge"],
            "deploy": ["report-merged"],
            "verify-production": ["deploy"],
            "report-complete": ["verify-production"],
        }
        for step, needs in expected_needs.items():
            with self.subTest(step=step):
                self.assertEqual(self.steps[step]["needs"], needs)


class FormulaContractTests(FormulaContractBaseTests, unittest.TestCase):
    SCRIPT = PACK_ROOT / "assets" / "scripts" / "checks" / "delivery-external-review-passed.sh"

    def run_check(self, outcome: str = "pass", *, mode: str = "normal") -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            fake_gc = root / "gc"
            fake_gc.write_text(
                """#!/bin/sh
if [ "$1" = "bd" ] && [ "$2" = "show" ]; then
  if [ "$3" = "gate" ]; then printf '%s\\n' "$FAKE_STEP_JSON"; else printf '%s\\n' "$FAKE_ROOT_JSON"; fi
elif [ "$1" = "bd" ] && [ "$2" = "list" ]; then
  printf '%s\\n' "$@" > "$FAKE_GC_ARGS"
  if [ "$FAKE_LIST_MODE" = "oversized" ]; then
    python3 - <<'PY'
import json
print(json.dumps([{
    "id": "external-scope",
    "status": "closed",
    "description": "x" * (3 * 1024 * 1024),
    "metadata": {
        "gc.root_bead_id": "root",
        "gc.step_id": "external-review",
        "gc.kind": "scope",
        "gc.outcome": "pass",
    },
}]))
PY
  else
    printf '%s\\n' "$FAKE_LIST_JSON"
  fi
else
  exit 64
fi
""",
                encoding="utf-8",
            )
            fake_gc.chmod(0o755)
            environment = os.environ.copy()
            environment.update({
                "GC_BEAD_ID": "gate",
                "FAKE_STEP_JSON": json.dumps([{
                    "id": "gate",
                    "metadata": {"gc.root_bead_id": "root"},
                }]),
                "FAKE_ROOT_JSON": json.dumps([{"id": "root", "metadata": {}}]),
                "FAKE_LIST_MODE": mode,
                "FAKE_GC_ARGS": str(root / "gc-args"),
                "PATH": f"{root}:{environment['PATH']}",
            })
            candidate = {
                "id": "external-scope",
                "status": "closed",
                "metadata": {
                    "gc.root_bead_id": "root",
                    "gc.step_id": "external-review",
                    "gc.kind": "scope",
                    "gc.outcome": outcome,
                },
            }
            if mode == "missing":
                candidates = []
            elif mode == "duplicate":
                candidates = [candidate, {**candidate, "id": "external-scope-2"}]
            else:
                if mode == "nonterminal":
                    candidate["status"] = "in_progress"
                elif mode == "wrong-kind":
                    candidate["metadata"]["gc.kind"] = "workflow"
                elif mode == "wrong-step":
                    candidate["metadata"]["gc.step_id"] = "report-green"
                elif mode == "wrong-root":
                    candidate["metadata"]["gc.root_bead_id"] = "other-root"
                candidates = [candidate]
            environment["FAKE_LIST_JSON"] = json.dumps(candidates)
            result = subprocess.run(
                ["bash", str(self.SCRIPT)], capture_output=True, text=True, env=environment
            )
            result.gc_args = (root / "gc-args").read_text(encoding="utf-8")
            return result

    def test_requires_the_nested_scope_to_explicitly_pass(self) -> None:
        self.assertEqual(self.run_check("pass").returncode, 0)
        failed = self.run_check("fail")
        self.assertEqual(failed.returncode, 1)
        self.assertIn("external-review scope did not pass", failed.stderr)

    def test_external_review_scope_query_is_exact_and_argv_safe(self) -> None:
        passed = self.run_check("pass")
        self.assertEqual(passed.returncode, 0, passed.stderr)
        self.assertIn("gc.root_bead_id=root", passed.gc_args)
        self.assertIn("gc.step_id=external-review", passed.gc_args)
        self.assertIn("gc.kind=scope", passed.gc_args)

        # This payload is larger than a typical ARG_MAX. It must be accepted
        # through stdin rather than forwarded as one Python argv element.
        oversized = self.run_check(mode="oversized")
        self.assertEqual(oversized.returncode, 0, oversized.stderr)

    def test_requires_one_terminal_matching_external_review_scope(self) -> None:
        for mode in ("missing", "duplicate", "nonterminal", "wrong-kind", "wrong-step", "wrong-root"):
            with self.subTest(mode=mode):
                result = self.run_check(mode=mode)
                self.assertEqual(result.returncode, 1)

    def test_release_controls_abort_the_semantic_release_scope(self) -> None:
        """A failed closed control must quarantine, never unlock, release work."""
        self.assertEqual(self.delivery["contract"], "graph.v2")

        release_steps = [
            "finalize",
            "publish",
            "report-pull-request",
            "external-review-result",
            "report-green",
            "merge",
            "report-merged",
            "deploy",
            "verify-production",
            "report-complete",
        ]
        scope = self.steps["release-body"]
        self.assertEqual(scope["needs"], release_steps)
        self.assertEqual(scope["metadata"], {
            "gc.kind": "scope",
            "gc.scope_name": "complete-delivery-release",
            "gc.scope_role": "body",
        })

        # These cover the real failure modes that formerly advanced on
        # status=closed alone: finalizer, publish Ralph, external-review setup
        # or loop (reported through its expansion parent), and report-green.
        # Graph v2's abort_scope turns each non-pass into an auditable failed
        # scope and skips every later release action, including merge/deploy.
        failure_paths = {
            "finalize": ["publish", "merge", "deploy"],
            "publish": ["report-pull-request", "merge", "deploy"],
            "external-review-result": ["report-green", "merge", "deploy"],
            "report-green": ["merge", "deploy"],
        }
        for failed_step, prohibited_after_failure in failure_paths.items():
            with self.subTest(failed_step=failed_step):
                metadata = self.steps[failed_step]["metadata"]
                self.assertEqual(metadata["gc.scope_ref"], "release-body")
                self.assertEqual(metadata["gc.scope_role"], "member")
                self.assertEqual(metadata["gc.on_fail"], "abort_scope")
                failed_index = release_steps.index(failed_step)
                for prohibited_step in prohibited_after_failure:
                    self.assertGreater(release_steps.index(prohibited_step), failed_index)

        templates = {node["id"]: node for node in self.gate["template"]}
        # The expansion target itself must be the semantic scope that the
        # outer external-review member consumes; a sibling latch is bypassable.
        external_scope = templates["{target}"]
        self.assertEqual(external_scope["needs"], [
            "{target}.setup-external-review",
            "{target}.external-review-loop",
            "{target}.finalize-external-review",
        ])
        self.assertEqual(external_scope["metadata"], {
            "gc.kind": "scope",
            "gc.scope_name": "complete-delivery-external-review",
            "gc.scope_role": "body",
        })
        for external_control in (
            "{target}.setup-external-review",
            "{target}.external-review-loop",
            "{target}.finalize-external-review",
        ):
            metadata = templates[external_control]["metadata"]
            self.assertEqual(metadata["gc.scope_ref"], "{target}")
            self.assertEqual(metadata["gc.scope_role"], "member")
            self.assertEqual(metadata["gc.on_fail"], "abort_scope")
        self.assertEqual(
            templates["{target}.setup-external-review"]["check"]["max_attempts"],
            1,
        )
        self.assertEqual(
            templates["{target}.external-review-loop"]["check"]["max_attempts"],
            2,
        )
        # Expansion targets replace the parent metadata when compiled. The
        # outer bridge is therefore a normal release-scope member, with a
        # mechanical check that reads the target scope's explicit outcome.
        bridge = self.steps["external-review-result"]
        self.assertEqual(bridge["needs"], ["external-review"])
        self.assertEqual(bridge["metadata"], {
            "gc.run_target": "complete-delivery.external-review-resolver",
            "gc.scope_ref": "release-body",
            "gc.scope_role": "member",
            "gc.on_fail": "abort_scope",
        })
        self.assertEqual(bridge["check"]["max_attempts"], 1)
        self.assertEqual(
            bridge["check"]["check"]["path"],
            ".gc/scripts/checks/delivery-external-review-passed.sh",
        )
        self.assertEqual(self.steps["report-green"]["needs"], ["external-review-result"])

    def test_release_failure_paths_execute_as_audited_quarantines(self) -> None:
        """Every terminal failure skips merge/deploy without rewriting evidence."""
        cases = (
            "finalize",
            "publish",
            "external-review.setup-external-review",
            "external-review.external-review-loop",
            "external-review.finalize-external-review",
            "report-green",
        )
        for failed_control in cases:
            with self.subTest(failed_control=failed_control):
                result = execute_release_failure_path(self.delivery, self.gate, failed_control)
                state = result["state"]
                audit = result["audit"]
                self.assertEqual(state["release-body"], {"status": "closed", "gc.outcome": "fail"})
                self.assertEqual(state["merge"], {"status": "skipped"})
                self.assertEqual(state["deploy"], {"status": "skipped"})
                self.assertNotIn({"scope": "release-body", "member": "merge", "gc.outcome": "fail"}, audit)
                self.assertTrue(any(item["gc.outcome"] == "fail" for item in audit))

                if failed_control.startswith("external-review."):
                    self.assertEqual(state["external-review"], {"status": "closed", "gc.outcome": "fail"})
                    self.assertEqual(
                        state["external-review-result"],
                        {"status": "closed", "gc.outcome": "fail"},
                    )
                    self.assertIn(
                        {"scope": "external-review", "member": failed_control, "gc.outcome": "fail"},
                        audit,
                    )
                else:
                    self.assertEqual(
                        state[failed_control],
                        {"status": "closed", "gc.outcome": "fail"},
                    )

    def test_external_review_loop_is_bounded_and_ordered(self) -> None:
        self.assertEqual(self.steps["external-review"]["expand"], "complete-delivery-pr-gate")
        templates = {node["id"]: node for node in self.gate["template"]}
        loop = templates["{target}.external-review-loop"]
        self.assertEqual(loop["check"]["max_attempts"], 2)
        self.assertEqual(
            loop["check"]["check"]["path"],
            ".gc/scripts/checks/delivery-pr-approved.sh",
        )
        children = {node["id"]: node for node in loop["children"]}
        ordered = [
            "{target}.inspect-current-head",
            "{target}.resolve-findings",
            "{target}.rerun-local-gates",
            "{target}.publish-fixes",
            "{target}.report-external-review",
        ]
        self.assertEqual(list(children), ordered)
        for previous, current in zip(ordered, ordered[1:]):
            self.assertEqual(children[current]["needs"], [previous])

    def test_mechanical_checks_are_wired(self) -> None:
        expected = {
            "delivery-preflight": "delivery-preflight.sh",
            "local-gates": "delivery-local-gates.sh",
            "publish": "delivery-pr-open.sh",
            "merge": "delivery-merged.sh",
            "verify-production": "delivery-release-verified.sh",
            "report-complete": "delivery-report-valid.sh",
        }
        for step, basename in expected.items():
            with self.subTest(step=step):
                self.assertEqual(
                    pathlib.Path(self.steps[step]["check"]["check"]["path"]).name,
                    basename,
                )

    def test_fail_closed_defaults_and_required_profile_vars(self) -> None:
        variables = self.delivery["vars"]
        expected = {
            "push": "true",
            "open_pr": "true",
            "required_checks": "auto",
            "coderabbit": "off",
            "allow_no_ci": "false",
            "allow_no_local_gates": "false",
            "deploy_mode": "command",
            "allow_no_smoke": "false",
        }
        for name, default in expected.items():
            with self.subTest(variable=name):
                self.assertEqual(variables[name]["default"], default)
        for name in (
            "setup_command",
            "lint_command",
            "typecheck_command",
            "test_command",
            "build_command",
            "browser_test_command",
            "security_command",
            "extra_gate_command",
            "deploy_command",
            "deploy_verify_command",
            "smoke_command",
            "report_publish_command",
            "source_bead_id",
            "source_title",
        ):
            self.assertIn(name, variables)

    def test_source_intent_is_preserved_across_the_delivery_stages(self) -> None:
        assets = {
            "requirements": "requirements.md",
            "plan": "plan.md",
            "decompose": "decompose.md",
            "finalize": "finalize.md",
            "report": "report-initialize.md",
        }
        for name, filename in assets.items():
            text = (
                PACK_ROOT / "assets" / "workflows" / "complete-delivery" / filename
            ).read_text(encoding="utf-8")
            with self.subTest(asset=name):
                self.assertIn("gc.var.source_bead_id", text)
        report = (
            PACK_ROOT / "assets" / "workflows" / "complete-delivery" / "report-initialize.md"
        ).read_text(encoding="utf-8")
        finalize = (
            PACK_ROOT / "assets" / "workflows" / "complete-delivery" / "finalize.md"
        ).read_text(encoding="utf-8")
        self.assertIn("source notes are never\npublic-report content", report)
        self.assertIn("Source trace", finalize)

    def test_pack_prompts_do_not_dispatch_provider_native_subagents(self) -> None:
        text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((PACK_ROOT / "assets" / "workflows").rglob("*.md"))
        )
        self.assertNotIn("Task tool (general-purpose):", text)
        self.assertNotIn("Dispatch implementer subagent", text)
        self.assertIn("Do not invoke provider-native subagents", text)

    def test_merge_and_release_assets_prohibit_bypass_and_require_sha(self) -> None:
        merge = (PACK_ROOT / "assets" / "workflows" / "complete-delivery" / "merge.md").read_text(
            encoding="utf-8"
        )
        release = (
            PACK_ROOT / "assets" / "scripts" / "checks" / "delivery-release-verified.sh"
        ).read_text(encoding="utf-8")
        self.assertRegex(merge, r"Never use\s+`--admin`")
        self.assertIn("a force push", merge)
        self.assertIn("^[0-9a-f]{40}$", release)
        self.assertIn('DEPLOY_MODE" = "not-applicable', release)

    def test_formula_runtime_assets_never_depend_on_pack_root_interpolation(self) -> None:
        prompts = sorted((PACK_ROOT / "assets" / "workflows").rglob("*.md"))
        for path in prompts:
            for line in path.read_text(encoding="utf-8").splitlines():
                if "{{pack_root}}" in line:
                    self.assertIn("never invoke", line, str(path.relative_to(PACK_ROOT)))
        for relative in (
            ".gc/scripts/delivery_gate.py",
            ".gc/scripts/delivery_report.py",
            ".gc/scripts/checks/delivery-local-gates.sh",
            ".gc/scripts/checks/delivery-external-review-deadline.sh",
        ):
            self.assertTrue(
                any(relative in path.read_text(encoding="utf-8") for path in prompts),
                relative,
            )


class CommandContractTests(unittest.TestCase):
    SCRIPT = PACK_ROOT / "commands" / "delivery" / "start" / "run.sh"

    def run_command(
        self,
        *arguments: str,
        source_json: str | None = None,
        profile: dict | None = None,
        setup=None,
        environment_updates: dict[str, str] | None = None,
        config_payload: dict | None = None,
        controller_pack_root: pathlib.Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            city = root / "city"
            city.mkdir()
            credential_config = root / "launcher-gh-config"
            credential_config.mkdir()
            (credential_config / "hosts.yml").write_text(
                "opaque credential fixture\n", encoding="utf-8"
            )
            rig = root / "rig"
            rig.mkdir()
            subprocess.run(["git", "init", "-q", str(rig)], check=True)
            subprocess.run(
                ["git", "remote", "add", "origin", "https://github.com/example/repo.git"],
                cwd=rig,
                check=True,
            )
            subprocess.run(
                ["git", "remote", "add", "upstream", "https://github.com/upstream/repo.git"],
                cwd=rig,
                check=True,
            )
            fake_gc = pathlib.Path(directory) / "gc"
            fake_gh = pathlib.Path(directory) / "gh"
            config = {
                "city_path": str(city),
                "config": {
                    "Rigs": [{"Name": "finance", "Path": str(rig), "FormulaVars": profile or {
                        "setup_command": "/bin/true",
                        "deploy_mode": "not-applicable",
                        "deploy_not_applicable_reason": "fixture",
                    }}]
                }
            }
            if config_payload is not None:
                config = config_payload
            controller_pack_root = controller_pack_root or PACK_ROOT
            controller_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=controller_pack_root,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            controller_import_status = {
                "imports": [
                    {
                        "name": "pack:complete-delivery",
                        "pin": {
                            "commit": controller_commit,
                            "version": f"sha:{controller_commit}",
                        },
                    }
                ]
            }
            controller_formula = {
                "search_paths": [str(controller_pack_root / "formulas")],
            }
            sink = root / "gc-args"
            calls = root / "gc-calls"
            fake_gc.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$*\" >> \"$FAKE_GC_CALLS\"\n"
                "case \"${1:-}:${2:-}\" in import:*|formula:show)\n"
                "  printf 'HOME=%s|GC_HOME=%s|GC_PACK_DIR=%s\\n' \"${HOME:-}\" \"${GC_HOME:-}\" \"${GC_PACK_DIR:-}\" >> \"$FAKE_CONTROLLER_ENVS\" ;;\n"
                "esac\n"
                "if [ \"${1:-}\" = config ] && [ \"${2:-}\" = show ]; then\n"
                "  [ \"${FAKE_GC_SLEEP_STAGE:-}\" != config ] || exec sleep 5\n"
                "  [ \"${FAKE_GC_FAIL_STAGE:-}\" != config ] || { printf 'config failed\\n' >&2; exit 23; }\n"
                "  printf '%s\\n' \"$FAKE_GC_CONFIG\"\n"
                "  exit 0\n"
                "fi\n"
                "if [ \"${1:-}\" = bd ] && [ \"${2:-}\" = show ]; then\n"
                "  [ \"${FAKE_GC_SLEEP_STAGE:-}\" != source ] || exec sleep 5\n"
                "  [ \"${FAKE_GC_FAIL_STAGE:-}\" != source ] || { printf 'source failed\\n' >&2; exit 24; }\n"
                "  printf '%s\\n' \"$FAKE_SOURCE_JSON\"\n"
                "  exit 0\n"
                "fi\n"
                "if [ \"${1:-}\" = import ] && [ \"${2:-}\" = install ]; then\n"
                "  [ \"${FAKE_GC_SLEEP_STAGE:-}\" != import-install ] || exec sleep 5\n"
                "  [ \"${FAKE_GC_FAIL_STAGE:-}\" != import-install ] || exit 26\n"
                "  exit 0\n"
                "fi\n"
                "if [ \"${1:-}\" = import ] && [ \"${2:-}\" = status ]; then\n"
                "  [ \"${FAKE_GC_SLEEP_STAGE:-}\" != import-status ] || exec sleep 5\n"
                "  [ \"${FAKE_GC_FAIL_STAGE:-}\" != import-status ] || exit 27\n"
                "  printf '%s\\n' \"$FAKE_CONTROLLER_IMPORT_STATUS\"\n"
                "  exit 0\n"
                "fi\n"
                "if [ \"${1:-}\" = formula ] && [ \"${2:-}\" = show ]; then\n"
                "  [ \"${FAKE_GC_SLEEP_STAGE:-}\" != formula-show ] || exec sleep 5\n"
                "  [ \"${FAKE_GC_FAIL_STAGE:-}\" != formula-show ] || exit 28\n"
                "  printf '%s\\n' \"$FAKE_CONTROLLER_FORMULA\"\n"
                "  exit 0\n"
                "fi\n"
                "[ \"${FAKE_GC_SLEEP_STAGE:-}\" != sling ] || exec sleep 5\n"
                "[ \"${FAKE_GC_FAIL_STAGE:-}\" != sling ] || { printf 'sling failed\\n' >&2; exit 25; }\n"
                "printf '%s\\n' \"$@\" | tee -a \"$FAKE_GC_SINK\"\n",
                encoding="utf-8",
            )
            fake_gc.chmod(0o755)
            fake_gh.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$*\" >> \"$FAKE_GH_CALLS\"\n"
                "printf 'HOME=%s|GH_CONFIG_DIR=%s|XDG_CONFIG_HOME=%s\\n' \"${HOME:-}\" \"${GH_CONFIG_DIR:-}\" \"${XDG_CONFIG_HOME:-}\" >> \"$FAKE_GH_ENVS\"\n"
                "if [ -n \"${GH_CONFIG_DIR:-}\" ]; then gh_config=$GH_CONFIG_DIR;\n"
                "elif [ -n \"${XDG_CONFIG_HOME:-}\" ]; then gh_config=$XDG_CONFIG_HOME/gh;\n"
                "else gh_config=${HOME:-}/.config/gh; fi\n"
                "test -f \"$gh_config/hosts.yml\" || exit 70\n"
                "if [ \"${FAKE_GH_AUTHENTICATED:-true}\" != true ]; then printf '%s\\n' \"${FAKE_GH_FAILURE_DIAGNOSTIC:-authentication failed}\" >&2; exit 1; fi\n"
                "if [ \"${1:-}\" = repo ] && [ \"${2:-}\" = view ]; then\n"
                "  printf '%s\\n' '{\"nameWithOwner\":\"example/repo\"}'\n"
                "fi\n"
                "if [ \"${1:-}\" = api ] && [ \"${FAKE_GH_PROTECTED:-true}\" != true ]; then exit 1; fi\n",
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)
            environment = os.environ.copy()
            environment["GC_PACK_DIR"] = str(PACK_ROOT)
            environment["PATH"] = f"{directory}:{environment['PATH']}"
            environment["FAKE_GC_CONFIG"] = json.dumps(config)
            environment["FAKE_CONTROLLER_IMPORT_STATUS"] = json.dumps(controller_import_status)
            environment["FAKE_CONTROLLER_FORMULA"] = json.dumps(controller_formula)
            environment["FAKE_SOURCE_JSON"] = (
                source_json
                if source_json is not None
                else json.dumps([{"id": arguments[0], "title": "Requested delivery"}])
            )
            environment["FAKE_GC_SINK"] = str(sink)
            environment["FAKE_GC_CALLS"] = str(calls)
            environment["FAKE_CONTROLLER_ENVS"] = str(root / "controller-envs")
            environment["FAKE_GH_CALLS"] = str(root / "gh-calls")
            environment["FAKE_GH_ENVS"] = str(root / "gh-envs")
            environment["GH_CONFIG_DIR"] = str(credential_config)
            if environment_updates:
                environment.update(environment_updates)
            if setup:
                setup(root, rig)
            result = subprocess.run(
                ["sh", str(self.SCRIPT), *arguments],
                capture_output=True,
                text=True,
                env=environment,
            )
            result.rig = rig
            result.sink = sink
            result.slinged = sink.exists()
            result.calls = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
            controller_envs = root / "controller-envs"
            result.controller_envs = (
                controller_envs.read_text(encoding="utf-8").splitlines()
                if controller_envs.exists()
                else []
            )
            gh_calls = root / "gh-calls"
            result.gh_calls = gh_calls.read_text(encoding="utf-8").splitlines() if gh_calls.exists() else []
            gh_envs = root / "gh-envs"
            result.gh_envs = gh_envs.read_text(encoding="utf-8").splitlines() if gh_envs.exists() else []
            city_gh = city / ".config" / "gh"
            result.city_gh_is_symlink = city_gh.is_symlink()
            try:
                result.city_gh_target = str(city_gh.resolve(strict=True))
            except OSError:
                result.city_gh_target = None
            result.credential_config = str(credential_config)
            result.city = str(city)
            result.materialized = {
                "delivery_preflight": (rig / ".gc/scripts/checks/delivery-preflight.sh").is_file(),
                "build_artifact": (rig / ".gc/scripts/checks/build-artifact-valid.sh").is_file(),
                "design_review": (rig / ".gc/scripts/checks/design-review-approved.sh").is_file(),
                "implementation_review": (
                    rig / ".gc/scripts/checks/implementation-review-approved.sh"
                ).is_file(),
                "validator": (rig / ".gc/scripts/validate_build_artifact.py").is_file(),
                "schema": (rig / ".gc/schemas/build/requirements.v1.yaml").is_file(),
            }
            preflight_check = rig / ".gc/scripts/checks/delivery-preflight.sh"
            result.materialized_preflight_contents = (
                preflight_check.read_text(encoding="utf-8") if preflight_check.is_file() else ""
            )
            result.materialized_review_checks = {}
            for name in ("design-review-approved.sh", "implementation-review-approved.sh"):
                path = rig / ".gc/scripts/checks" / name
                if path.is_file():
                    result.materialized_review_checks[name] = {
                        "bytes": path.read_bytes(),
                        "executable": bool(path.stat().st_mode & stat.S_IXUSR),
                    }
            result.materialized_paths = sorted(
                str(path.relative_to(rig))
                for root in (rig / ".gc",)
                if root.exists()
                for path in root.rglob("*")
                if path.is_file()
            )
            result.legacy_schema_exists = (rig / "schemas/build/requirements.v1.yaml").exists()
            manifest = rig / ".gc" / "complete-delivery-assets.json"
            try:
                result.manifest = (
                    json.loads(manifest.read_text(encoding="utf-8"))
                    if manifest.is_file() and not manifest.is_symlink()
                    else None
                )
            except json.JSONDecodeError:
                result.manifest = None
            return result

    def test_one_step_command_launches_terminal_formula_with_ergonomic_defaults(self) -> None:
        result = self.run_command("fi-123", "--rig", "finance")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = result.stdout.splitlines()
        self.assertEqual(args[:5], ["sling", "finance/gc.run-operator", "fi-123", "--on", "complete-delivery"])
        for value in (
            "source_bead_id=fi-123",
            "source_title=Requested delivery",
            "launcher_github_preflight=github-city-v1",
            "report_title=Requested delivery",
            "interaction_mode=autonomous",
            "review_mode=agent",
            "drain_policy=separate",
            "push=true",
            "open_pr=true",
        ):
            self.assertIn(value, args)
        self.assertEqual(result.materialized, {
            "delivery_preflight": True,
            "build_artifact": True,
            "design_review": True,
            "implementation_review": True,
            "validator": True,
            "schema": True,
        })
        self.assertEqual(result.materialized_paths, [
            ".gc/complete-delivery-assets.json",
            ".gc/schemas/build/decomposition.v1.yaml",
            ".gc/schemas/build/final-report.v1.yaml",
            ".gc/schemas/build/implementation-summary.v1.yaml",
            ".gc/schemas/build/plan.v1.yaml",
            ".gc/schemas/build/requirements.v1.yaml",
            ".gc/schemas/build/review.v1.yaml",
            ".gc/scripts/checks/build-artifact-valid.sh",
            ".gc/scripts/checks/delivery-common.sh",
            ".gc/scripts/checks/delivery-external-review-deadline.sh",
            ".gc/scripts/checks/delivery-external-review-passed.sh",
            ".gc/scripts/checks/delivery-local-gates.sh",
            ".gc/scripts/checks/delivery-merged.sh",
            ".gc/scripts/checks/delivery-pr-approved.sh",
            ".gc/scripts/checks/delivery-pr-open.sh",
            ".gc/scripts/checks/delivery-preflight.sh",
            ".gc/scripts/checks/delivery-release-verified.sh",
            ".gc/scripts/checks/delivery-report-green.sh",
            ".gc/scripts/checks/delivery-report-valid.sh",
            ".gc/scripts/checks/delivery-source-artifact-valid.sh",
            ".gc/scripts/checks/design-review-approved.sh",
            ".gc/scripts/checks/implementation-review-approved.sh",
            ".gc/scripts/delivery_gate.py",
            ".gc/scripts/delivery_report.py",
            ".gc/scripts/validate_build_artifact.py",
        ])
        self.assertEqual(
            sorted(result.manifest["assets"]),
            [path for path in result.materialized_paths if path != ".gc/complete-delivery-assets.json"],
        )
        self.assertEqual(result.manifest["version"], 2)
        self.assertEqual(set(result.manifest["asset_hashes"]), set(result.manifest["assets"]))
        self.assertEqual(
            result.gh_calls,
            [
                "auth status",
                "repo view https://github.com/example/repo.git --json nameWithOwner",
                "api repos/example/repo/branches/main/protection --silent",
                "auth status",
                "repo view https://github.com/example/repo.git --json nameWithOwner",
                "api repos/example/repo/branches/main/protection --silent",
            ],
        )
        self.assertTrue(result.city_gh_is_symlink)
        self.assertEqual(result.city_gh_target, result.credential_config)
        self.assertEqual(len(result.gh_envs), 6)
        self.assertTrue(all(f"GH_CONFIG_DIR={result.credential_config}" in line for line in result.gh_envs[:3]))
        self.assertTrue(all(f"HOME={result.city}" in line for line in result.gh_envs[3:]))
        self.assertTrue(all("GH_CONFIG_DIR=|" in line for line in result.gh_envs[3:]))
        for name, materialized in result.materialized_review_checks.items():
            source = REPO_ROOT / "gascity/assets/scripts/checks" / name
            self.assertEqual(materialized["bytes"], source.read_bytes())
            self.assertTrue(materialized["executable"])

    def test_controller_local_exact_pack_is_installed_and_resolved_before_dispatch(self) -> None:
        result = self.run_command("fi-123", "--rig", "finance")

        self.assertEqual(result.returncode, 0, result.stderr)
        install = result.calls.index("import install")
        status = result.calls.index("import status --json")
        formula = result.calls.index("formula show complete-delivery --rig finance --json")
        sling = next(index for index, call in enumerate(result.calls) if call.startswith("sling "))
        self.assertLess(install, status)
        self.assertLess(status, formula)
        self.assertLess(formula, sling)
        self.assertEqual(len(result.controller_envs), 3)
        self.assertTrue(all(f"HOME={result.city}" in value for value in result.controller_envs))
        self.assertTrue(all("GC_HOME=|" in value for value in result.controller_envs))
        self.assertTrue(all(value.endswith("GC_PACK_DIR=") for value in result.controller_envs))

    def test_stale_or_foreign_controller_pack_fails_before_assets_or_dispatch(self) -> None:
        foreign_commit = "b" * 40
        status = json.dumps(
            {
                "imports": [
                    {
                        "name": "pack:complete-delivery",
                        "pin": {"commit": foreign_commit, "version": f"sha:{foreign_commit}"},
                    }
                ]
            }
        )
        result = self.run_command(
            "fi-123",
            "--rig",
            "finance",
            environment_updates={"FAKE_CONTROLLER_IMPORT_STATUS": status},
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("exact locked revision", result.stderr)
        self.assertFalse(result.slinged)
        self.assertEqual(result.materialized_paths, [])

    def test_generated_checks_are_materialized_from_the_controller_resolved_pack(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller_repo = pathlib.Path(directory) / "controller-packs"
            shutil.copytree(REPO_ROOT / "complete-delivery", controller_repo / "complete-delivery")
            shutil.copytree(REPO_ROOT / "gascity", controller_repo / "gascity")
            check = controller_repo / "complete-delivery/assets/scripts/checks/delivery-preflight.sh"
            check.write_text(
                check.read_text(encoding="utf-8") + "\n# controller-local marker\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-q", str(controller_repo)], check=True)
            subprocess.run(["git", "config", "user.email", "tests@example.invalid"], cwd=controller_repo, check=True)
            subprocess.run(["git", "config", "user.name", "Complete Delivery tests"], cwd=controller_repo, check=True)
            subprocess.run(["git", "add", "."], cwd=controller_repo, check=True)
            subprocess.run(["git", "commit", "-qm", "controller cache fixture"], cwd=controller_repo, check=True)

            result = self.run_command(
                "fi-123",
                "--rig",
                "finance",
                controller_pack_root=controller_repo / "complete-delivery",
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("controller-local marker", result.materialized_preflight_contents)

    def test_existing_exact_city_github_capability_is_revalidated_idempotently(self) -> None:
        def setup(root: pathlib.Path, _rig: pathlib.Path) -> None:
            destination = root / "city" / ".config" / "gh"
            destination.parent.mkdir()
            destination.symlink_to(root / "launcher-gh-config", target_is_directory=True)

        result = self.run_command("fi-123", "--rig", "finance", setup=setup)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.city_gh_is_symlink)
        self.assertEqual(result.city_gh_target, result.credential_config)
        self.assertEqual(len(result.gh_calls), 6)

    def test_city_github_capability_collisions_fail_before_assets_or_dispatch(self) -> None:
        def foreign_directory(root: pathlib.Path, _rig: pathlib.Path) -> None:
            destination = root / "city" / ".config" / "gh"
            destination.mkdir(parents=True)

        def foreign_symlink(root: pathlib.Path, _rig: pathlib.Path) -> None:
            foreign = root / "foreign-gh-config"
            foreign.mkdir()
            destination = root / "city" / ".config" / "gh"
            destination.parent.mkdir()
            destination.symlink_to(foreign, target_is_directory=True)

        def symlink_parent(root: pathlib.Path, _rig: pathlib.Path) -> None:
            foreign = root / "foreign-config-parent"
            foreign.mkdir()
            (root / "city" / ".config").symlink_to(foreign, target_is_directory=True)

        for setup in (foreign_directory, foreign_symlink, symlink_parent):
            with self.subTest(setup=setup.__name__):
                result = self.run_command(
                    "fi-123", "--rig", "finance", setup=setup
                )
                self.assertEqual(result.returncode, 1)
                self.assertIn("city GitHub capability", result.stderr)
                self.assertFalse(result.slinged)
                self.assertEqual(result.materialized_paths, [])

    def test_launch_refuses_a_foreign_managed_schema_destination(self) -> None:
        def setup(_root: pathlib.Path, rig: pathlib.Path) -> None:
            destination = rig / ".gc/schemas/build/requirements.v1.yaml"
            destination.parent.mkdir(parents=True)
            destination.write_text("foreign schema\n", encoding="utf-8")

        result = self.run_command("fi-123", "--rig=finance", setup=setup)

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(result.slinged)
        self.assertIn("not owned by Complete Delivery", result.stderr)

    def test_launch_refuses_a_foreign_gstack_review_check_destination(self) -> None:
        for name in ("design-review-approved.sh", "implementation-review-approved.sh"):
            with self.subTest(name=name):
                def setup(_root: pathlib.Path, rig: pathlib.Path) -> None:
                    destination = rig / ".gc/scripts/checks" / name
                    destination.parent.mkdir(parents=True)
                    destination.write_text("foreign review check\n", encoding="utf-8")

                result = self.run_command("fi-123", "--rig=finance", setup=setup)

                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(result.slinged)
                self.assertIn("not owned by Complete Delivery", result.stderr)

    def test_launch_refuses_a_tracked_managed_destination_even_when_manifest_owned(self) -> None:
        def setup(_root: pathlib.Path, rig: pathlib.Path) -> None:
            destination = rig / ".gc/scripts/checks/delivery-preflight.sh"
            destination.parent.mkdir(parents=True)
            source = subprocess.run(
                ["git", "show", "origin/main:complete-delivery/assets/scripts/checks/delivery-preflight.sh"],
                cwd=REPO_ROOT,
                capture_output=True,
                check=True,
            ).stdout
            destination.write_bytes(source)
            manifest = rig / ".gc/complete-delivery-assets.json"
            manifest.write_text(
                json.dumps({
                    "assets": [".gc/scripts/checks/delivery-preflight.sh"],
                    "inherited_from": "gascity",
                    "owner": "complete-delivery",
                    "version": 1,
                }),
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "add", ".gc/scripts/checks/delivery-preflight.sh"],
                cwd=rig,
                check=True,
            )

        result = self.run_command("fi-123", "--rig=finance", setup=setup)

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(result.slinged)
        self.assertIn("is tracked by the target rig", result.stderr)

    def test_safe_upgrade_migrates_only_manifest_owned_matching_legacy_schema(self) -> None:
        def setup(_root: pathlib.Path, rig: pathlib.Path) -> None:
            legacy = rig / "schemas/build/requirements.v1.yaml"
            legacy.parent.mkdir(parents=True)
            shutil.copy2(REPO_ROOT / "gascity/schemas/build/requirements.v1.yaml", legacy)
            manifest = rig / ".gc/complete-delivery-assets.json"
            manifest.parent.mkdir()
            manifest.write_text(
                json.dumps({
                    "assets": ["schemas/build/requirements.v1.yaml"],
                    "inherited_from": "gascity",
                    "owner": "complete-delivery",
                    "version": 1,
                }),
                encoding="utf-8",
            )

        result = self.run_command("fi-123", "--rig=finance", setup=setup)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(result.legacy_schema_exists)
        self.assertTrue(result.materialized["schema"])
        self.assertEqual(result.manifest["version"], 2)

    def test_safe_upgrade_refuses_a_modified_legacy_schema(self) -> None:
        def setup(_root: pathlib.Path, rig: pathlib.Path) -> None:
            legacy = rig / "schemas/build/requirements.v1.yaml"
            legacy.parent.mkdir(parents=True)
            legacy.write_text("modified schema\n", encoding="utf-8")
            manifest = rig / ".gc/complete-delivery-assets.json"
            manifest.parent.mkdir()
            manifest.write_text(
                json.dumps({
                    "assets": ["schemas/build/requirements.v1.yaml"],
                    "inherited_from": "gascity",
                    "owner": "complete-delivery",
                    "version": 1,
                }),
                encoding="utf-8",
            )

        result = self.run_command("fi-123", "--rig=finance", setup=setup)

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(result.slinged)
        self.assertTrue(result.legacy_schema_exists)
        self.assertIn("was modified after installation", result.stderr)

    def test_interactive_is_one_flag(self) -> None:
        result = self.run_command("fi-123", "--rig=finance", "--interactive")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = result.stdout.splitlines()
        self.assertIn("interaction_mode=interactive", args)
        self.assertIn("review_mode=interactive", args)

    def test_source_intent_is_read_before_fixture_launch(self) -> None:
        result = self.run_command(
            "fi-123",
            "--rig=finance",
            source_json='[{"id":"fi-123","title":"Reject dirty checkout"}]',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        args = result.stdout.splitlines()
        self.assertIn("source_bead_id=fi-123", args)
        self.assertIn("source_title=Reject dirty checkout", args)
        self.assertIn("report_title=Reject dirty checkout", args)

    def test_unauthenticated_launcher_fails_before_assets_or_dispatch(self) -> None:
        result = self.run_command(
            "fi-123",
            "--rig=finance",
            environment_updates={
                "FAKE_GH_AUTHENTICATED": "false",
                "FAKE_GH_FAILURE_DIAGNOSTIC": "opaque-token-fixture",
            },
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("gh authentication check failed", result.stderr)
        self.assertNotIn("opaque-token-fixture", result.stderr)
        self.assertFalse(result.slinged)
        self.assertEqual(result.materialized_paths, [])

    def test_unprotected_launcher_base_branch_fails_before_dispatch(self) -> None:
        result = self.run_command(
            "fi-123",
            "--rig=finance",
            environment_updates={"FAKE_GH_PROTECTED": "false"},
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("protected base branch check for main failed", result.stderr)
        self.assertFalse(result.slinged)
        self.assertEqual(result.materialized_paths, [])

    def test_missing_or_ambiguous_source_fails_before_dispatch(self) -> None:
        for source_json in ("[]", '[{"title":"one"},{"title":"two"}]', '[{}]'):
            with self.subTest(source_json=source_json):
                result = self.run_command("fi-123", "--rig=finance", source_json=source_json)
                self.assertEqual(result.returncode, 1)
                self.assertIn("cannot resolve exact source intent", result.stderr)
                self.assertNotIn("sling", result.stdout)

        mismatched = self.run_command(
            "fi-123",
            "--rig=finance",
            source_json='[{"id":"fi-other","title":"Wrong source"}]',
        )
        self.assertEqual(mismatched.returncode, 1)
        self.assertIn("cannot resolve exact source intent", mismatched.stderr)
        self.assertFalse(mismatched.slinged)

    def test_invalid_bead_is_rejected_before_dispatch(self) -> None:
        result = self.run_command("fi-123;echo-bad", "--rig", "finance")
        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid bead id", result.stderr)

    def test_option_shaped_bead_and_empty_agent_fail_before_any_gc_call(self) -> None:
        for arguments in (
            ("-fi-123", "--rig", "finance"),
            ("fi-123", "--rig", "finance", "--agent="),
        ):
            with self.subTest(arguments=arguments):
                result = self.run_command(*arguments)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.calls, [])
                self.assertFalse(result.slinged)

    def test_missing_flag_value_has_actionable_error(self) -> None:
        result = self.run_command("fi-123", "--rig")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--rig requires a value", result.stderr)

    def test_invalid_profile_fails_before_sling(self) -> None:
        result = self.run_command(
            "fi-123",
            "--rig",
            "finance",
            profile={"deploy_mode": "command"},
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("Complete Delivery profile is invalid", result.stderr)
        self.assertFalse(result.slinged)
        self.assertEqual(result.materialized_paths, [])

    def test_profile_validation_matches_runtime_preflight_semantics(self) -> None:
        invalid_profiles = (
            {
                "setup_command": " \t ",
                "deploy_mode": "not-applicable",
                "deploy_not_applicable_reason": "fixture",
            },
            {
                "setup_command": "/bin/true",
                "allow_no_smoke": "true",
                "no_smoke_reason": " \t ",
                "deploy_mode": "not-applicable",
                "deploy_not_applicable_reason": "fixture",
            },
            {
                "setup_command": "/bin/true",
                "deploy_mode": "command",
                "deploy_command": " \t ",
                "deploy_verify_command": "/bin/true",
                "smoke_command": "/bin/true",
            },
            {
                "setup_command": "/bin/true",
                "deploy_mode": "ci",
                "deploy_ci_workflow": ".github/workflows/../deploy.yml",
                "deploy_environment": "production",
                "deploy_verify_command": "/bin/true",
                "smoke_command": "/bin/true",
            },
            {
                "setup_command": "/bin/true",
                "deploy_mode": "command",
                "deploy_command": "/bin/true",
                "deploy_verify_command": "/bin/true",
                "smoke_command": "/bin/true",
                "smoke_timeout": "2h",
            },
        )
        for profile in invalid_profiles:
            with self.subTest(profile=profile):
                result = self.run_command("fi-123", "--rig", "finance", profile=profile)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertFalse(result.slinged)
                self.assertEqual(result.materialized_paths, [])

        conditionally_unused = {
            "setup_command": "/bin/true",
            "allow_no_smoke": "true",
            "no_smoke_reason": "No production smoke surface",
            "smoke_timeout": "unused-and-invalid",
            "deploy_mode": "ci",
            "deploy_ci_workflow": ".github/workflows/deploy.yml",
            "deploy_environment": "production",
            "deploy_verify_command": "/bin/true",
            "deploy_timeout": "unused-and-invalid",
        }
        result = self.run_command(
            "fi-123", "--rig", "finance", profile=conditionally_unused
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_url_artifact_and_stale_root_fail_without_managed_writes(self) -> None:
        for value in (
            "https:///missing-host",
            "https://-bad.example",
            "https://user@example.com",
        ):
            invalid_url = {
                "setup_command": "/bin/true",
                "deploy_mode": "not-applicable",
                "deploy_not_applicable_reason": "fixture",
                "production_url": value,
            }
            with self.subTest(production_url=value):
                result = self.run_command(
                    "fi-123", "--rig", "finance", profile=invalid_url
                )
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.materialized_paths, [])

        traversal = self.run_command(
            "fi-123", "--rig", "finance", "--artifact-root", "../escape"
        )
        self.assertEqual(traversal.returncode, 1)
        self.assertIn("artifact_root", traversal.stderr)
        self.assertEqual(traversal.materialized_paths, [])

        def populate(_root, rig):
            target = rig / "plans" / "complete-delivery" / "fi-123"
            target.mkdir(parents=True)
            (target / "state.json").write_text("stale", encoding="utf-8")

        stale = self.run_command("fi-123", "--rig", "finance", setup=populate)
        self.assertEqual(stale.returncode, 1)
        self.assertIn("already populated", stale.stderr)
        self.assertFalse(stale.slinged)
        self.assertIsNone(stale.manifest)

    def test_symlink_escapes_and_manifest_symlink_fail_before_asset_copy(self) -> None:
        def artifact_escape(root, rig):
            outside = root / "outside-artifacts"
            outside.mkdir()
            (rig / "plans").symlink_to(outside, target_is_directory=True)

        escaped = self.run_command("fi-123", "--rig", "finance", setup=artifact_escape)
        self.assertEqual(escaped.returncode, 1)
        self.assertIn("artifact_root", escaped.stderr)
        self.assertIsNone(escaped.manifest)

        def contained_artifact_symlink(_root, rig):
            target = rig / "real-plans"
            target.mkdir()
            (rig / "plans").symlink_to(target, target_is_directory=True)

        contained = self.run_command(
            "fi-123", "--rig", "finance", setup=contained_artifact_symlink
        )
        self.assertEqual(contained.returncode, 1)
        self.assertIn("symlink", contained.stderr)
        self.assertIsNone(contained.manifest)

        def manifest_escape(root, rig):
            outside = root / "foreign-manifest.json"
            outside.write_text("foreign", encoding="utf-8")
            managed = rig / ".gc"
            managed.mkdir()
            (managed / "complete-delivery-assets.json").symlink_to(outside)

        manifest = self.run_command("fi-123", "--rig", "finance", setup=manifest_escape)
        self.assertEqual(manifest.returncode, 1)
        self.assertIn("symlink", manifest.stderr)
        self.assertFalse(manifest.slinged)
        self.assertFalse(manifest.materialized["delivery_preflight"])

    def test_source_title_controls_and_malformed_config_fail_closed(self) -> None:
        controlled = self.run_command(
            "fi-123",
            "--rig",
            "finance",
            source_json=json.dumps([{"id": "fi-123", "title": "unsafe\nsource"}]),
        )
        self.assertEqual(controlled.returncode, 1)
        self.assertIn("cannot resolve exact source intent", controlled.stderr)
        self.assertFalse(controlled.slinged)

        malformed = self.run_command(
            "fi-123", "--rig", "finance", config_payload={"unexpected": []}
        )
        self.assertEqual(malformed.returncode, 1)
        self.assertIn("exact resolved config object", malformed.stderr)
        self.assertFalse(malformed.slinged)
        self.assertEqual(malformed.materialized_paths, [])

    def test_config_lookup_source_lookup_and_sling_have_distinct_timeouts(self) -> None:
        config_timeout = self.run_command(
            "fi-123",
            "--rig",
            "finance",
            environment_updates={
                "FAKE_GC_SLEEP_STAGE": "config",
                "GC_COMPLETE_DELIVERY_CONFIG_TIMEOUT_SECONDS": "0.05",
            },
        )
        self.assertEqual(config_timeout.returncode, 1)
        self.assertIn("gc config show timed out", config_timeout.stderr)
        self.assertFalse(config_timeout.slinged)
        self.assertEqual(config_timeout.materialized_paths, [])

        source_timeout = self.run_command(
            "fi-123",
            "--rig",
            "finance",
            environment_updates={
                "FAKE_GC_SLEEP_STAGE": "source",
                "GC_COMPLETE_DELIVERY_LOOKUP_TIMEOUT": "0.05s",
            },
        )
        self.assertEqual(source_timeout.returncode, 1)
        self.assertIn("gc bd show timed out", source_timeout.stderr)
        self.assertFalse(source_timeout.slinged)
        self.assertEqual(source_timeout.materialized_paths, [])

        sling_timeout = self.run_command(
            "fi-123",
            "--rig",
            "finance",
            environment_updates={
                "FAKE_GC_SLEEP_STAGE": "sling",
                "GC_COMPLETE_DELIVERY_SLING_TIMEOUT": "0.05s",
            },
        )
        self.assertEqual(sling_timeout.returncode, 1)
        self.assertIn("gc sling timed out", sling_timeout.stderr)
        self.assertFalse(sling_timeout.slinged)

    def test_report_publisher_command_uses_resolved_pack_root(self) -> None:
        wrapper = PACK_ROOT / "commands" / "report" / "publish" / "run.sh"
        text = wrapper.read_text(encoding="utf-8")
        self.assertIn('"$GC_PACK_DIR/assets/scripts/publish_delivery_report.py"', text)
        self.assertTrue((wrapper.parent / "help.md").is_file())


class GithubCapabilityUnitTests(unittest.TestCase):
    def test_config_source_honors_gh_then_xdg_then_home_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            gh_override = root / "gh-override"
            xdg = root / "xdg"
            home = root / "home"
            for path in (gh_override, xdg / "gh", home / ".config" / "gh"):
                path.mkdir(parents=True)

            self.assertEqual(
                prepare_delivery.github_config_source(
                    {
                        "GH_CONFIG_DIR": str(gh_override),
                        "XDG_CONFIG_HOME": str(xdg),
                        "HOME": str(home),
                    }
                ),
                gh_override,
            )
            self.assertEqual(
                prepare_delivery.github_config_source(
                    {"XDG_CONFIG_HOME": str(xdg), "HOME": str(home)}
                ),
                xdg / "gh",
            )
            self.assertEqual(
                prepare_delivery.github_config_source({"HOME": str(home)}),
                home / ".config" / "gh",
            )

    def test_config_source_fails_closed_without_one_absolute_existing_directory(self) -> None:
        for environment in (
            {},
            {"GH_CONFIG_DIR": "relative"},
            {"GH_CONFIG_DIR": "/definitely/missing/complete-delivery-gh"},
        ):
            with self.subTest(environment=environment):
                with self.assertRaises(prepare_delivery.LaunchPreflightError):
                    prepare_delivery.github_config_source(environment)

    def test_controller_probe_removes_github_and_xdg_overrides(self) -> None:
        city = pathlib.Path("/srv/example-city")
        with mock.patch.dict(
            os.environ,
            {
                "HOME": "/user/home",
                "GC_HOME": "/caller/cache",
                "GC_PACK_DIR": "/caller/complete-delivery",
                "PATH": "/usr/bin:/bin",
                "GH_CONFIG_DIR": "/credential/config",
                "GH_TOKEN": "secret",
                "GH_REPO": "wrong/repo",
                "GITHUB_REPOSITORY": "wrong/repo",
                "XDG_CONFIG_HOME": "/user/config",
                "LANG": "C.UTF-8",
            },
            clear=True,
        ):
            environment = prepare_delivery.sanitized_city_github_environment(city)

        self.assertEqual(environment["HOME"], str(city))
        self.assertEqual(environment["PATH"], "/usr/bin:/bin")
        self.assertEqual(environment["LANG"], "C.UTF-8")
        self.assertFalse(
            any(name.startswith(("GH_", "GITHUB_", "XDG_")) for name in environment)
        )
        self.assertNotIn("GC_HOME", environment)
        self.assertNotIn("GC_PACK_DIR", environment)


class MaterializationRecoveryTests(unittest.TestCase):
    # LEGACY_V1_ASSET_HASHES attests the 0.1.0 release, not the mutable main
    # branch. Keep this fixture pinned to those release bytes so it exercises
    # the supported legacy upgrade rather than manufacturing a false tamper.
    LEGACY_V1_RELEASE = "d8fc7e834f2c66101d1141c80db58e7fa82594bf"

    def make_rig(self, root: pathlib.Path) -> pathlib.Path:
        rig = root / "rig"
        rig.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(rig)], check=True)
        return rig

    def install_real_v1_inventory(self, rig: pathlib.Path) -> None:
        for relative in prepare_delivery.LEGACY_V1_ASSET_HASHES:
            destination = rig / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if relative == ".gc/scripts/checks/build-artifact-valid.sh":
                source = "gascity/assets/scripts/checks/build-artifact-valid.sh"
            elif relative == ".gc/scripts/validate_build_artifact.py":
                source = "gascity/assets/scripts/validate_build_artifact.py"
            elif relative.startswith(".gc/scripts/checks/"):
                source = f"complete-delivery/assets/scripts/checks/{pathlib.Path(relative).name}"
            elif relative.startswith(".gc/scripts/"):
                source = f"complete-delivery/assets/scripts/{pathlib.Path(relative).name}"
            elif relative.startswith("schemas/build/"):
                source = f"gascity/{relative}"
            else:
                self.fail(f"unexpected v1 asset {relative}")
            contents = subprocess.run(
                ["git", "show", f"{self.LEGACY_V1_RELEASE}:{source}"],
                cwd=REPO_ROOT,
                capture_output=True,
                check=True,
            ).stdout
            destination.write_bytes(contents)
        manifest = rig / ".gc" / prepare_delivery.MANIFEST_NAME
        manifest.write_text(
            json.dumps(
                {
                    "assets": sorted(prepare_delivery.LEGACY_V1_ASSET_HASHES),
                    "inherited_from": "gascity",
                    "owner": "complete-delivery",
                    "version": 1,
                }
            ),
            encoding="utf-8",
        )

    def test_full_realistic_v1_inventory_upgrades_only_known_release_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rig = self.make_rig(pathlib.Path(directory))
            self.install_real_v1_inventory(rig)

            installed = prepare_delivery.materialize_assets(PACK_ROOT, rig)

            self.assertEqual(len(installed), 24)
            self.assertFalse((rig / "schemas/build/requirements.v1.yaml").exists())
            manifest = json.loads(
                (rig / ".gc" / prepare_delivery.MANIFEST_NAME).read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["version"], prepare_delivery.MANIFEST_VERSION)
            self.assertEqual(set(manifest["asset_hashes"]), set(manifest["assets"]))
            for name in ("design-review-approved.sh", "implementation-review-approved.sh"):
                relative = f".gc/scripts/checks/{name}"
                destination = rig / relative
                source = REPO_ROOT / "gascity/assets/scripts/checks" / name
                self.assertIn(relative, installed)
                self.assertEqual(destination.read_bytes(), source.read_bytes())
                self.assertEqual(stat.S_IMODE(destination.stat().st_mode), stat.S_IMODE(source.stat().st_mode))

            rejected_rig = self.make_rig(pathlib.Path(directory) / "rejected")
            self.install_real_v1_inventory(rejected_rig)
            changed = rejected_rig / ".gc/scripts/checks/delivery-preflight.sh"
            changed.write_text("locally modified\n", encoding="utf-8")
            with self.assertRaisesRegex(prepare_delivery.LaunchPreflightError, "modified"):
                prepare_delivery.materialize_assets(PACK_ROOT, rejected_rig)

    def test_interrupted_copy_stale_cleanup_and_journal_cleanup_recover_on_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rig = self.make_rig(pathlib.Path(directory))
            original_copy = prepare_delivery.atomic_copy
            copies = 0

            def fail_second_copy(*args, **kwargs):
                nonlocal copies
                copies += 1
                if copies == 2:
                    raise OSError("injected copy failure")
                return original_copy(*args, **kwargs)

            with mock.patch.object(prepare_delivery, "atomic_copy", side_effect=fail_second_copy):
                with self.assertRaises(OSError):
                    prepare_delivery.materialize_assets(PACK_ROOT, rig)
            self.assertTrue((rig / ".gc" / prepare_delivery.TRANSACTION_NAME).exists())
            prepare_delivery.materialize_assets(PACK_ROOT, rig)
            self.assertFalse((rig / ".gc" / prepare_delivery.TRANSACTION_NAME).exists())

            tampered_rig = self.make_rig(pathlib.Path(directory) / "tampered")
            copies = 0
            with mock.patch.object(prepare_delivery, "atomic_copy", side_effect=fail_second_copy):
                with self.assertRaises(OSError):
                    prepare_delivery.materialize_assets(PACK_ROOT, tampered_rig)
            (tampered_rig / ".gc/scripts/checks/delivery-common.sh").write_text(
                "tampered after interruption\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(prepare_delivery.LaunchPreflightError, "modified asset"):
                prepare_delivery.materialize_assets(PACK_ROOT, tampered_rig)

            stale_rig = self.make_rig(pathlib.Path(directory) / "stale")
            self.install_real_v1_inventory(stale_rig)
            original_write = prepare_delivery.atomic_write_text
            writes = 0

            def fail_manifest_write(*args, **kwargs):
                nonlocal writes
                writes += 1
                if writes == 2:
                    raise OSError("injected manifest replacement failure")
                return original_write(*args, **kwargs)

            with mock.patch.object(prepare_delivery, "atomic_write_text", side_effect=fail_manifest_write):
                with self.assertRaises(OSError):
                    prepare_delivery.materialize_assets(PACK_ROOT, stale_rig)
            self.assertTrue((stale_rig / ".gc" / prepare_delivery.TRANSACTION_NAME).exists())
            prepare_delivery.materialize_assets(PACK_ROOT, stale_rig)
            self.assertFalse((stale_rig / "schemas/build/requirements.v1.yaml").exists())

            unlink_rig = self.make_rig(pathlib.Path(directory) / "unlink")
            self.install_real_v1_inventory(unlink_rig)
            original_unlink = pathlib.Path.unlink

            def fail_mid_stale_cleanup(path, *args, **kwargs):
                if str(path).endswith("schemas/build/requirements.v1.yaml"):
                    raise OSError("injected stale cleanup failure")
                return original_unlink(path, *args, **kwargs)

            with mock.patch.object(pathlib.Path, "unlink", new=fail_mid_stale_cleanup):
                with self.assertRaises(OSError):
                    prepare_delivery.materialize_assets(PACK_ROOT, unlink_rig)
            self.assertTrue((unlink_rig / ".gc" / prepare_delivery.TRANSACTION_NAME).exists())
            prepare_delivery.materialize_assets(PACK_ROOT, unlink_rig)
            self.assertFalse((unlink_rig / "schemas/build/review.v1.yaml").exists())

            completed_rig = self.make_rig(pathlib.Path(directory) / "completed")
            self.install_real_v1_inventory(completed_rig)
            journal_removed = False

            def fail_first_journal_cleanup(path, *args, **kwargs):
                nonlocal journal_removed
                if path.name == prepare_delivery.TRANSACTION_NAME and not journal_removed:
                    journal_removed = True
                    raise OSError("injected post-manifest cleanup failure")
                return original_unlink(path, *args, **kwargs)

            with mock.patch.object(pathlib.Path, "unlink", new=fail_first_journal_cleanup):
                with self.assertRaises(OSError):
                    prepare_delivery.materialize_assets(PACK_ROOT, completed_rig)
            self.assertTrue((completed_rig / ".gc" / prepare_delivery.TRANSACTION_NAME).exists())
            prepare_delivery.materialize_assets(PACK_ROOT, completed_rig)
            self.assertFalse((completed_rig / ".gc" / prepare_delivery.TRANSACTION_NAME).exists())

    def test_recovery_rejects_tampered_or_malformed_stale_path_journals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)

            tampered_rig = self.make_rig(root / "tampered-stale-path")
            self.install_real_v1_inventory(tampered_rig)

            with mock.patch.object(
                prepare_delivery, "atomic_copy", side_effect=OSError("injected copy failure")
            ):
                with self.assertRaises(OSError):
                    prepare_delivery.materialize_assets(PACK_ROOT, tampered_rig)

            journal_path = tampered_rig / ".gc" / prepare_delivery.TRANSACTION_NAME
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            desired_relative = ".gc/scripts/checks/delivery-preflight.sh"
            desired_path = tampered_rig / desired_relative
            before = desired_path.read_bytes()
            journal["stale_paths"].append(desired_relative)
            # This matches the desired path's original state, as an attacker
            # would need to do to satisfy the old per-path state check.
            journal["prior_states"][str(desired_path)] = journal["prior_states"][desired_relative]
            journal_path.write_text(json.dumps(journal), encoding="utf-8")

            with self.assertRaisesRegex(prepare_delivery.LaunchPreflightError, "stale paths"):
                prepare_delivery.materialize_assets(PACK_ROOT, tampered_rig)
            self.assertEqual(desired_path.read_bytes(), before)
            self.assertTrue(journal_path.exists())

            malformed_rig = self.make_rig(root / "malformed-stale-path")
            self.install_real_v1_inventory(malformed_rig)
            with mock.patch.object(
                prepare_delivery, "atomic_copy", side_effect=OSError("injected copy failure")
            ):
                with self.assertRaises(OSError):
                    prepare_delivery.materialize_assets(PACK_ROOT, malformed_rig)
            malformed_journal_path = malformed_rig / ".gc" / prepare_delivery.TRANSACTION_NAME
            malformed_journal = json.loads(malformed_journal_path.read_text(encoding="utf-8"))
            malformed_journal["stale_paths"] = [42]
            malformed_journal_path.write_text(json.dumps(malformed_journal), encoding="utf-8")

            with self.assertRaisesRegex(prepare_delivery.LaunchPreflightError, "unsafe stale paths"):
                prepare_delivery.materialize_assets(PACK_ROOT, malformed_rig)

    def test_recovery_allows_initially_absent_authenticated_stale_assets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rig = self.make_rig(pathlib.Path(directory))
            self.install_real_v1_inventory(rig)
            missing_relative = "schemas/build/requirements.v1.yaml"
            (rig / missing_relative).unlink()

            with mock.patch.object(
                prepare_delivery, "atomic_copy", side_effect=OSError("injected copy failure")
            ):
                with self.assertRaises(OSError):
                    prepare_delivery.materialize_assets(PACK_ROOT, rig)

            journal = json.loads(
                (rig / ".gc" / prepare_delivery.TRANSACTION_NAME).read_text(encoding="utf-8")
            )
            self.assertIn(missing_relative, journal["stale_paths"])
            prepare_delivery.materialize_assets(PACK_ROOT, rig)
            self.assertFalse((rig / missing_relative).exists())

    def test_recovery_reinstalls_initially_absent_manifest_owned_desired_asset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rig = self.make_rig(pathlib.Path(directory))
            self.install_real_v1_inventory(rig)
            missing_relative = ".gc/scripts/checks/delivery-preflight.sh"
            (rig / missing_relative).unlink()

            original_copy = prepare_delivery.atomic_copy
            copies = 0

            def fail_later_copy(*args, **kwargs):
                nonlocal copies
                copies += 1
                if copies == 2:
                    raise OSError("injected later copy failure")
                return original_copy(*args, **kwargs)

            with mock.patch.object(prepare_delivery, "atomic_copy", side_effect=fail_later_copy):
                with self.assertRaisesRegex(OSError, "later copy failure"):
                    prepare_delivery.materialize_assets(PACK_ROOT, rig)

            self.assertTrue((rig / ".gc" / prepare_delivery.TRANSACTION_NAME).exists())
            plan = prepare_delivery.materialization_plan(PACK_ROOT, rig)
            source = next(source for source, _, relative in plan if relative == missing_relative)
            prepare_delivery.materialize_assets(PACK_ROOT, rig)
            self.assertEqual((rig / missing_relative).read_bytes(), source.read_bytes())
            self.assertFalse((rig / ".gc" / prepare_delivery.TRANSACTION_NAME).exists())


class ReportSecurityContractTests(unittest.TestCase):
    SOURCE_ID = "fi-123"
    SOURCE_TITLE = "Requested delivery"
    MERGE_SHA = "a" * 40
    PR_URL = "https://github.com/example/repo/pull/123"

    def complete_state(
        self, root: pathlib.Path, *, no_smoke_reason: str = ""
    ) -> pathlib.Path:
        state_path = root / "state.json"
        self.assertEqual(
            delivery_report.main(
                [
                    "init",
                    "--state",
                    str(state_path),
                    "--title",
                    self.SOURCE_TITLE,
                    "--goal",
                    "Reach verified production",
                    "--repo",
                    "example/repo",
                    "--bead-id",
                    self.SOURCE_ID,
                ]
            ),
            0,
        )
        state = delivery_report.load_state(state_path)
        for stage in delivery_report.STAGES:
            state["stages"][stage] = {
                "status": "passed",
                "summary": "Verified",
                "evidence": [],
            }
        state.update(
            sha=self.MERGE_SHA,
            pr_url=self.PR_URL,
            next_action="No action required",
            no_smoke_reason=no_smoke_reason,
        )
        delivery_report.persist(state_path, state)
        return state_path

    def final_args(self, state_path: pathlib.Path, **changes):
        values = {
            "state": state_path,
            "merge_sha": self.MERGE_SHA,
            "deployed_sha": self.MERGE_SHA,
            "deploy_status": "verified",
            "pr_url": self.PR_URL,
            "production_url": "",
            "source_bead_id": self.SOURCE_ID,
            "source_title": self.SOURCE_TITLE,
            "require_no_smoke_reason": False,
            "no_smoke_reason": "",
            "expected_no_smoke_reason": "",
        }
        values.update(changes)
        return types.SimpleNamespace(**values)

    def test_early_complete_marker_never_makes_report_live(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            state_path = root / "state.json"
            delivery_report.main(
                [
                    "init",
                    "--state",
                    str(state_path),
                    "--title",
                    self.SOURCE_TITLE,
                    "--goal",
                    "Goal",
                    "--bead-id",
                    self.SOURCE_ID,
                ]
            )
            state = delivery_report.load_state(state_path)
            state["stages"]["complete"] = {
                "status": "passed",
                "summary": "Premature",
                "evidence": [],
            }
            delivery_report.persist(state_path, state)
            self.assertEqual(delivery_report.overall_status(state), "in progress")
            with self.assertRaisesRegex(delivery_report.ReportError, "stage 'plan'"):
                delivery_report.validate_final(self.final_args(state_path))

    def test_no_smoke_reason_must_match_and_is_rendered_as_plaintext(self) -> None:
        reason = "No production smoke endpoint is exposed"
        with tempfile.TemporaryDirectory() as directory:
            state_path = self.complete_state(pathlib.Path(directory), no_smoke_reason=reason)
            with self.assertRaisesRegex(delivery_report.ReportError, "does not match"):
                delivery_report.validate_final(
                    self.final_args(
                        state_path,
                        require_no_smoke_reason=True,
                        no_smoke_reason="A different reason",
                        expected_no_smoke_reason=reason,
                    )
                )

            state = delivery_report.validate_final(
                self.final_args(
                    state_path,
                    require_no_smoke_reason=True,
                    no_smoke_reason=reason,
                    expected_no_smoke_reason=reason,
                )
            )
            self.assertEqual(delivery_report.overall_status(state), "live")
            rendered = (state_path.parent / "index.html").read_text(encoding="utf-8")
            self.assertIn(reason, rendered)
            self.assertIn("Production smoke-test exception", rendered)

    def test_final_marker_binds_urls_deploy_semantics_and_smoke_exception_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = self.complete_state(pathlib.Path(directory))
            production_url = "https://service.example.test"
            state = delivery_report.load_state(state_path)
            state["production_url"] = production_url
            delivery_report.persist(state_path, state)
            state = delivery_report.validate_final(
                self.final_args(state_path, production_url=production_url)
            )
            self.assertEqual(state["delivery"]["pr_url"], self.PR_URL)
            self.assertEqual(state["final_validation"]["pr_url"], self.PR_URL)
            self.assertEqual(state["delivery"]["production_url"], production_url)
            self.assertEqual(
                state["final_validation"]["production_url"], production_url
            )

            state["pr_url"] = "https://github.com/example/repo/pull/999"
            delivery_report.persist(state_path, state)
            self.assertEqual(delivery_report.overall_status(state), "in progress")
            with self.assertRaisesRegex(delivery_report.ReportError, "pull-request URL"):
                delivery_report.validate_final(
                    self.final_args(state_path, production_url=production_url)
                )

        with tempfile.TemporaryDirectory() as directory:
            state_path = self.complete_state(pathlib.Path(directory))
            with self.assertRaisesRegex(delivery_report.ReportError, "must not record"):
                delivery_report.validate_final(
                    self.final_args(state_path, deploy_status="not_applicable")
                )

        with tempfile.TemporaryDirectory() as directory:
            state_path = self.complete_state(
                pathlib.Path(directory), no_smoke_reason="Stale exception"
            )
            with self.assertRaisesRegex(delivery_report.ReportError, "stale no-smoke"):
                delivery_report.validate_final(self.final_args(state_path))

        with tempfile.TemporaryDirectory() as directory:
            state_path = self.complete_state(pathlib.Path(directory))
            unsafe = "https://-bad.example/pull/123"
            state = delivery_report.load_state(state_path)
            state["pr_url"] = unsafe
            delivery_report.persist(state_path, state)
            with self.assertRaisesRegex(delivery_report.ReportError, "unsafe"):
                delivery_report.validate_final(self.final_args(state_path, pr_url=unsafe))

    def test_tampered_html_css_and_source_binding_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            state_path = self.complete_state(root)
            report = root / "index.html"
            report.write_text(report.read_text(encoding="utf-8") + "tamper", encoding="utf-8")
            with self.assertRaisesRegex(delivery_report.ReportError, "stale or tampered"):
                delivery_report.validate_final(self.final_args(state_path))

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            state_path = self.complete_state(root)
            stylesheet = root / "styles.css"
            stylesheet.write_text("tamper", encoding="utf-8")
            with self.assertRaisesRegex(delivery_report.ReportError, "stylesheet"):
                delivery_report.validate_final(self.final_args(state_path))

        with tempfile.TemporaryDirectory() as directory:
            state_path = self.complete_state(pathlib.Path(directory))
            with self.assertRaisesRegex(delivery_report.ReportError, "source bead ID"):
                delivery_report.validate_final(
                    self.final_args(state_path, source_bead_id="fi-other")
                )

    def run_report_check(self, modifier=None) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            report_root = root / "artifacts" / "delivery-report"
            report_root.mkdir(parents=True)
            state_path = self.complete_state(report_root)
            metadata = {
                "gc.var.artifact_root": "artifacts",
                "delivery.report_path": "artifacts/delivery-report/index.html",
                "delivery.report_state_path": "artifacts/delivery-report/state.json",
                "delivery.merge_sha": self.MERGE_SHA,
                "delivery.deployed_sha": self.MERGE_SHA,
                "delivery.deploy_status": "verified",
                "delivery.pr_url": self.PR_URL,
                "gc.var.production_url": "",
                "gc.var.source_bead_id": self.SOURCE_ID,
                "gc.var.source_title": self.SOURCE_TITLE,
                "gc.var.deploy_mode": "command",
                "gc.var.allow_no_smoke": "false",
                "gc.var.smoke_command": "/bin/true",
                "gc.var.no_smoke_reason": "",
                "delivery.no_smoke_reason": "",
            }
            if modifier:
                modifier(root, report_root, state_path, metadata)

            bin_dir = root / "bin"
            bin_dir.mkdir()
            fake_gc = bin_dir / "gc"
            fake_gc.write_text(
                "#!/bin/sh\n"
                "if [ \"${1:-}\" = bd ] && [ \"${2:-}\" = show ]; then\n"
                "  if [ \"${3:-}\" = step-1 ]; then printf '%s\\n' \"$FAKE_STEP_JSON\"; "
                "  else printf '%s\\n' \"$FAKE_ROOT_JSON\"; fi\n"
                "  exit 0\n"
                "fi\n"
                "printf 'unexpected gc call: %s\\n' \"$*\" >&2\n"
                "exit 1\n",
                encoding="utf-8",
            )
            fake_gc.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                {
                    "GC_BEAD_ID": "step-1",
                    "GC_WORK_DIR": str(root),
                    "FAKE_STEP_JSON": json.dumps(
                        [{"metadata": {"gc.root_bead_id": "root-1"}}]
                    ),
                    "FAKE_ROOT_JSON": json.dumps([{"metadata": metadata}]),
                    "PATH": f"{bin_dir}:{environment['PATH']}",
                }
            )
            result = subprocess.run(
                ["bash", str(REPORT_CHECK)],
                capture_output=True,
                text=True,
                env=environment,
            )
            result.report_text = (
                (report_root / "index.html").read_text(encoding="utf-8")
                if (report_root / "index.html").is_file()
                else ""
            )
            return result

    def test_shell_validator_binds_one_bundle_and_rejects_symlink_and_tamper(self) -> None:
        valid = self.run_report_check()
        self.assertEqual(valid.returncode, 0, valid.stderr)
        self.assertIn("Live", valid.report_text)

        def split(_root, _report_root, _state_path, metadata):
            metadata["delivery.report_path"] = "artifacts/other/index.html"

        split_result = self.run_report_check(split)
        self.assertEqual(split_result.returncode, 1)
        self.assertIn("canonical contained report bundle", split_result.stderr)

        def symlink(root, report_root, _state_path, _metadata):
            trusted = root / "trusted-report"
            report_root.rename(trusted)
            report_root.symlink_to(trusted, target_is_directory=True)

        symlink_result = self.run_report_check(symlink)
        self.assertEqual(symlink_result.returncode, 1)
        self.assertIn("canonical contained report bundle", symlink_result.stderr)

        def tamper(_root, report_root, _state_path, _metadata):
            report = report_root / "index.html"
            report.write_text(report.read_text(encoding="utf-8") + "tamper", encoding="utf-8")

        tampered = self.run_report_check(tamper)
        self.assertEqual(tampered.returncode, 1)
        self.assertIn("stale or tampered", tampered.stdout)

        def mismatched_deploy_mode(_root, _report_root, _state_path, metadata):
            metadata["gc.var.deploy_mode"] = "not-applicable"

        mismatch = self.run_report_check(mismatched_deploy_mode)
        self.assertEqual(mismatch.returncode, 1)
        self.assertIn("inconsistent", mismatch.stderr)

    def test_shell_validator_rejects_mismatched_durable_no_smoke_reason(self) -> None:
        def mismatch(_root, report_root, state_path, metadata):
            reason = "No production smoke surface"
            state = delivery_report.load_state(state_path)
            state["no_smoke_reason"] = reason
            delivery_report.persist(state_path, state)
            metadata.update(
                {
                    "gc.var.allow_no_smoke": "true",
                    "gc.var.smoke_command": "",
                    "gc.var.no_smoke_reason": reason,
                    "delivery.no_smoke_reason": "A different durable reason",
                }
            )

        result = self.run_report_check(mismatch)
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not match", result.stdout)


if __name__ == "__main__":
    unittest.main()
