"""Complete Delivery contract tests require Python 3.11+ (stdlib tomllib)."""

from __future__ import annotations

import json
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from datetime import datetime, timedelta, timezone


PACK_ROOT = pathlib.Path(__file__).resolve().parents[1]
FORMULA_PATH = PACK_ROOT / "formulas" / "complete-delivery-pr-gate.formula.toml"
HANDOFF_PATH = "<artifact_root>/delivery/external-review-handoff.json"
LOCAL_GATES_SCRIPT = (
    PACK_ROOT / "assets" / "scripts" / "checks" / "delivery-local-gates.sh"
)
TERMINAL_GATE_SCRIPT = (
    PACK_ROOT / "assets" / "scripts" / "checks" / "delivery-pr-approved.sh"
)
DEADLINE_SCRIPT = (
    PACK_ROOT / "assets" / "scripts" / "checks" / "delivery-external-review-deadline.sh"
)


class PrGateContractTests(unittest.TestCase):
    def run_deadline(
        self,
        metadata: dict[str, str],
        history: list[dict],
        mode: str | None,
        extra_env: dict[str, str] | None = None,
        without_flock: bool = False,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            state = root / "state.json"
            history_path = root / "history.json"
            state.write_text(json.dumps([{"metadata": metadata}]), encoding="utf-8")
            history_path.write_text(json.dumps(history), encoding="utf-8")
            gc = root / "gc"
            gc.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "state = os.environ['FAKE_GC_STATE']\n"
                "history = os.environ['FAKE_GC_HISTORY']\n"
                "args = sys.argv[1:]\n"
                "subcommand = args[1] if len(args) > 1 and args[0] == 'bd' else ''\n"
                "if subcommand == 'show':\n"
                " print(open(state).read())\n"
                "elif subcommand == 'history':\n"
                " import time\n"
                " if os.environ.get('FAKE_GC_HANG_SUBCOMMAND') == subcommand: time.sleep(2)\n"
                " print(open(history).read())\n"
                "elif subcommand == 'update':\n"
                " data = json.load(open(state)); meta = data[0]['metadata']\n"
                " for index, value in enumerate(args):\n"
                "  if value == '--set-metadata':\n"
                "   key, value = args[index + 1].split('=', 1); meta[key] = value\n"
                " open(state, 'w').write(json.dumps(data))\n"
                " entries = json.load(open(history)); entries.insert(0, {'Issue': data[0]})\n"
                " open(history, 'w').write(json.dumps(entries))\n"
                "else:\n"
                " raise SystemExit('unexpected gc invocation: ' + repr(args))\n",
                encoding="utf-8",
            )
            gc.chmod(0o755)
            env = os.environ.copy()
            env.update({
                "GC_BEAD_ID": "root-1",
                "GC_WORK_DIR": str(root),
                "FAKE_GC_STATE": str(state),
                "FAKE_GC_HISTORY": str(history_path),
                "PATH": f"{root}:{env['PATH']}",
            })
            if extra_env:
                env.update(extra_env)
            if without_flock:
                commands = root / "commands-without-flock"
                commands.mkdir()
                for name in ("cat", "date", "dirname", "mkdir", "mktemp", "python3", "rm", "sleep", "timeout"):
                    target = shutil.which(name, path=env["PATH"])
                    assert target is not None, name
                    (commands / name).symlink_to(target)
                env["PATH"] = f"{root}:{commands}"
            command = ["/bin/bash", str(DEADLINE_SCRIPT)]
            if mode is not None:
                command.append(mode)
            result = subprocess.run(command, capture_output=True, text=True, env=env)
            return result, json.loads(state.read_text(encoding="utf-8")), json.loads(history_path.read_text(encoding="utf-8"))

    def test_external_review_deadline_is_first_write_immutable_and_fail_closed(self) -> None:
        before = datetime.now(timezone.utc).replace(microsecond=0)
        initialized, state, history = self.run_deadline({}, [], "--initialize")
        after = datetime.now(timezone.utc).replace(microsecond=0)
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        metadata = state[0]["metadata"]
        started = datetime.strptime(
            metadata["delivery.external_review_started_at"], "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc)
        deadline = datetime.strptime(
            metadata["delivery.external_review_deadline"], "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc)
        self.assertLessEqual(before, started)
        self.assertLessEqual(started, after)
        self.assertEqual(deadline, started + timedelta(hours=2))
        self.assertEqual(len(history), 1)

        resumed, state, resumed_history = self.run_deadline(metadata, history, "--initialize")
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(state[0]["metadata"], metadata)
        self.assertEqual(resumed_history, history)

        for label, changed_metadata, changed_history, expected in (
            ("missing", {}, [], "missing"),
            ("malformed", {"delivery.external_review_started_at": metadata["delivery.external_review_started_at"], "delivery.external_review_deadline": "tomorrow"}, [], "canonical UTC"),
            (
                "moved-forward",
                {
                    **metadata,
                    "delivery.external_review_deadline": (
                        deadline + timedelta(minutes=30)
                    ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
                [{"Issue": {"metadata": metadata}}],
                "no later than two hours",
            ),
            (
                "reset",
                {
                    **metadata,
                    "delivery.external_review_deadline": (
                        deadline - timedelta(minutes=30)
                    ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
                [{"Issue": {"metadata": metadata}}],
                "does not match immutable first entry",
            ),
        ):
            with self.subTest(label=label):
                result, _, _ = self.run_deadline(changed_metadata, changed_history, "--validate")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stderr)

    def test_forged_clock_override_cannot_rescue_an_expired_deadline(self) -> None:
        expired = {
            "delivery.external_review_started_at": "2000-01-01T00:00:00Z",
            "delivery.external_review_deadline": "2000-01-01T02:00:00Z",
        }
        result, _, _ = self.run_deadline(
            expired,
            [{"Issue": {"metadata": expired}}],
            "--validate",
            {"DELIVERY_NOW_UTC": "1970-01-01T00:00:00Z"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("expired", result.stderr)
        self.assertNotIn(
            'NOW="${DELIVERY_NOW_UTC', DEADLINE_SCRIPT.read_text(encoding="utf-8")
        )

    def test_deadline_defaults_to_validation_and_bounds_discovery_reads(self) -> None:
        defaulted, state, history = self.run_deadline({}, [], None)
        self.assertNotEqual(defaulted.returncode, 0)
        self.assertIn("missing", defaulted.stderr)
        self.assertEqual(state[0]["metadata"], {})
        self.assertEqual(history, [])

        timed_out, _, _ = self.run_deadline(
            {}, [], "--initialize", {"FAKE_GC_HANG_SUBCOMMAND": "history"}
        )
        self.assertNotEqual(timed_out.returncode, 0)
        self.assertIn("during deadline discovery", timed_out.stderr)
        self.assertIn('timeout --signal=KILL 1s gc bd history', DEADLINE_SCRIPT.read_text(encoding="utf-8"))

    def test_only_first_deadline_initialization_requires_flock(self) -> None:
        initialized, _state, history = self.run_deadline({}, [], "--initialize")
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        metadata = _state[0]["metadata"]

        validated, _, _ = self.run_deadline(
            metadata, history, "--validate", without_flock=True
        )
        self.assertEqual(validated.returncode, 0, validated.stderr)

        unavailable, _, _ = self.run_deadline(
            {}, [], "--initialize", without_flock=True
        )
        self.assertNotEqual(unavailable.returncode, 0)
        self.assertIn("flock is required", unavailable.stderr)

    def test_deadline_recomputes_real_clock_after_internal_reads(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            state = root / "state.json"
            history_path = root / "history.json"
            state.write_text(json.dumps([{"metadata": {}}]), encoding="utf-8")
            history_path.write_text("[]", encoding="utf-8")
            gc = root / "gc"
            gc.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, pathlib, sys\n"
                "state_path = pathlib.Path(os.environ['FAKE_GC_STATE'])\n"
                "history_path = pathlib.Path(os.environ['FAKE_GC_HISTORY'])\n"
                "if sys.argv[2] == 'show': print(state_path.read_text())\n"
                "elif sys.argv[2] == 'history': print(history_path.read_text())\n"
                "elif sys.argv[2] == 'update':\n"
                " data = json.loads(state_path.read_text()); metadata = data[0]['metadata']\n"
                " for i, value in enumerate(sys.argv):\n"
                "  if value == '--set-metadata': key, value = sys.argv[i + 1].split('=', 1); metadata[key] = value\n"
                " state_path.write_text(json.dumps(data))\n"
                " history = json.loads(history_path.read_text()); history.insert(0, {'Issue': data[0]}); history_path.write_text(json.dumps(history))\n"
                "else: raise SystemExit('unexpected gc invocation: ' + repr(sys.argv))\n",
                encoding="utf-8",
            )
            gc.chmod(0o755)
            date = root / "date"
            date.write_text(
                "#!/usr/bin/env python3\n"
                "import os, pathlib\n"
                "counter = pathlib.Path(os.environ['FAKE_DATE_COUNTER'])\n"
                "index = int(counter.read_text() if counter.exists() else '0')\n"
                "values = os.environ['FAKE_DATE_VALUES'].split(',')\n"
                "counter.write_text(str(index + 1))\n"
                "print(values[min(index, len(values) - 1)])\n",
                encoding="utf-8",
            )
            date.chmod(0o755)
            environment = os.environ.copy()
            environment.update({
                "GC_BEAD_ID": "root-1",
                "GC_WORK_DIR": str(root),
                "FAKE_GC_STATE": str(state),
                "FAKE_GC_HISTORY": str(history_path),
                "FAKE_DATE_COUNTER": str(root / "date-count"),
                "FAKE_DATE_VALUES": ",".join((
                    now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    (now + timedelta(hours=2, seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                )),
                "PATH": f"{root}:{environment['PATH']}",
            })
            result = subprocess.run(
                ["bash", str(DEADLINE_SCRIPT), "--initialize"],
                capture_output=True,
                text=True,
                env=environment,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("expired", result.stderr)

    def test_deadline_initialization_uses_one_pair_for_concurrent_callers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            state = root / "state.json"
            history_path = root / "history.json"
            state.write_text(json.dumps([{"metadata": {}}]), encoding="utf-8")
            history_path.write_text("[]", encoding="utf-8")
            gc = root / "gc"
            gc.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, pathlib, sys, time\n"
                "state_path = pathlib.Path(os.environ['FAKE_GC_STATE'])\n"
                "history_path = pathlib.Path(os.environ['FAKE_GC_HISTORY'])\n"
                "args = sys.argv[1:]\n"
                "if args[1] == 'show': print(state_path.read_text())\n"
                "elif args[1] == 'history': print(history_path.read_text())\n"
                "elif args[1] == 'update':\n"
                " if os.environ.get('FAKE_GC_UPDATE_DELAY'): time.sleep(float(os.environ['FAKE_GC_UPDATE_DELAY']))\n"
                " data = json.loads(state_path.read_text()); metadata = data[0]['metadata']\n"
                " for i, value in enumerate(args):\n"
                "  if value == '--set-metadata': key, value = args[i + 1].split('=', 1); metadata[key] = value\n"
                " state_path.write_text(json.dumps(data))\n"
                " history = json.loads(history_path.read_text()); history.insert(0, {'Issue': data[0]}); history_path.write_text(json.dumps(history))\n"
                "else: raise SystemExit('unexpected gc invocation: ' + repr(args))\n",
                encoding="utf-8",
            )
            gc.chmod(0o755)
            environment = os.environ.copy()
            environment.update({
                "GC_BEAD_ID": "root-1",
                "GC_WORK_DIR": str(root),
                "FAKE_GC_STATE": str(state),
                "FAKE_GC_HISTORY": str(history_path),
                "FAKE_GC_UPDATE_DELAY": "2",
                "PATH": f"{root}:{environment['PATH']}",
            })
            first = subprocess.Popen(["bash", str(DEADLINE_SCRIPT), "--initialize"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment)
            second = subprocess.Popen(["bash", str(DEADLINE_SCRIPT), "--initialize"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment)
            first_stdout, first_stderr = first.communicate(timeout=10)
            second_stdout, second_stderr = second.communicate(timeout=10)
            self.assertEqual(first.returncode, 0, first_stderr)
            self.assertEqual(second.returncode, 0, second_stderr)
            self.assertIn("deadline valid", first_stdout)
            self.assertIn("deadline valid", second_stdout)
            metadata = json.loads(state.read_text(encoding="utf-8"))[0]["metadata"]
            self.assertEqual(len(json.loads(history_path.read_text(encoding="utf-8"))), 1)
            self.assertEqual(
                metadata["delivery.external_review_deadline"],
                (datetime.strptime(metadata["delivery.external_review_started_at"], "%Y-%m-%dT%H:%M:%SZ") + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
            self.assertIn("DEADLINE_LOCK_WAIT=30", DEADLINE_SCRIPT.read_text(encoding="utf-8"))

    def test_external_review_actions_and_terminal_gate_require_deadline_validation(self) -> None:
        workflows = PACK_ROOT / "assets" / "workflows" / "complete-delivery-pr-gate"
        for filename in (
            "{target}.inspect-current-head.md",
            "{target}.resolve-findings.md",
            "{target}.rerun-local-gates.md",
            "{target}.publish-fixes.md",
            "{target}.report-external-review.md",
            "{target}.external-review-loop.md",
            "{target}.md",
        ):
            with self.subTest(filename=filename):
                self.assertIn(".gc/scripts/checks/delivery-external-review-deadline.sh --validate", (workflows / filename).read_text(encoding="utf-8"))
        self.assertIn(".gc/scripts/checks/delivery-external-review-deadline.sh --initialize", (workflows / "{target}.setup-external-review.md").read_text(encoding="utf-8"))
        self.assertIn("delivery-external-review-deadline.sh\" --validate", TERMINAL_GATE_SCRIPT.read_text(encoding="utf-8"))
        formula = tomllib.loads(FORMULA_PATH.read_text(encoding="utf-8"))
        setup = next(template for template in formula["template"] if template["id"] == "{target}.setup-external-review")
        self.assertEqual(setup["check"]["check"]["path"], ".gc/scripts/checks/delivery-external-review-deadline.sh")
        self.assertNotIn("args", setup["check"]["check"])
        loop = (workflows / "{target}.external-review-loop.md").read_text(encoding="utf-8")
        self.assertIn("Immediately before every source-editing repair mutation and every commit", loop)

        required_deadline_guards = (
            "Immediately before every source-editing repair mutation",
            ".gc/scripts/checks/delivery-external-review-deadline.sh --validate",
            "Separately, immediately before each `git commit`",
            "write only blocker-only handoff evidence",
            "perform neither the source-editing mutation nor any `git commit`",
        )
        resolve_findings = (workflows / "{target}.resolve-findings.md").read_text(
            encoding="utf-8"
        )
        for required_guard in required_deadline_guards:
            with self.subTest(resolve_findings_guard=required_guard):
                self.assert_prose_contains(resolve_findings, required_guard)

        post_commit_guards = (
            "Immediately after the final repair commit",
            "immediately before persisting successful handoff evidence",
            "either post-commit validation fails",
            "replace the handoff with blocker-only state",
            "persist no successful identity, candidate, disposition, fix-commit, tested-commit, publication, or equality evidence",
            "make no further source-editing mutation or commit",
        )
        for required_guard in post_commit_guards:
            with self.subTest(resolve_findings_post_commit_guard=required_guard):
                self.assert_prose_contains(resolve_findings, required_guard)
        resolver = (
            PACK_ROOT / "agents" / "external-review-resolver" / "prompt.template.md"
        ).read_text(encoding="utf-8")
        for required_guard in required_deadline_guards:
            with self.subTest(resolver_guard=required_guard):
                self.assert_prose_contains(resolver, required_guard)
    @classmethod
    def setUpClass(cls) -> None:
        with FORMULA_PATH.open("rb") as formula_file:
            cls.formula = tomllib.load(formula_file)
        cls.templates = {template["id"]: template for template in cls.formula["template"]}

    def assert_prose_contains(self, text: str, expected: str) -> None:
        self.assertIn(" ".join(expected.split()), " ".join(text.split()))

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

    def test_only_outer_report_green_may_authorize_passing_report(self) -> None:
        workflows = PACK_ROOT / "assets" / "workflows" / "complete-delivery-pr-gate"
        precheck = (workflows / "{target}.report-external-review.md").read_text(
            encoding="utf-8"
        )
        loop = (workflows / "{target}.external-review-loop.md").read_text(
            encoding="utf-8"
        )
        finalizer = (workflows / "{target}.md").read_text(encoding="utf-8")
        outer_report = (
            PACK_ROOT / "assets" / "workflows" / "complete-delivery" / "report-green.md"
        ).read_text(encoding="utf-8")

        self.assert_prose_contains(precheck, "Keep `external-review` `active`")
        self.assert_prose_contains(
            precheck,
            "set the immediate next action to the `external-review-loop` terminal mechanical check. "
            "Keep `external-review` active",
        )
        self.assert_prose_contains(precheck, "proven publication whose canonical full-SHA `published_head` exactly equals the updated workflow-root `delivery.head_sha`")
        self.assert_prose_contains(precheck, "prior-inspected-head `pr-gate.json` is not current-head evidence")
        self.assert_prose_contains(precheck, "root-head-mismatched other than the exact proven publication transition above")
        self.assert_prose_contains(precheck, "may publish only configured active or blocked status")
        self.assert_prose_contains(precheck, "must never publish `external-review` as `passed` or a protected-merge next action")
        self.assert_prose_contains(precheck, "Immediately before running `report_publish_command`")
        self.assert_prose_contains(precheck, "If the second validation or publication fails")
        self.assert_prose_contains(precheck, "revert the just-written active-state mutation to an explicit non-pass blocker")
        self.assertIn("child report pre-terminal", loop)
        self.assertIn("leave `external-review` `active`", loop)
        self.assertIn("must not publish `passed` or a protected-merge next action", loop)
        self.assert_prose_contains(loop, "does not authorize the passing report, protected merge, or report publication")
        self.assert_prose_contains(finalizer, "evidence-only")
        self.assert_prose_contains(finalizer, "must never mark `external-review` as `passed`")
        self.assert_prose_contains(finalizer, "must never mark `external-review` as `passed`, set a protected-merge next action, or invoke `report_publish_command`")
        self.assert_prose_contains(finalizer, "Top-level `complete-delivery/report-green.md`")
        self.assert_prose_contains(outer_report, "sole outer report authority")
        self.assert_prose_contains(outer_report, "report_publish_command")
        self.assert_prose_contains(outer_report, "require it to succeed")
        self.assert_prose_contains(outer_report, "leaves the report in its prior non-passing state")
        self.assert_prose_contains(outer_report, "Do not attempt a compensating revert")

    def test_outer_formula_orders_external_review_before_report_green_and_merge(self) -> None:
        outer_formula_path = PACK_ROOT / "formulas" / "complete-delivery.formula.toml"
        with outer_formula_path.open("rb") as formula_file:
            outer_formula = tomllib.load(formula_file)
        steps = {step["id"]: step for step in outer_formula["steps"]}

        self.assertEqual(steps["report-green"]["needs"], ["external-review"])
        self.assertEqual(
            steps["report-green"]["check"]["check"]["path"],
            ".gc/scripts/checks/delivery-report-green.sh",
        )
        self.assertEqual(steps["report-green"]["check"]["check"]["timeout"], "1m")
        self.assertEqual(steps["merge"]["needs"], ["report-green"])

    def test_terminal_publication_identity_and_deadline_guards_are_fail_closed(self) -> None:
        workflows = PACK_ROOT / "assets" / "workflows" / "complete-delivery-pr-gate"
        publish_fixes = (workflows / "{target}.publish-fixes.md").read_text(
            encoding="utf-8"
        )
        rerun_local_gates = (workflows / "{target}.rerun-local-gates.md").read_text(
            encoding="utf-8"
        )
        precheck = (workflows / "{target}.report-external-review.md").read_text(
            encoding="utf-8"
        )

        self.assert_prose_contains(
            publish_fixes,
            "Immediately re-read and exactly verify every just-written workflow-root and handoff identity field before any thread resolution",
        )
        self.assert_prose_contains(publish_fixes, "persistence that is failed, partial, malformed, or head-mismatched is a distinct publication blocker")
        self.assert_prose_contains(publish_fixes, "clear all authority fields")
        self.assert_prose_contains(publish_fixes, "keep every mapped thread open")
        for guard in (
            "Immediately before every source edit",
            "every local-gate-script invocation",
            "every commit",
        ):
            with self.subTest(guard=guard):
                self.assert_prose_contains(rerun_local_gates, guard)
        self.assert_prose_contains(precheck, "Immediately before the living-report mutation")
        self.assert_prose_contains(precheck, "Immediately before running `report_publish_command`")
        self.assert_prose_contains(precheck, "revert the just-written active-state mutation")
        self.assert_prose_contains(
            precheck,
            "canonical `tested_commit` is valid, whose `local_gates` status is `passed`, and whose `published_head_matches_tested_commit` is true",
        )

        finalizer = (workflows / "{target}.md").read_text(encoding="utf-8")
        self.assert_prose_contains(finalizer, "Immediately before recording finalizer pass evidence")
        self.assert_prose_contains(finalizer, "terminal check and immediate revalidation have passed")

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

        self.assert_prose_contains(prompt, "Never push or resolve a thread in this lane")
        self.assertIn("resolve only a current mapped thread", prompt)
        self.assertTrue("Only the Formula v2 `external-review-loop` terminal check" in prompt and all(" ".join(term.split()) in " ".join(prompt.split()) for term in ("semantic `gc.complete-delivery.pr-gate.v1` identity", "`schema`", "`repo`", "`pr_number`", "`passed`", "`true` only", '`state: "passed"', "`false` only", '`state: "blocked"', "`required_checks` as a list", "`coderabbit` as an object", "`unresolved_threads` as a list", "`human_change_requests` as a list", "`blockers` as a list", "blocker-only state")))
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

        for content, inspection_instruction in (
            (prompt, "Before invoking that script, inspect the configured commands."),
            (rerun_local_gates, "Before invoking it, inspect those configured commands."),
        ):
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
            self.assert_prose_contains(content, inspection_instruction)
            self.assertIn("do not run it: record a blocker", content)
            self.assertIn("Never run such a gate before publication", content)

        self.assert_prose_contains(
            publish_fixes, "`published_head == tested_commit`"
        )
        self.assert_prose_contains(publish_fixes, "Commit containment alone is not sufficient")
        self.assertIn("Formula iteration to inspect and retest that exact refreshed head", prompt)

    def test_publication_refresh_failure_requires_a_new_full_sha_before_more_work(self) -> None:
        workflows = PACK_ROOT / "assets" / "workflows" / "complete-delivery-pr-gate"
        publish_fixes = (workflows / "{target}.publish-fixes.md").read_text(encoding="utf-8")
        prompt = (PACK_ROOT / "agents" / "external-review-resolver" / "prompt.template.md").read_text(
            encoding="utf-8"
        )

        for content in (publish_fixes, prompt):
            normalized = " ".join(content.split())
            self.assertIn("record a publication failure", normalized)
            self.assertIn("keep every mapped thread open", normalized)
            self.assertIn("do not record passing publication evidence", normalized)
            self.assertIn("reacquire a current PR head that is a full SHA", normalized)
            self.assertIn("successful refresh returns a different full-SHA", normalized)

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
            self.assertTrue(all(term in content for term in ("`fix_commit`", "clean worktree", "HEAD == inspected_head", "blocker-only")))
        for content in (prompt, publish_fixes):
            normalized = " ".join(content.split())
            self.assertTrue(all(term in normalized for term in ("shared repository-scoped", "resolveReviewThread")))
            self.assertTrue("between that final check and all" in normalized or "after that final head check and before all" in normalized)
        self.assertTrue(all(term in resolve_findings for term in ("replace the entire handoff object", "only blocker state")))
        self.assertTrue(all(term in content for content in (prompt, rerun_local_gates) for term in ("at most three complete regression-repair-and-rerun", "fourth repair", "replace the entire handoff", "blocker-only retry-exhausted evidence", "no authority fields", "close with a non-pass outcome")))
        self.assertTrue(all(term in rerun_local_gates for term in ("candidate_commit", "tested_commit", "final committed `HEAD`", "individual thread `fix_commit`", "full local-gate sequence passes")))
        for content in (prompt, publish_fixes):
            self.assert_prose_contains(content, "no empty commit/push")
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
        self.assertIn("`HEAD == tested_commit`", publish_fixes)
        self.assertIn("Push exactly `tested_commit`", publish_fixes)
        self.assert_prose_contains(publish_fixes, "After acquiring it, recheck the clean tree and canonical `HEAD == tested_commit` while holding it")
        self.assert_prose_contains(publish_fixes, "unavailable lock, dirty tree, or mismatch fails closed before push, refresh, or resolution")

    def test_non_actionable_thread_resolution_requires_published_disposition_evidence(self) -> None:
        workflows = PACK_ROOT / "assets" / "workflows" / "complete-delivery-pr-gate"
        publish_fixes = (workflows / "{target}.publish-fixes.md").read_text(
            encoding="utf-8"
        )

        self.assert_prose_contains(publish_fixes, "invalid, superseded, or otherwise non-actionable")
        self.assertIn("disposition evidence was published", publish_fixes)
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

    def run_local_gates(
        self, command: str, *, session_id: str = "", empty_parser_output: bool = False
    ) -> tuple[subprocess.CompletedProcess[str], pathlib.Path]:
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
        if empty_parser_output:
            python = bin_dir / "python3"
            python.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = \"-\" ]; then exit 0; fi\n"
                "exec \"$REAL_PYTHON\" \"$@\"\n",
                encoding="utf-8",
            )
            python.chmod(0o755)
        metadata = {
            "gc.var.allow_no_local_gates": "false",
            "gc.var.test_command": command,
        }
        environment = os.environ.copy()
        environment.update(
            {
                "GC_BEAD_ID": "step-1",
                "GC_WORK_DIR": str(repository),
                "GC_SESSION_ID": session_id,
                "FAKE_GC_STEP_JSON": json.dumps([{"metadata": metadata}]),
                "PATH": f"{bin_dir}:{environment['PATH']}",
                "REAL_PYTHON": sys.executable,
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

    def test_empty_local_gate_parser_output_fails_closed_under_nounset(self) -> None:
        result, _ = self.run_local_gates("true", empty_parser_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("local gate command could not be parsed safely: true", result.stderr)

    def test_local_gates_reject_remote_approval_commands_before_bash_can_run_them(self) -> None:
        policy_commands = (
            "delivery_gate.py", "delivery-pr-approved.sh", r"delivery_gat\e.py",
            r"delivery-pr-approv\ed.sh", "gh pr checks", "/usr/bin/gh api repos/example/repo/pulls/8",
            "coderabbit review", "./remote-approval-wrapper", "curl https://api.github.com/repos/example/repo/pulls/8",
            '"delivery_gate.py"', "'delivery_gate.py'", '"gh" pr checks', "'gh' pr checks",
            "timeout 60 gh pr checks", "nice gh pr checks", "xargs -a /dev/null coderabbit review",
            "python3 /tmp/gh", "python3 /tmp/remote-approval-wrapper",
            "python3 -c \"import subprocess; subprocess.run(['gh', 'pr', 'checks'])\"",
            "node -e \"require('child_process').execSync('gh pr checks')\"",
            "perl -e \"system 'gh', 'pr', 'checks'\"", "ruby -e \"system 'gh', 'pr', 'checks'\"",
            "awk --execute \"system(\\\"gh pr checks\\\")\"",
        )
        syntax_commands = (
            "delivery_gate." + "\\\n" + "py", chr(96) + "delivery_gate.py" + chr(96),
            "{delivery_gate.py,x}", "{gh,x} pr checks", "delivery_gate.py</dev/null",
            "delivery_gate.py>/dev/null", "gh>/dev/null pr checks",
        )
        for terminal_command in policy_commands:
            with self.subTest(terminal_command=terminal_command), tempfile.TemporaryDirectory() as directory:
                marker = pathlib.Path(directory) / "side-effect"
                command = f"{terminal_command} {shlex.quote(str(marker))}"
                result, _ = self.run_local_gates(command)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    "invokes a terminal remote approval gate or provider command",
                    result.stderr,
                )
                self.assertFalse(marker.exists())

        for terminal_command in syntax_commands:
            with self.subTest(terminal_command=terminal_command):
                with tempfile.TemporaryDirectory() as directory:
                    marker = pathlib.Path(directory) / "side-effect"
                    command = f"{terminal_command}; touch {shlex.quote(str(marker))}"
                    result, _ = self.run_local_gates(command)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("local gate command could not be parsed safely", result.stderr)
                    self.assertFalse(marker.exists())

    def test_local_gates_reject_process_wrappers_before_execution(self) -> None:
        for wrapper in ("timeout 60", "nice", "ionice", "setsid", "xargs -a /dev/null"):
            with self.subTest(wrapper=wrapper), tempfile.TemporaryDirectory() as directory:
                marker = pathlib.Path(directory) / "side-effect"
                result, _ = self.run_local_gates(
                    f"{wrapper} touch {shlex.quote(str(marker))}"
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("terminal remote approval gate", result.stderr)
                self.assertFalse(marker.exists())

    def test_local_gates_reject_privilege_user_switch_wrappers_before_execution(self) -> None:
        direct_wrappers = ("doas", "pkexec", "runuser", "setpriv", "su", "sudo")
        nested_command_wrappers = (
            ("su -c", "gh pr checks"),
            ("runuser -u nobody --", "delivery-pr-approved.sh"),
        )
        for wrapper in direct_wrappers:
            with self.subTest(wrapper=wrapper), tempfile.TemporaryDirectory() as directory:
                marker = pathlib.Path(directory) / "side-effect"
                result, _ = self.run_local_gates(
                    f"{wrapper} touch {shlex.quote(str(marker))}"
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("terminal remote approval gate", result.stderr)
                self.assertFalse(marker.exists())

        for wrapper, nested_command in nested_command_wrappers:
            with (
                self.subTest(wrapper=wrapper, nested_command=nested_command),
                tempfile.TemporaryDirectory() as directory,
            ):
                marker = pathlib.Path(directory) / "side-effect"
                result, _ = self.run_local_gates(
                    f"{wrapper} {shlex.quote(f'{nested_command}; touch {marker}')}"
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("terminal remote approval gate", result.stderr)
                self.assertFalse(marker.exists())

    def test_local_gates_reject_terminal_scripts_in_interpreter_arguments_before_execution(self) -> None:
        cases = (("python3", "delivery_gate.py", "import pathlib\npathlib.Path({marker!r}).touch()\n"), ("bash", "delivery-pr-approved.sh", "#!/usr/bin/env bash\ntouch {marker}\n"))
        for interpreter, script_name, source in cases:
            with self.subTest(interpreter=interpreter, script_name=script_name):
                with tempfile.TemporaryDirectory() as directory:
                    marker = pathlib.Path(directory) / "script-ran"
                    script = pathlib.Path(directory) / script_name
                    marker_value = (
                        shlex.quote(str(marker)) if interpreter == "bash" else str(marker)
                    )
                    script.write_text(source.format(marker=marker_value), encoding="utf-8")
                    script.chmod(0o755)
                    result, _ = self.run_local_gates(
                        f"{interpreter} {shlex.quote(str(script))}"
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("terminal remote approval gate", result.stderr)
                    self.assertFalse(marker.exists())
    def test_local_gates_reject_interpreter_inline_program_forms_before_execution(self) -> None:
        forms = (
            "node -e", "node --eval=", "node -p", "node --print=", "node --require node:path -p", "node -pe",
            "python2 -c", "python3 -c", "python3 -X dev -c", "python3 -Ic", "pypy3 -c", "perl -e", "perl -E", "perl -we", "perl -pe", "perl -0 -e", "perl -C -e", "ruby -e", "ruby -we",
            "awk -e", "awk --source=", "awk", "awk --",
        )
        for form in forms:
            with self.subTest(form=form), tempfile.TemporaryDirectory() as directory:
                marker = pathlib.Path(directory) / "inline-ran"
                source = {
                    "python2 -c": f"__import__('pathlib').Path({str(marker)!r}).touch()", "python3 -Ic": f"__import__('pathlib').Path({str(marker)!r}).touch()", "pypy3 -c": f"__import__('pathlib').Path({str(marker)!r}).touch()",
                    "ruby -we": f"File.write({str(marker)!r}, '')",
                    "node -pe": f"require('fs').writeFileSync({str(marker)!r}, '')",
                }.get(form, f"touch {marker}")
                code = shlex.quote(source)
                attached_code = form.endswith(("=", "-e", "-p", "-c", "-E"))
                command = f"{form}{code}" if attached_code else f"{form} {code}"
                result, _ = self.run_local_gates(command)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("terminal remote approval gate", result.stderr)
                self.assertFalse(marker.exists())
    def test_local_gates_allow_awk_program_files_with_positional_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            program = root / "program.awk"
            input_file = root / "input.txt"
            program.write_text("{ print }\n", encoding="utf-8")
            input_file.write_text("benign input\n", encoding="utf-8")
            result, _ = self.run_local_gates(f"awk -f {shlex.quote(str(program))} {shlex.quote(str(input_file))}")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("benign input", result.stdout)
    def test_local_gates_allow_python_arguments_after_script_or_module_selection(self) -> None:
        for command in ("python3 script.py -e value", "python3 -m pytest -c pyproject.toml"):
            with self.subTest(command=command):
                result, _ = self.run_local_gates(command)
                self.assertNotIn("could not be parsed safely", result.stderr)
                self.assertIn("local gate [test]", result.stdout)
    def test_local_gates_reject_command_substitution_before_side_effects(self) -> None:
        constructions = {
            "dollar": (
                "$(printf g; printf h) pr checks",
                "$(printf 'delivery_'; printf 'gate.py')",
                "$(printf 'delivery-pr-'; printf 'approved.sh')",
            ),
            "backtick": (
                "`printf g; printf h` pr checks",
                "`printf 'delivery_'; printf 'gate.py'`",
                "`printf 'delivery-pr-'; printf 'approved.sh'`",
            ),
        }
        for form, terminal_commands in constructions.items():
            for terminal_command in terminal_commands:
                with self.subTest(form=form, terminal_command=terminal_command):
                    with tempfile.TemporaryDirectory() as directory:
                        marker = pathlib.Path(directory) / "side-effect"
                        command = f"{terminal_command}; touch {shlex.quote(str(marker))}"
                        result, _ = self.run_local_gates(command)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn("command substitution", result.stderr)
                        self.assertFalse(marker.exists())
    def test_local_gates_use_a_restricted_argv_executor(self) -> None:
        unsupported_commands = (
            "$'g''h' pr checks",
            "gh${GC_SESSION_ID:-manual} pr checks",
            "touch $((1 + 1))",
            "touch <(printf x)",
            "touch *.tmp",
            "{gh,x} pr checks",
            "bash -lc 'touch should-not-run'",
            "eval touch should-not-run",
        )
        for command in unsupported_commands:
            with self.subTest(command=command):
                with tempfile.TemporaryDirectory() as directory:
                    marker = pathlib.Path(directory) / "side-effect"
                    result, _ = self.run_local_gates(
                        f"{command}; touch {shlex.quote(str(marker))}"
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("terminal remote approval gate", result.stderr)
                    self.assertFalse(marker.exists())
    def test_local_gates_resolve_quotes_escapes_and_controlled_session_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = pathlib.Path(directory) / "quoted marker"
            result, _ = self.run_local_gates(f"touch {shlex.quote(str(marker))}")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(marker.exists())

        with tempfile.TemporaryDirectory() as directory:
            marker = pathlib.Path(directory) / "escaped marker"
            escaped_marker = str(marker).replace(" ", "\\ ")
            result, _ = self.run_local_gates(f"touch {escaped_marker}")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(marker.exists())

        with tempfile.TemporaryDirectory() as directory:
            marker_root = pathlib.Path(directory)
            result, _ = self.run_local_gates(
                f"touch {shlex.quote(str(marker_root))}/${{GC_SESSION_ID:-manual}}",
                session_id="session-42",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((marker_root / "session-42").exists())
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            capture = root / "capture-argv"
            capture.write_text("#!/usr/bin/env python3\nimport sys\nopen(sys.argv[1], 'w').write(sys.argv[2])\n", encoding="utf-8")
            capture.chmod(0o755)
            for expected, argument in (("$HOME", "'$HOME'"), (r"literal\q", r'"literal\q"')):
                output = root / expected.replace("$", "dollar").replace("\\", "backslash")
                result, _ = self.run_local_gates(f"{shlex.quote(str(capture))} {shlex.quote(str(output))} {argument}")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(output.read_text(encoding="utf-8"), expected)
        for command in ("printf '%s' $HOME", 'printf \'%s\' "$HOME"'):
            with self.subTest(command=command):
                result, _ = self.run_local_gates(command)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("parameter, arithmetic, or command expansion", result.stderr)
    def test_local_gates_reject_quote_split_terminal_commands_before_side_effects(self) -> None:
        for terminal_command in (
            'g"h" pr checks',
            '"g"h pr checks',
            'delivery_"gate.py"',
            'delivery-pr-"approved.sh"',
        ):
            with self.subTest(terminal_command=terminal_command):
                with tempfile.TemporaryDirectory() as directory:
                    marker = pathlib.Path(directory) / "side-effect"
                    command = f"{terminal_command}; touch {shlex.quote(str(marker))}"
                    result, _ = self.run_local_gates(command)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("terminal remote approval gate", result.stderr)
                    self.assertFalse(marker.exists())

    def test_local_gates_reject_redirection_and_grouping_operators(self) -> None:
        for operator in (";", "&", "|", "(", ")", "<", ">", "{", "}"):
            with self.subTest(operator=operator):
                result, _ = self.run_local_gates(f"printf '%s' {operator}")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("local gate command could not be parsed safely", result.stderr)

    def test_local_gates_allow_ordinary_shell_named_path_arguments(self) -> None:
        for name in ("sh", "su"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                marker = pathlib.Path(directory) / name
                result, _ = self.run_local_gates(f"touch {shlex.quote(str(marker))}")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(marker.exists())

    def test_setup_and_inspection_fail_closed_on_missing_prerequisites_or_stale_evidence(self) -> None:
        workflows = PACK_ROOT / "assets" / "workflows" / "complete-delivery-pr-gate"
        setup = (workflows / "{target}.setup-external-review.md").read_text(encoding="utf-8")
        inspect = (workflows / "{target}.inspect-current-head.md").read_text(encoding="utf-8")
        for prerequisite in ("authenticated `gh`", "delivery_gate.py", "writable"):
            self.assertIn(prerequisite, setup)
        self.assert_prose_contains(setup, "never set `gc.outcome=pass`")
        report = (workflows / "{target}.report-external-review.md").read_text(encoding="utf-8")
        finalizer = (workflows / "{target}.md").read_text(encoding="utf-8")
        self.assert_prose_contains(report, "close with a non-pass outcome")
        self.assert_prose_contains(finalizer, "then close with a non-pass outcome")
        for requirement in (
            "Remove any pre-existing",
            "semantic `gc.complete-delivery.pr-gate.v1` identity: exact `schema`, workflow-root `repo` and `pr_number`, Boolean `passed`: `true` only with `state: \"passed\"` and `false` only with `state: \"blocked\"`, canonical full `head_sha`, and typed `required_checks` as a list, `coderabbit` as an object, `unresolved_threads` as a list, `human_change_requests` as a list, and `blockers` as a list",
            "canonical full `head_sha` exactly equals workflow-root `delivery.head_sha`",
            "Never consume a pre-existing artifact after a command failure",
        ):
            self.assert_prose_contains(inspect, requirement)
        self.assert_prose_contains(inspect, "First invalidate prior terminal-success evidence")
        self.assert_prose_contains(inspect, "record it as `candidate_commit`")

    def test_fresh_blocked_snapshots_and_post_lock_rechecks_preserve_no_stale_authority(self) -> None:
        workflows = PACK_ROOT / "assets" / "workflows" / "complete-delivery-pr-gate"
        snapshot_contents = ((workflows / "{target}.resolve-findings.md").read_text(encoding="utf-8"), (PACK_ROOT / "agents" / "external-review-resolver" / "prompt.template.md").read_text(encoding="utf-8"))
        for content in snapshot_contents:
            self.assert_prose_contains(content, "fresh canonical head-matched blocked snapshot")
            self.assertIn("prior terminal-success evidence", content)
            self.assertTrue(all(term in content for term in ("inspected_head", "candidate_commit")))
            self.assert_prose_contains(content, "blocker-only state")
        for content in ((workflows / "{target}.publish-fixes.md").read_text(encoding="utf-8"), (PACK_ROOT / "agents" / "external-review-resolver" / "prompt.template.md").read_text(encoding="utf-8")):
            self.assert_prose_contains(content, "After acquiring it, recheck")
            self.assert_prose_contains(content, "unavailable lock, dirty tree, or mismatch fails closed before push, refresh, or resolution")

    def test_every_non_success_transition_invalidates_prior_success_evidence(self) -> None:
        workflows = PACK_ROOT / "assets" / "workflows" / "complete-delivery-pr-gate"
        consumers = {
            "{target}.setup-external-review.md": (
                "If any prerequisite is failed, blocked, skipped, or unavailable, "
                "perform the same invalidation"
            ),
            "{target}.inspect-current-head.md": (
                "invalidate stale `tested_commit`, `local_gates`, `published_head`, "
                "and `published_head_matches_tested_commit`"
            ),
            "{target}.resolve-findings.md": "write only blocker state",
            "{target}.rerun-local-gates.md": (
                "must leave all of those success fields cleared or explicitly "
                "overwritten as failed"
            ),
            "{target}.publish-fixes.md": "write blocker-only state",
            "{target}.report-external-review.md": (
                "otherwise invalidate `tested_commit`, `local_gates`, `published_head`, "
                "and `published_head_matches_tested_commit`"
            ),
            "{target}.external-review-loop.md": (
                "Treat any failed, blocked, skipped, unavailable, stale, malformed, "
                "or head-mismatched child evidence as fail-closed: invalidate stale "
                "`tested_commit`, `local_gates`, `published_head`, and "
                "`published_head_matches_tested_commit`"
            ),
            "{target}.md": (
                "On any non-success finalization path, invalidate the handoff's "
                "`tested_commit`, `local_gates`, `published_head`, and "
                "`published_head_matches_tested_commit` success evidence"
            ),
        }
        for filename, invalidation_semantics in consumers.items():
            with self.subTest(filename=filename):
                text = (workflows / filename).read_text(encoding="utf-8")
                self.assert_prose_contains(text, invalidation_semantics)
                for field in (
                    "tested_commit",
                    "local_gates",
                    "published_head",
                    "published_head_matches_tested_commit",
                ):
                    self.assertIn(field, text)

    def test_publication_retains_current_test_evidence_until_an_invalidating_failure(self) -> None:
        workflow = (
            PACK_ROOT / "assets" / "workflows" / "complete-delivery-pr-gate" / "{target}.publish-fixes.md"
        ).read_text(encoding="utf-8")
        self.assert_prose_contains(
            workflow, "retaining and validating the current attempt's `tested_commit` and passed `local_gates`"
        )
        self.assert_prose_contains(
            workflow, "Clear only stale `published_head` and equality success evidence before push or refresh"
        )
        self.assert_prose_contains(
            workflow, "that invalidates the whole handoff, clear `tested_commit` and `local_gates`"
        )

    def test_terminal_publication_and_merge_recovery_are_deadline_and_state_bound(self) -> None:
        workflows = PACK_ROOT / "assets" / "workflows" / "complete-delivery-pr-gate"
        publish_fixes = (workflows / "{target}.publish-fixes.md").read_text(encoding="utf-8")
        merge = (PACK_ROOT / "assets" / "workflows" / "complete-delivery" / "merge.md").read_text(encoding="utf-8")

        for requirement in (
            "Bound lock acquisition by the deadline's remaining time",
            "immediately run `.gc/scripts/checks/delivery-external-review-deadline.sh --validate`",
            "perform no push, refresh, or resolution",
            "exhausted deadline, attempt budget, or recovery budget is a non-pass outcome",
        ):
            self.assert_prose_contains(publish_fixes, requirement)
        self.assert_prose_contains(
            publish_fixes,
            "after the final permitted resolution and before recording any pass outcome",
        )
        self.assert_prose_contains(publish_fixes, "write blocker-only expiry evidence")
        self.assert_prose_contains(publish_fixes, "preserve the truth of any already-resolved threads")
        self.assert_prose_contains(publish_fixes, "must not claim to reopen a thread")
        self.assert_prose_contains(merge, "Do not invoke `delivery-pr-open.sh` for this already-merged recovery")
        self.assert_prose_contains(merge, "only after that fresh reconciliation proves `merged=false` and `state=open`")
        self.assert_prose_contains(publish_fixes, "immediately persist all refreshed workflow-root identity fields before any thread resolution")
        for field in (
            "delivery.head_sha",
            "delivery.repo",
            "delivery.branch",
            "delivery.pr_number",
            "delivery.pr_url",
        ):
            with self.subTest(field=field):
                self.assertIn(field, publish_fixes)
        self.assert_prose_contains(publish_fixes, "deadline validation failure is a deadline blocker, a refresh failure is a publication blocker, and a thread-resolution failure is a resolution blocker")

    def test_local_gate_success_is_invalidated_if_the_deadline_expires(self) -> None:
        workflow = (
            PACK_ROOT / "assets" / "workflows" / "complete-delivery-pr-gate" / "{target}.rerun-local-gates.md"
        ).read_text(encoding="utf-8")
        self.assert_prose_contains(workflow, "Immediately after all local gates pass and before recording any success evidence")
        self.assert_prose_contains(
            workflow,
            "Immediately after that final deadline validation and before atomically writing any passing authority",
        )
        self.assert_prose_contains(workflow, "repeat the clean-tree check")
        self.assert_prose_contains(workflow, "erase `tested_commit`, `local_gates`, `published_head`, and `published_head_matches_tested_commit`")

    def test_local_gates_execute_an_allowed_local_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = pathlib.Path(directory) / "local-gate-ran"
            result, _ = self.run_local_gates(f"touch {shlex.quote(str(marker))}")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(marker.exists())

    def test_local_gates_allow_benign_coderabbit_named_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = pathlib.Path(directory) / "test_coderabbit.py"
            result, _ = self.run_local_gates(f"touch {shlex.quote(str(marker))}")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(marker.exists())

    def run_terminal_gate(
        self, handoff: dict[str, object], report: object | None = None
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
            started = datetime.now(timezone.utc).replace(microsecond=0)
            metadata = {
                "delivery.repo": "owner/repo",
                "delivery.pr_number": "8",
                "gc.var.artifact_root": str(artifact_root),
                "delivery.external_review_started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "delivery.external_review_deadline": (started + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            gc.write_text(
                '#!/bin/sh\nif [ "${2:-}" = history ]; then\n  printf "%s\\n" "$FAKE_GC_HISTORY_JSON"\nelse\n  printf "%s\\n" "$FAKE_GC_STEP_JSON"\nfi\n',
                encoding="utf-8",
            )
            gc.chmod(0o755)
            if report is not None:
                report_fixture = root / "delivery-gate-report.json"
                report_fixture.write_text(
                    report if isinstance(report, str) else json.dumps(report), encoding="utf-8"
                )
                python = bin_dir / "python3"
                python.write_text(
                    "#!/bin/sh\n"
                    "if [ \"${1##*/}\" = delivery_gate.py ]; then\n"
                    "  shift\n"
                    "  while [ \"$#\" -gt 0 ]; do\n"
                    "    if [ \"$1\" = --output ]; then\n"
                    "      if ! \"$REAL_PYTHON\" -c 'import json, sys; json.load(open(sys.argv[1]))' \"$GATE_REPORT_FIXTURE\"; then\n"
                    "        printf '%s\n' 'delivery_gate.py: malformed report fixture' >&2\n"
                    "        exit 2\n"
                    "      fi\n"
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
                    "FAKE_GC_HISTORY_JSON": json.dumps([{"Issue": {"metadata": metadata}}]),
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

    def test_terminal_gate_rejects_noncanonical_local_gate_evidence(self) -> None:
        commit = "a" * 40
        for extra_evidence in (
            {"nested": {"result": {"status": "failed"}}},
            {"availability": "unavailable"},
            {"passed": True},
        ):
            with self.subTest(extra_evidence=extra_evidence):
                result = self.run_terminal_gate(
                    {
                        "candidate_commit": commit,
                        "tested_commit": commit,
                        "published_head": commit,
                        "published_head_matches_tested_commit": True,
                        "local_gates": {
                            "status": "passed",
                            "tested_commit": commit,
                            **extra_evidence,
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

    def test_terminal_gate_canonicalizes_valid_full_sha_evidence_before_comparison(self) -> None:
        commit = "aB" * 20
        result = self.run_terminal_gate(
            {
                "candidate_commit": commit.swapcase(),
                "tested_commit": commit,
                "published_head": commit.upper(),
                "published_head_matches_tested_commit": True,
                "local_gates": {"status": "passed", "tested_commit": commit.swapcase()},
            },
            {"passed": True, "head_sha": commit.upper()},
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

    def test_terminal_gate_rejects_malformed_or_missing_head_delivery_gate_report(self) -> None:
        commit = "a" * 40
        handoff = {
            "candidate_commit": commit,
            "tested_commit": commit,
            "published_head": commit,
            "published_head_matches_tested_commit": True,
            "local_gates": {"status": "passed", "tested_commit": commit},
        }
        malformed = self.run_terminal_gate(handoff, "not-json")
        self.assertEqual(malformed.returncode, 2, malformed.stderr)
        self.assertIn("delivery_gate.py", malformed.stderr)

        missing_head = self.run_terminal_gate(handoff, {"passed": True})
        self.assertNotEqual(missing_head.returncode, 0)
        self.assertIn("delivery_gate.py", missing_head.stderr)


if __name__ == "__main__":
    unittest.main()
