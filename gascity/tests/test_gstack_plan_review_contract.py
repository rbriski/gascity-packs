"""Regression fixtures for root-bound gstack plan-review retry lanes."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import tempfile
import unittest


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "assets/scripts/checks/gstack-plan-review-context-valid.py"
LANES = (
    ("founder", "plan-review.founder-scope-review", "founder"),
    ("design", "plan-review.design-plan-review", "design"),
    ("engineering", "plan-review.engineering-plan-review", "engineering"),
    ("devex", "plan-review.devex-plan-review", "devex"),
)


class GstackPlanReviewContractTests(unittest.TestCase):
    def test_plan_edit_iterates_then_fresh_retry_validates_all_modes(self) -> None:
        with fixture_environment() as fixture:
            prepared = fixture.run({"beads": {"root": fixture.root, "setup": fixture.setup}, "items": []}, "--prepare", bead_id="setup")
            self.assertEqual(prepared.returncode, 0, prepared.stderr)

            first = fixture.contract("1")
            missing = fixture.run({"beads": {"root": fixture.root, "loop": {"id": "loop", "metadata": first}}, "items": []})
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("attempt-local plan-review context is missing", missing.stderr)

            first_items = fixture.completed_lane_items(first)
            synthesis = fixture.synthesis_item(first)
            first_items.append(synthesis)
            synthesis_check = fixture.run(
                {"beads": {"root": fixture.root, "synthesis": synthesis}, "items": first_items[:-1]},
                "--synthesis",
                bead_id="synthesis",
            )
            self.assertEqual(synthesis_check.returncode, 0, synthesis_check.stderr)

            apply = fixture.apply_item(first, verdict="iterate")
            apply_inputs = fixture.run(
                {"beads": {"root": fixture.root, "apply": apply}, "items": first_items},
                "--apply-inputs",
                bead_id="apply",
            )
            self.assertEqual(apply_inputs.returncode, 0, apply_inputs.stderr)

            # The apply lane may modify the reviewed plan after it consumed the
            # first attempt.  Its post-edit validation must still validate the
            # immutable binding and outputs rather than recomputing attempt 1.
            fixture.plan.write_text("corrected finance plan\n", encoding="utf-8")
            apply_check = fixture.run(
                {"beads": {"root": fixture.root, "apply": apply}, "items": first_items},
                "--apply",
                bead_id="apply",
            )
            self.assertEqual(apply_check.returncode, 0, apply_check.stderr)

            second = fixture.contract("2")
            second_items = fixture.completed_lane_items(second)
            second_context = fixture.context_path(second)
            payload = json.loads(second_context.read_text(encoding="utf-8"))
            self.assertEqual(payload["plan_sha256"], hashlib.sha256(fixture.plan.read_bytes()).hexdigest())

            second_synthesis = fixture.synthesis_item(second)
            second_items.append(second_synthesis)
            self.assertEqual(
                fixture.run(
                    {"beads": {"root": fixture.root, "synthesis": second_synthesis}, "items": second_items[:-1]},
                    "--synthesis",
                    bead_id="synthesis",
                ).returncode,
                0,
            )
            second_apply = fixture.apply_item(second, verdict="done")
            self.assertEqual(
                fixture.run(
                    {"beads": {"root": fixture.root, "apply": second_apply}, "items": second_items},
                    "--apply-inputs",
                    bead_id="apply",
                ).returncode,
                0,
            )
            self.assertEqual(
                fixture.run(
                    {"beads": {"root": fixture.root, "apply": second_apply}, "items": second_items},
                    "--apply",
                    bead_id="apply",
                ).returncode,
                0,
            )
            second_items.append(second_apply)
            approved = fixture.run(
                {"beads": {"root": fixture.root, "loop": {"id": "loop", "metadata": second}}, "items": second_items}
            )
            self.assertEqual(approved.returncode, 0, approved.stderr)

    def test_fan_in_rejects_open_failed_and_wrong_context_outputs(self) -> None:
        with fixture_environment() as fixture:
            self.assertEqual(
                fixture.run({"beads": {"root": fixture.root, "setup": fixture.setup}, "items": []}, "--prepare", bead_id="setup").returncode,
                0,
            )
            contract = fixture.contract("1")
            items = fixture.completed_lane_items(contract)
            loop = {"id": "loop", "metadata": contract}
            synthesis = fixture.synthesis_item(contract)
            apply = fixture.apply_item(contract, verdict="done")

            items[0]["status"] = "open"
            rejected_open = fixture.run({"beads": {"root": fixture.root, "loop": loop}, "items": items + [synthesis, apply]})
            self.assertIn("must be closed", rejected_open.stderr)
            items[0]["status"] = "closed"
            items[0]["metadata"].pop("gc.outcome")
            rejected_failed = fixture.run({"beads": {"root": fixture.root, "loop": loop}, "items": items + [synthesis, apply]})
            self.assertIn("gc.outcome=pass", rejected_failed.stderr)
            items[0]["metadata"]["gc.outcome"] = "pass"

            # A report from the recurring-payments source cannot masquerade as
            # this finance attempt even if its metadata path looks plausible.
            output = pathlib.Path(items[0]["metadata"]["gstack.plan_review.output_path"])
            output.write_text(output.read_text(encoding="utf-8").replace("source-finance", "source-recurring-payments"), encoding="utf-8")
            rejected_source = fixture.run({"beads": {"root": fixture.root, "loop": loop}, "items": items + [synthesis, apply]})
            self.assertIn("not bound to this root/source/attempt/context", rejected_source.stderr)

            outside = fixture.tmp / "recurring-payments.md"
            outside.write_text("unrelated\n", encoding="utf-8")
            items[0]["metadata"]["gstack.plan_review.output_path"] = str(outside)
            rejected_path = fixture.run({"beads": {"root": fixture.root, "loop": loop}, "items": items + [synthesis, apply]})
            self.assertIn("must be exactly", rejected_path.stderr)

    def test_concurrent_lane_setup_publishes_parseable_attempt_snapshot(self) -> None:
        with fixture_environment() as fixture:
            self.assertEqual(
                fixture.run({"beads": {"root": fixture.root, "setup": fixture.setup}, "items": []}, "--prepare", bead_id="setup").returncode,
                0,
            )
            contract = fixture.contract("1")
            lane_beads = {
                lane: {"id": lane, "metadata": {**contract, "gc.step_id": step}}
                for lane, step, _ in LANES
            }
            payload = {"beads": {"root": fixture.root, **lane_beads}, "items": []}
            fixture.fixture.write_text(json.dumps(payload), encoding="utf-8")
            processes = [fixture.invoke("--lane-inputs", lane, bead_id=lane) for lane, _, _ in LANES]
            for (lane, _, output_name), process in zip(LANES, processes):
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, stderr or stdout)
                self.assertEqual(
                    json.loads(stdout),
                    fixture.manifest(
                        contract,
                        inputs=[fixture.plan, fixture.review_input],
                        outputs=[fixture.context_path(contract).parent / f"{output_name}.md"],
                    ),
                )
            snapshot = fixture.context_path(contract)
            self.assertEqual(
                json.loads(snapshot.read_text(encoding="utf-8")),
                {
                    "root_bead_id": "root",
                    "source_bead_id": "source-finance",
                    "artifact_root": str(fixture.artifact),
                    "plan_path": str(fixture.plan),
                    "review_context_path": str(fixture.review_input),
                    "attempt": "1",
                    "scope_ref": contract["gc.scope_ref"],
                    "plan_sha256": hashlib.sha256(fixture.plan.read_bytes()).hexdigest(),
                    "review_context_sha256": hashlib.sha256(fixture.review_input.read_bytes()).hexdigest(),
                },
            )

    def test_synthesis_and_apply_inputs_print_bound_path_manifests(self) -> None:
        with fixture_environment() as fixture:
            self.assertEqual(
                fixture.run({"beads": {"root": fixture.root, "setup": fixture.setup}, "items": []}, "--prepare", bead_id="setup").returncode,
                0,
            )
            contract = fixture.contract("1")
            lanes = fixture.completed_lane_items(contract)
            synthesis = fixture.synthesis_item(contract)
            synthesis_inputs = fixture.run(
                {"beads": {"root": fixture.root, "synthesis": synthesis}, "items": lanes},
                "--synthesis-inputs",
                bead_id="synthesis",
            )
            self.assertEqual(synthesis_inputs.returncode, 0, synthesis_inputs.stderr)
            self.assertEqual(
                json.loads(synthesis_inputs.stdout),
                fixture.manifest(
                    contract,
                    inputs=[fixture.context_path(contract).parent / f"{name}.md" for _, _, name in LANES],
                    outputs=[fixture.context_path(contract).parent / "synthesis.md"],
                ),
            )

            apply = fixture.apply_item(contract, verdict="done")
            apply_inputs = fixture.run(
                {"beads": {"root": fixture.root, "apply": apply}, "items": lanes + [synthesis]},
                "--apply-inputs",
                bead_id="apply",
            )
            self.assertEqual(apply_inputs.returncode, 0, apply_inputs.stderr)
            self.assertEqual(
                json.loads(apply_inputs.stdout),
                fixture.manifest(
                    contract,
                    inputs=[fixture.context_path(contract).parent / "synthesis.md", fixture.plan],
                    outputs=[fixture.plan, fixture.context_path(contract).parent / "remediation.md"],
                ),
            )


class Fixture:
    def __init__(self, tmp: pathlib.Path) -> None:
        self.tmp = tmp
        self.artifact = tmp / "artifacts"
        self.artifact.mkdir()
        self.plan = self.artifact / "implementation-plan.md"
        self.review_input = self.artifact / "plan-review-context.md"
        self.plan.write_text("correct finance plan\n", encoding="utf-8")
        self.review_input.write_text("correct finance review input\n", encoding="utf-8")
        self.root = {"id": "root", "metadata": {"gc.var.artifact_root": str(self.artifact), "gc.var.source_bead_id": "source-finance", "gc.build.plan_path": str(self.plan), "gc.build.plan_review_context_path": str(self.review_input)}}
        self.setup = {"id": "setup", "metadata": {"gc.root_bead_id": "root"}}
        self.fixture = tmp / "fixture.json"
        self.fake_gc = tmp / "gc"
        self.fake_gc.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "data=json.load(open(os.environ['GC_FIXTURE']))\n"
            "beads_cli = 'b' 'd'\n"
            "if sys.argv[1:3] == [beads_cli,'show']: print(json.dumps([data['beads'][sys.argv[3]]]))\n"
            "elif sys.argv[1:3] == [beads_cli,'list']: print(json.dumps(data['items']))\n"
            "elif sys.argv[1:3] == [beads_cli,'update']: print('{}')\n"
            "else: raise SystemExit(2)\n",
            encoding="utf-8",
        )
        self.fake_gc.chmod(0o755)

    def contract(self, attempt: str) -> dict[str, str]:
        return {"gc.root_bead_id": "root", "gc.attempt": attempt, "gc.scope_ref": f"complete-delivery.plan-review.gstack-plan-review-loop.iteration.{attempt}"}

    def context_path(self, contract: dict[str, str]) -> pathlib.Path:
        return self.artifact / "plan-review/root" / f"attempt-{contract['gc.attempt']}" / "context.json"

    def header(self, contract: dict[str, str]) -> str:
        return "\n".join(("root_bead_id: root", "source_bead_id: source-finance", f"attempt: {contract['gc.attempt']}", f"scope_ref: {contract['gc.scope_ref']}", f"context_path: {self.context_path(contract)}"))

    def manifest(self, contract: dict[str, str], *, inputs: list[pathlib.Path], outputs: list[pathlib.Path]) -> dict[str, object]:
        return {
            "root_bead_id": "root",
            "source_bead_id": "source-finance",
            "artifact_root": str(self.artifact),
            "plan_path": str(self.plan),
            "review_context_path": str(self.review_input),
            "attempt": contract["gc.attempt"],
            "scope_ref": contract["gc.scope_ref"],
            "context_path": str(self.context_path(contract)),
            "permitted_input_paths": [str(path) for path in inputs],
            "permitted_output_paths": [str(path) for path in outputs],
        }

    def invoke(self, *args: str, bead_id: str) -> subprocess.Popen[str]:
        return subprocess.Popen([str(SCRIPT), *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={**os.environ, "GC_BEAD_ID": bead_id, "GC_FIXTURE": str(self.fixture), "PATH": f"{self.tmp}{os.pathsep}{os.environ['PATH']}"})

    def run(self, data: dict[str, object], *args: str, bead_id: str = "loop") -> subprocess.CompletedProcess[str]:
        self.fixture.write_text(json.dumps(data), encoding="utf-8")
        return subprocess.run([str(SCRIPT), *(args or ("--loop",))], text=True, capture_output=True, env={**os.environ, "GC_BEAD_ID": bead_id, "GC_FIXTURE": str(self.fixture), "PATH": f"{self.tmp}{os.pathsep}{os.environ['PATH']}"}, check=False)

    def completed_lane_items(self, contract: dict[str, str]) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        for lane, step, filename in LANES:
            lane_bead = {"id": lane, "metadata": {**contract, "gc.step_id": step}}
            created = self.run({"beads": {"root": self.root, lane: lane_bead}, "items": []}, "--lane-inputs", lane, bead_id=lane)
            if created.returncode:
                raise AssertionError(created.stderr)
            output = self.context_path(contract).parent / f"{filename}.md"
            output.write_text(self.header(contract) + "\nreview\n", encoding="utf-8")
            items.append({"id": lane, "status": "closed", "metadata": {**contract, "gc.step_id": step, "gc.outcome": "pass", f"gstack.plan_review.{lane}_verdict": "approve", "gstack.plan_review.output_path": str(output)}})
        return items

    def synthesis_item(self, contract: dict[str, str]) -> dict[str, object]:
        output = self.context_path(contract).parent / "synthesis.md"
        output.write_text(self.header(contract) + "\nsynthesis\n", encoding="utf-8")
        return {"id": "synthesis", "status": "closed", "metadata": {**contract, "gc.step_id": "plan-review.synthesize-plan-review", "gc.outcome": "pass", "gstack.plan_review.synthesis_path": str(output), "gstack.plan_review.output_path": str(output)}}

    def apply_item(self, contract: dict[str, str], *, verdict: str) -> dict[str, object]:
        output = self.context_path(contract).parent / "remediation.md"
        output.write_text(self.header(contract) + "\nremediation\n", encoding="utf-8")
        return {"id": "apply", "status": "closed", "metadata": {**contract, "gc.step_id": "plan-review.apply-plan-review-findings", "gc.outcome": "pass", "design_review.verdict": verdict, "design_review.report_path": str(output), "gstack.plan_review.output_path": str(output)}}


class fixture_environment:
    def __enter__(self) -> Fixture:
        self.directory = tempfile.TemporaryDirectory()
        return Fixture(pathlib.Path(self.directory.name))

    def __exit__(self, *unused: object) -> None:
        self.directory.cleanup()


if __name__ == "__main__":
    unittest.main()
