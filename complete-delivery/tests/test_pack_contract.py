from __future__ import annotations

import os
import pathlib
import stat
import subprocess
import tempfile
import tomllib
import unittest


PACK_ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO_ROOT = PACK_ROOT.parent
FORMULA_DIR = PACK_ROOT / "formulas"


def load_toml(path: pathlib.Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def formula_nodes(formula: dict):
    for node in formula.get("steps", []):
        yield node
    for node in formula.get("template", []):
        yield node
        yield from node.get("children", [])


class PackContractTests(unittest.TestCase):
    def test_pack_imports_and_formula_identity(self) -> None:
        pack = load_toml(PACK_ROOT / "pack.toml")
        formula = load_toml(FORMULA_DIR / "complete-delivery.formula.toml")
        self.assertEqual(pack["pack"]["name"], "complete-delivery")
        self.assertEqual(pack["pack"]["schema"], 2)
        self.assertEqual(pack["imports"]["gstack"]["source"], "../gstack")
        self.assertEqual(formula["formula"], "complete-delivery")
        self.assertEqual(formula["extends"], ["gstack-build"])
        self.assertEqual(formula["contract"], "graph.v2")
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


class FormulaContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.delivery = load_toml(FORMULA_DIR / "complete-delivery.formula.toml")
        cls.gate = load_toml(FORMULA_DIR / "complete-delivery-pr-gate.formula.toml")
        cls.steps = {step["id"]: step for step in cls.delivery["steps"]}

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
            "report-green": ["external-review"],
            "merge": ["report-green"],
            "report-merged": ["merge"],
            "deploy": ["report-merged"],
            "verify-production": ["deploy"],
            "report-complete": ["verify-production"],
        }
        for step, needs in expected_needs.items():
            with self.subTest(step=step):
                self.assertEqual(self.steps[step]["needs"], needs)

    def test_external_review_loop_is_bounded_and_ordered(self) -> None:
        self.assertEqual(self.steps["external-review"]["expand"], "complete-delivery-pr-gate")
        templates = {node["id"]: node for node in self.gate["template"]}
        loop = templates["{target}.external-review-loop"]
        self.assertEqual(loop["check"]["max_attempts"], 12)
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
            "coderabbit": "required",
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
        self.assertIn("never `--admin`", merge)
        self.assertIn("never a force", merge)
        self.assertIn("^[0-9a-f]{40}$", release)
        self.assertIn('DEPLOY_MODE" = "not-applicable', release)


class CommandContractTests(unittest.TestCase):
    SCRIPT = PACK_ROOT / "commands" / "delivery" / "start" / "run.sh"

    def run_command(
        self, *arguments: str, source_json: str = '[{"title":"Requested delivery"}]'
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            fake_gc = pathlib.Path(directory) / "gc"
            fake_gc.write_text(
                "#!/bin/sh\n"
                'if [ "$1" = "bd" ] && [ "$2" = "show" ]; then\n'
                f"  printf '%s\\n' '{source_json}'\n"
                "  exit 0\n"
                "fi\n"
                'printf "%s\\n" "$@"\n',
                encoding="utf-8",
            )
            fake_gc.chmod(0o755)
            environment = os.environ.copy()
            environment["GC_PACK_DIR"] = str(PACK_ROOT)
            environment["PATH"] = f"{directory}:{environment['PATH']}"
            return subprocess.run(
                ["sh", str(self.SCRIPT), *arguments],
                capture_output=True,
                text=True,
                env=environment,
            )

    def test_one_step_command_launches_terminal_formula_with_ergonomic_defaults(self) -> None:
        result = self.run_command("fi-123", "--rig", "finance")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = result.stdout.splitlines()
        self.assertEqual(args[:5], ["sling", "finance/gc.run-operator", "fi-123", "--on", "complete-delivery"])
        for value in (
            "source_bead_id=fi-123",
            "source_title=Requested delivery",
            "report_title=Requested delivery",
            "interaction_mode=autonomous",
            "review_mode=agent",
            "drain_policy=separate",
            "push=true",
            "open_pr=true",
        ):
            self.assertIn(value, args)

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
            source_json='[{"title":"Reject dirty checkout"}]',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        args = result.stdout.splitlines()
        self.assertIn("source_bead_id=fi-123", args)
        self.assertIn("source_title=Reject dirty checkout", args)
        self.assertIn("report_title=Reject dirty checkout", args)

    def test_missing_or_ambiguous_source_fails_before_dispatch(self) -> None:
        for source_json in ("[]", '[{"title":"one"},{"title":"two"}]', '[{}]'):
            with self.subTest(source_json=source_json):
                result = self.run_command("fi-123", "--rig=finance", source_json=source_json)
                self.assertEqual(result.returncode, 1)
                self.assertIn("cannot resolve source intent", result.stderr)
                self.assertNotIn("sling", result.stdout)

    def test_invalid_bead_is_rejected_before_dispatch(self) -> None:
        result = self.run_command("fi-123;echo-bad", "--rig", "finance")
        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid bead id", result.stderr)

    def test_missing_flag_value_has_actionable_error(self) -> None:
        result = self.run_command("fi-123", "--rig")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--rig requires a value", result.stderr)

    def test_report_publisher_command_uses_resolved_pack_root(self) -> None:
        wrapper = PACK_ROOT / "commands" / "report" / "publish" / "run.sh"
        text = wrapper.read_text(encoding="utf-8")
        self.assertIn('"$GC_PACK_DIR/assets/scripts/publish_delivery_report.py"', text)
        self.assertTrue((wrapper.parent / "help.md").is_file())


if __name__ == "__main__":
    unittest.main()
