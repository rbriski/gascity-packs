from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import re
import stat
import subprocess
import tempfile
import tomllib
import types
import unittest


PACK_ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO_ROOT = PACK_ROOT.parent
FORMULA_DIR = PACK_ROOT / "formulas"
REPORT_SCRIPT = PACK_ROOT / "assets" / "scripts" / "delivery_report.py"
REPORT_CHECK = PACK_ROOT / "assets" / "scripts" / "checks" / "delivery-report-valid.sh"
REPORT_SPEC = importlib.util.spec_from_file_location("pack_contract_delivery_report", REPORT_SCRIPT)
assert REPORT_SPEC and REPORT_SPEC.loader
delivery_report = importlib.util.module_from_spec(REPORT_SPEC)
REPORT_SPEC.loader.exec_module(delivery_report)


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

    def test_formulas_require_supported_compiler(self) -> None:
        for formula in (self.delivery, self.gate):
            with self.subTest(formula=formula["formula"]):
                self.assertEqual(formula["requires"]["formula_compiler"], ">=2.0.0")
                self.assertNotIn("contract", formula)

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
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            rig = root / "rig"
            rig.mkdir()
            subprocess.run(["git", "init", "-q", str(rig)], check=True)
            fake_gc = pathlib.Path(directory) / "gc"
            fake_gh = pathlib.Path(directory) / "gh"
            config = {
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
            sink = root / "gc-args"
            calls = root / "gc-calls"
            fake_gc.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$*\" >> \"$FAKE_GC_CALLS\"\n"
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
                "[ \"${FAKE_GC_SLEEP_STAGE:-}\" != sling ] || exec sleep 5\n"
                "[ \"${FAKE_GC_FAIL_STAGE:-}\" != sling ] || { printf 'sling failed\\n' >&2; exit 25; }\n"
                "printf '%s\\n' \"$@\" | tee -a \"$FAKE_GC_SINK\"\n",
                encoding="utf-8",
            )
            fake_gc.chmod(0o755)
            fake_gh.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$*\" >> \"$FAKE_GH_CALLS\"\n"
                "[ \"${FAKE_GH_AUTHENTICATED:-true}\" = true ] || exit 1\n"
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
            environment["FAKE_SOURCE_JSON"] = (
                source_json
                if source_json is not None
                else json.dumps([{"id": arguments[0], "title": "Requested delivery"}])
            )
            environment["FAKE_GC_SINK"] = str(sink)
            environment["FAKE_GC_CALLS"] = str(calls)
            environment["FAKE_GH_CALLS"] = str(root / "gh-calls")
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
            gh_calls = root / "gh-calls"
            result.gh_calls = gh_calls.read_text(encoding="utf-8").splitlines() if gh_calls.exists() else []
            result.materialized = {
                "delivery_preflight": (rig / ".gc/scripts/checks/delivery-preflight.sh").is_file(),
                "build_artifact": (rig / ".gc/scripts/checks/build-artifact-valid.sh").is_file(),
                "validator": (rig / ".gc/scripts/validate_build_artifact.py").is_file(),
                "schema": (rig / "schemas/build/requirements.v1.yaml").is_file(),
            }
            result.materialized_paths = sorted(
                str(path.relative_to(rig))
                for root in (rig / ".gc", rig / "schemas" / "build")
                if root.exists()
                for path in root.rglob("*")
                if path.is_file()
            )
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
            "launcher_github_preflight=github-v1",
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
            "validator": True,
            "schema": True,
        })
        self.assertEqual(result.materialized_paths, [
            ".gc/complete-delivery-assets.json",
            ".gc/scripts/checks/build-artifact-valid.sh",
            ".gc/scripts/checks/delivery-common.sh",
            ".gc/scripts/checks/delivery-external-review-deadline.sh",
            ".gc/scripts/checks/delivery-local-gates.sh",
            ".gc/scripts/checks/delivery-merged.sh",
            ".gc/scripts/checks/delivery-pr-approved.sh",
            ".gc/scripts/checks/delivery-pr-open.sh",
            ".gc/scripts/checks/delivery-preflight.sh",
            ".gc/scripts/checks/delivery-release-verified.sh",
            ".gc/scripts/checks/delivery-report-green.sh",
            ".gc/scripts/checks/delivery-report-valid.sh",
            ".gc/scripts/checks/delivery-source-artifact-valid.sh",
            ".gc/scripts/delivery_gate.py",
            ".gc/scripts/delivery_report.py",
            ".gc/scripts/validate_build_artifact.py",
            "schemas/build/decomposition.v1.yaml",
            "schemas/build/final-report.v1.yaml",
            "schemas/build/implementation-summary.v1.yaml",
            "schemas/build/plan.v1.yaml",
            "schemas/build/requirements.v1.yaml",
            "schemas/build/review.v1.yaml",
        ])
        self.assertEqual(
            sorted(result.manifest["assets"]),
            [path for path in result.materialized_paths if path != ".gc/complete-delivery-assets.json"],
        )
        self.assertEqual(
            result.gh_calls,
            [
                "auth status",
                "repo view --json nameWithOwner",
                "api repos/{owner}/{repo}/branches/main/protection --silent",
            ],
        )

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
            environment_updates={"FAKE_GH_AUTHENTICATED": "false"},
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("gh authentication check failed", result.stderr)
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
