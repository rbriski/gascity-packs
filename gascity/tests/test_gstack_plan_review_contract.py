"""Regression fixture for root-bound gstack plan-review retry lanes."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import tempfile
import unittest


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "assets/scripts/checks/gstack-plan-review-context-valid.py"


class GstackPlanReviewContractTests(unittest.TestCase):
    def test_retry_requires_fresh_root_bound_context_for_all_four_lanes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            artifact = tmp / "artifacts"
            artifact.mkdir()
            plan = artifact / "implementation-plan.md"
            review_input = artifact / "plan-review-context.md"
            plan.write_text("correct finance plan\n", encoding="utf-8")
            review_input.write_text("correct finance review input\n", encoding="utf-8")
            root = {
                "id": "root",
                "metadata": {
                    "gc.var.artifact_root": str(artifact),
                    "gc.var.source_bead_id": "source-finance",
                    "gc.build.plan_path": str(plan),
                    "gc.build.plan_review_context_path": str(review_input),
                },
            }

            fixture = tmp / "fixture.json"
            fake_gc = tmp / "gc"
            fake_gc.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "data=json.load(open(os.environ['GC_FIXTURE']))\n"
                "if sys.argv[1:3] == ['bd','show']:\n"
                " print(json.dumps([data['beads'][sys.argv[3]]]))\n"
                "elif sys.argv[1:3] == ['bd','list']:\n"
                " print(json.dumps(data['items']))\n"
                "elif sys.argv[1:3] == ['bd','update']:\n"
                " print('{}')\n"
                "else: raise SystemExit(2)\n",
                encoding="utf-8",
            )
            fake_gc.chmod(0o755)

            def run(data: dict[str, object], *args: str, bead_id: str = "loop") -> subprocess.CompletedProcess[str]:
                fixture.write_text(json.dumps(data), encoding="utf-8")
                command_args = args or ("--loop",)
                return subprocess.run(
                    [str(SCRIPT), *command_args],
                    text=True,
                    capture_output=True,
                    env={**os.environ, "GC_BEAD_ID": bead_id, "GC_FIXTURE": str(fixture), "PATH": f"{tmp}{os.pathsep}{os.environ['PATH']}"},
                    check=False,
                )

            scope = "complete-delivery.plan-review.gstack-plan-review-loop.iteration.2"
            common = {"gc.root_bead_id": "root", "gc.attempt": "2", "gc.scope_ref": scope}
            loop = {"id": "loop", "metadata": common}
            setup = {"id": "setup", "metadata": {"gc.root_bead_id": "root"}}
            prepared = run({"beads": {"root": root, "setup": setup}, "items": []}, "--prepare", bead_id="setup")
            self.assertEqual(prepared.returncode, 0, prepared.stderr)
            missing = run({"beads": {"root": root, "loop": loop}, "items": []})
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("attempt-local plan-review context is missing", missing.stderr)

            attempt_dir = artifact / "plan-review/root/attempt-2"
            context_path = attempt_dir / "context.json"
            header = "\n".join((
                "root_bead_id: root", "source_bead_id: source-finance", "attempt: 2",
                f"scope_ref: {scope}", f"context_path: {context_path}",
            ))
            items: list[dict[str, object]] = []
            for lane, step, filename in (
                ("founder", "plan-review.founder-scope-review", "founder"),
                ("design", "plan-review.design-plan-review", "design"),
                ("engineering", "plan-review.engineering-plan-review", "engineering"),
                ("devex", "plan-review.devex-plan-review", "devex"),
            ):
                lane_bead = {"id": lane, "metadata": {**common, "gc.step_id": step}}
                fresh_lane = run({"beads": {"root": root, lane: lane_bead}, "items": []}, "--lane-inputs", lane, bead_id=lane)
                self.assertEqual(fresh_lane.returncode, 0, fresh_lane.stderr)
                output = attempt_dir / f"{filename}.md"
                output.write_text(header + "\nreview\n", encoding="utf-8")
                items.append({"id": lane, "metadata": {**common, "gc.step_id": step, f"gstack.plan_review.{lane}_verdict": "approve", "gstack.plan_review.output_path": str(output)}})
            synthesis = attempt_dir / "synthesis.md"
            synthesis.write_text(header + "\nsynthesis\n", encoding="utf-8")
            items.append({"id": "synthesis", "metadata": {**common, "gc.step_id": "plan-review.synthesize-plan-review", "gstack.plan_review.synthesis_path": str(synthesis)}})
            remediation = attempt_dir / "remediation.md"
            remediation.write_text(header + "\nremediation\n", encoding="utf-8")
            items.append({"id": "apply", "metadata": {**common, "gc.step_id": "plan-review.apply-plan-review-findings", "design_review.verdict": "done", "design_review.report_path": str(remediation)}})

            corrected = run({"beads": {"root": root, "loop": loop}, "items": items})
            self.assertEqual(corrected.returncode, 0, corrected.stderr)
            self.assertIn("root-bound current-attempt outputs", corrected.stdout)


if __name__ == "__main__":
    unittest.main()
