from __future__ import annotations

import json
import os
import pathlib
import shlex
import subprocess
import sys
import tempfile
import tomllib
import unittest


PACK_ROOT = pathlib.Path(__file__).resolve().parents[1]
FORMULA_PATH = PACK_ROOT / "formulas" / "complete-delivery-pr-gate.formula.toml"
HANDOFF_PATH = "<artifact_root>/delivery/external-review-handoff.json"
LOCAL_GATES_SCRIPT = (
    PACK_ROOT / "assets" / "scripts" / "checks" / "delivery-local-gates.sh"
)
TERMINAL_GATE_SCRIPT = (
    PACK_ROOT / "assets" / "scripts" / "checks" / "delivery-pr-approved.sh"
)


class PrGateContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with FORMULA_PATH.open("rb") as formula_file:
            cls.formula = tomllib.load(formula_file)
        cls.templates = {template["id"]: template for template in cls.formula["template"]}

    def test_formula_routes_every_lane_to_a_declared_agent(self) -> None:
        declared_targets = {
            f"complete-delivery.{manifest.parent.name}"
            for manifest in (PACK_ROOT / "agents").glob("*/agent.toml")
        }
        run_targets = {
            template["metadata"]["gc.run_target"]
            for template in self.formula["template"]
        }
        loop = self.templates["{target}.external-review-loop"]
        run_targets.update(
            child["metadata"]["gc.run_target"] for child in loop["children"]
        )

        self.assertEqual(
            run_targets,
            {
                "complete-delivery.external-review-resolver",
                "complete-delivery.report-editor",
            },
        )
        self.assertTrue(run_targets <= declared_targets)

    def test_formula_routes_terminal_report_update_to_report_editor(self) -> None:
        terminal = self.templates["{target}"]

        self.assertEqual(
            terminal["metadata"]["gc.run_target"],
            "complete-delivery.report-editor",
        )

    def test_only_post_check_finalizer_may_publish_passing_report(self) -> None:
        workflows = PACK_ROOT / "assets" / "workflows" / "complete-delivery-pr-gate"
        precheck = (workflows / "{target}.report-external-review.md").read_text(
            encoding="utf-8"
        )
        loop = (workflows / "{target}.external-review-loop.md").read_text(
            encoding="utf-8"
        )
        finalizer = (workflows / "{target}.md").read_text(encoding="utf-8")

        self.assertIn("Keep\n`external-review` `active`", precheck)
        self.assertIn("never claim protected merge is next", precheck)
        self.assertIn("child report pre-terminal", loop)
        self.assertIn("leave `external-review` `active`", loop)
        self.assertIn("must not publish `passed` or a protected-merge next action", loop)
        self.assertIn("post-check `{target}.md` finalizer", loop)
        self.assertIn("sole\nauthority", finalizer)

    def test_formula_preserves_the_bounded_resolve_test_publish_handoff(self) -> None:
        loop = self.templates["{target}.external-review-loop"]
        self.assertEqual(loop["needs"], ["{target}.setup-external-review"])
        self.assertEqual(loop["check"]["max_attempts"], 12)
        self.assertEqual(
            loop["check"]["check"]["path"],
            ".gc/scripts/checks/delivery-pr-approved.sh",
        )

        children = {child["id"]: child for child in loop["children"]}
        self.assertEqual(
            children["{target}.resolve-findings"]["needs"],
            ["{target}.inspect-current-head"],
        )
        self.assertEqual(
            children["{target}.rerun-local-gates"]["needs"],
            ["{target}.resolve-findings"],
        )
        self.assertEqual(
            children["{target}.publish-fixes"]["needs"],
            ["{target}.rerun-local-gates"],
        )
        self.assertEqual(
            children["{target}.report-external-review"]["needs"],
            ["{target}.publish-fixes"],
        )
        for lane in (
            "{target}.resolve-findings",
            "{target}.rerun-local-gates",
            "{target}.publish-fixes",
        ):
            self.assertEqual(
                children[lane]["metadata"]["gc.continuation_group"],
                "complete-delivery-pr-fixes",
            )

        workflows = PACK_ROOT / "assets" / "workflows" / "complete-delivery-pr-gate"
        for filename in (
            "{target}.resolve-findings.md",
            "{target}.rerun-local-gates.md",
            "{target}.publish-fixes.md",
            "{target}.md",
        ):
            self.assertIn(HANDOFF_PATH, (workflows / filename).read_text(encoding="utf-8"))

    def test_resolver_prompt_keeps_publication_and_terminal_gate_in_their_lanes(self) -> None:
        prompt = (PACK_ROOT / "agents" / "external-review-resolver" / "prompt.template.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("Never push\n  or resolve a thread in this lane", prompt)
        self.assertIn("resolve valid mapped threads when `published_head == tested_commit`", prompt)
        self.assertIn("Only the Formula v2 `external-review-loop` terminal check", prompt)
        self.assertNotIn("After fixes are pushed and applicable review threads are resolved", prompt)

    def test_nonterminal_lanes_only_run_local_gates_and_require_exact_published_head(self) -> None:
        workflows = PACK_ROOT / "assets" / "workflows" / "complete-delivery-pr-gate"
        rerun_local_gates = (workflows / "{target}.rerun-local-gates.md").read_text(
            encoding="utf-8"
        )
        publish_fixes = (workflows / "{target}.publish-fixes.md").read_text(
            encoding="utf-8"
        )
        prompt = (PACK_ROOT / "agents" / "external-review-resolver" / "prompt.template.md").read_text(
            encoding="utf-8"
        )

        for content in (prompt, rerun_local_gates):
            self.assertIn("complete nonterminal local-gate set", content)
            for local_command in (
                "setup_command",
                "lint_command",
                "typecheck_command",
                "test_command",
                "build_command",
                "browser_test_command",
                "security_command",
                "extra_gate_command",
            ):
                self.assertIn(local_command, content)
            self.assertIn("delivery_gate.py", content)
            self.assertIn("delivery-pr-approved.sh", content)
            self.assertIn("inspect", content)
            self.assertIn("do not run it: record a blocker", content)
            self.assertIn("Never run such a gate before publication", content)

        self.assertIn("`published_head` is exactly equal to the\nartifact's `tested_commit`", publish_fixes)
        self.assertIn("Commit\ncontainment alone is not sufficient", publish_fixes)
        self.assertIn("Formula iteration must inspect and retest that exact refreshed head", prompt)

    def test_handoff_instructions_preserve_candidate_and_no_push_head_evidence(self) -> None:
        workflows = PACK_ROOT / "assets" / "workflows" / "complete-delivery-pr-gate"
        resolve_findings = (workflows / "{target}.resolve-findings.md").read_text(
            encoding="utf-8"
        )
        rerun_local_gates = (workflows / "{target}.rerun-local-gates.md").read_text(
            encoding="utf-8"
        )
        publish_fixes = (workflows / "{target}.publish-fixes.md").read_text(
            encoding="utf-8"
        )
        prompt = (PACK_ROOT / "agents" / "external-review-resolver" / "prompt.template.md").read_text(
            encoding="utf-8"
        )

        for content in (prompt, resolve_findings):
            self.assertIn("candidate_commit", content)
            self.assertIn("inspected_head", content)
            self.assertIn("no source", content)
            self.assertIn("final committed `HEAD`", content)
            self.assertIn("separate `fix_commit`", content)
        self.assertIn("candidate_commit", rerun_local_gates)
        self.assertIn("tested_commit", rerun_local_gates)
        self.assertIn("final committed `HEAD`", rerun_local_gates)
        self.assertIn("individual thread `fix_commit`", rerun_local_gates)
        for content in (prompt, publish_fixes):
            self.assertIn("no-push iteration", content)
            self.assertIn("published_head_matches_tested_commit", content)

    def test_handoff_instructions_require_immutable_test_and_publish_tree(self) -> None:
        workflows = PACK_ROOT / "assets" / "workflows" / "complete-delivery-pr-gate"
        rerun_local_gates = (workflows / "{target}.rerun-local-gates.md").read_text(
            encoding="utf-8"
        )
        publish_fixes = (workflows / "{target}.publish-fixes.md").read_text(
            encoding="utf-8"
        )

        for content in (rerun_local_gates, publish_fixes):
            self.assertIn("clean", content)
        self.assertIn("candidate_commit", rerun_local_gates)
        self.assertIn("remain clean", rerun_local_gates)
        self.assertIn("still equal `candidate_commit`", rerun_local_gates)
        self.assertIn("still exactly equal `tested_commit`", publish_fixes)
        self.assertIn("Push exactly `tested_commit`", publish_fixes)

    def test_non_actionable_thread_resolution_requires_published_disposition_evidence(self) -> None:
        workflows = PACK_ROOT / "assets" / "workflows" / "complete-delivery-pr-gate"
        publish_fixes = (workflows / "{target}.publish-fixes.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("invalid,\nsuperseded, or otherwise non-actionable", publish_fixes)
        self.assertIn("disposition evidence has been published", publish_fixes)
        self.assertIn("published_head_matches_tested_commit` is true", publish_fixes)

    def test_every_thread_resolution_instruction_requires_exact_head_equality(self) -> None:
        workflows = PACK_ROOT / "assets" / "workflows" / "complete-delivery-pr-gate"
        resolver_prompt = (
            PACK_ROOT / "agents" / "external-review-resolver" / "prompt.template.md"
        ).read_text(encoding="utf-8")
        resolution_instructions = (
            resolver_prompt,
            (workflows / "{target}.resolve-findings.md").read_text(encoding="utf-8"),
            (workflows / "{target}.publish-fixes.md").read_text(encoding="utf-8"),
            (workflows / "{target}.external-review-loop.md").read_text(encoding="utf-8"),
        )

        for instruction in resolution_instructions:
            normalized = " ".join(instruction.replace("`", "").split()).lower()
            self.assertTrue(
                "published_head == tested_commit" in normalized
                or "published_head is exactly equal to tested_commit" in normalized
                or (
                    "published_head is exactly equal to the artifact's tested_commit"
                    in normalized
                ),
                normalized,
            )
            self.assertRegex(normalized, r"containment[^.]*not sufficient")
            self.assertNotIn("contains every mapped fix commit", normalized)
            self.assertNotRegex(normalized, r"containment(?: alone)? is sufficient")

    def run_local_gates(self, command: str) -> tuple[subprocess.CompletedProcess[str], pathlib.Path]:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        root = pathlib.Path(temporary_directory.name)
        repository = root / "repository"
        repository.mkdir()
        bin_dir = root / "bin"
        bin_dir.mkdir()
        gc = bin_dir / "gc"
        gc.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$FAKE_GC_STEP_JSON"\n',
            encoding="utf-8",
        )
        gc.chmod(0o755)
        metadata = {
            "gc.var.allow_no_local_gates": "false",
            "gc.var.test_command": command,
        }
        environment = os.environ.copy()
        environment.update(
            {
                "GC_BEAD_ID": "step-1",
                "GC_WORK_DIR": str(repository),
                "FAKE_GC_STEP_JSON": json.dumps([{"metadata": metadata}]),
                "PATH": f"{bin_dir}:{environment['PATH']}",
            }
        )
        return (
            subprocess.run(
                ["bash", str(LOCAL_GATES_SCRIPT)],
                capture_output=True,
                text=True,
                env=environment,
            ),
            root,
        )

    def test_local_gates_reject_remote_approval_commands_before_bash_can_run_them(self) -> None:
        for terminal_command in (
            "delivery_gate.py",
            "delivery-pr-approved.sh",
            r"delivery_gat\e.py",
            r"delivery-pr-approv\ed.sh",
            "gh pr checks",
            "/usr/bin/gh api repos/example/repo/pulls/8",
            "coderabbit review",
            "./remote-approval-wrapper",
            "curl https://api.github.com/repos/example/repo/pulls/8",
            "delivery_gate." + "\\\n" + "py",
        ):
            with self.subTest(terminal_command=terminal_command):
                with tempfile.TemporaryDirectory() as directory:
                    marker = pathlib.Path(directory) / "side-effect"
                    command = f"{terminal_command}; touch {shlex.quote(str(marker))}"
                    result, _ = self.run_local_gates(command)

                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("terminal remote approval gate", result.stderr)
                    self.assertFalse(marker.exists())

    def test_local_gates_execute_an_allowed_local_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = pathlib.Path(directory) / "local-gate-ran"
            result, _ = self.run_local_gates(f"touch {shlex.quote(str(marker))}")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(marker.exists())

    def test_local_gates_allow_benign_coderabbit_named_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = pathlib.Path(directory) / "local-gate-ran"
            result, _ = self.run_local_gates(
                f"mkdir tests; touch tests/test_coderabbit.py; touch {shlex.quote(str(marker))}"
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(marker.exists())

    def run_terminal_gate(
        self, handoff: dict[str, object], report: dict[str, object] | None = None
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            artifact_root = root / "artifacts"
            handoff_path = artifact_root / "delivery" / "external-review-handoff.json"
            handoff_path.parent.mkdir(parents=True)
            handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
            bin_dir = root / "bin"
            bin_dir.mkdir()
            gc = bin_dir / "gc"
            metadata = {
                "delivery.repo": "owner/repo",
                "delivery.pr_number": "8",
                "gc.var.artifact_root": str(artifact_root),
            }
            gc.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$FAKE_GC_STEP_JSON"\n',
                encoding="utf-8",
            )
            gc.chmod(0o755)
            if report is not None:
                report_fixture = root / "delivery-gate-report.json"
                report_fixture.write_text(json.dumps(report), encoding="utf-8")
                python = bin_dir / "python3"
                python.write_text(
                    "#!/bin/sh\n"
                    "if [ \"${1##*/}\" = delivery_gate.py ]; then\n"
                    "  shift\n"
                    "  while [ \"$#\" -gt 0 ]; do\n"
                    "    if [ \"$1\" = --output ]; then\n"
                    "      cp \"$GATE_REPORT_FIXTURE\" \"$2\"\n"
                    "      exit 0\n"
                    "    fi\n"
                    "    shift\n"
                    "  done\n"
                    "  exit 2\n"
                    "fi\n"
                    "exec \"$REAL_PYTHON\" \"$@\"\n",
                    encoding="utf-8",
                )
                python.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                {
                    "GC_BEAD_ID": "step-1",
                    "FAKE_GC_STEP_JSON": json.dumps([{"metadata": metadata}]),
                    "PATH": f"{bin_dir}:{environment['PATH']}",
                    "REAL_PYTHON": sys.executable,
                }
            )
            if report is not None:
                environment["GATE_REPORT_FIXTURE"] = str(report_fixture)
            return subprocess.run(
                ["bash", str(TERMINAL_GATE_SCRIPT)],
                capture_output=True,
                text=True,
                env=environment,
            )

    def test_terminal_gate_rejects_blocked_or_skipped_local_gate_evidence(self) -> None:
        commit = "a" * 40
        for status in ("blocked", "skipped"):
            with self.subTest(status=status):
                result = self.run_terminal_gate(
                    {
                        "candidate_commit": commit,
                        "tested_commit": commit,
                        "published_head": commit,
                        "published_head_matches_tested_commit": True,
                        "local_gates": {
                            "status": status,
                            "tested_commit": commit,
                        },
                    }
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("proven passing local-gate evidence", result.stderr)

    def test_terminal_gate_rejects_unproven_tested_commit(self) -> None:
        result = self.run_terminal_gate(
            {
                "candidate_commit": "a" * 40,
                "tested_commit": "a" * 40,
                "published_head": "b" * 40,
                "published_head_matches_tested_commit": False,
                "local_gates": {
                    "status": "passed",
                    "tested_commit": "a" * 40,
                },
            }
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("proven passing local-gate evidence", result.stderr)

    def test_terminal_gate_rejects_missing_or_mismatched_candidate_commit(self) -> None:
        commit = "a" * 40
        for candidate_commit in (None, "b" * 40):
            with self.subTest(candidate_commit=candidate_commit):
                handoff: dict[str, object] = {
                    "tested_commit": commit,
                    "published_head": commit,
                    "published_head_matches_tested_commit": True,
                    "local_gates": {
                        "status": "passed",
                        "tested_commit": commit,
                    },
                }
                if candidate_commit is not None:
                    handoff["candidate_commit"] = candidate_commit
                result = self.run_terminal_gate(handoff)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("proven passing local-gate evidence", result.stderr)

    def test_terminal_gate_accepts_passing_delivery_gate_report(self) -> None:
        commit = "a" * 40
        result = self.run_terminal_gate(
            {
                "candidate_commit": commit,
                "tested_commit": commit,
                "published_head": commit,
                "published_head_matches_tested_commit": True,
                "local_gates": {"status": "passed", "tested_commit": commit},
            },
            {"passed": True, "head_sha": commit},
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_terminal_gate_rejects_failed_or_wrong_head_delivery_gate_report(self) -> None:
        commit = "a" * 40
        handoff = {
            "candidate_commit": commit,
            "tested_commit": commit,
            "published_head": commit,
            "published_head_matches_tested_commit": True,
            "local_gates": {"status": "passed", "tested_commit": commit},
        }
        for report in (
            {"passed": False, "head_sha": commit},
            {"passed": True, "head_sha": "b" * 40},
        ):
            with self.subTest(report=report):
                result = self.run_terminal_gate(handoff, report)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("delivery_gate.py", result.stderr)


if __name__ == "__main__":
    unittest.main()
