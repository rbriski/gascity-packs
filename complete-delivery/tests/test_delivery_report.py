from __future__ import annotations

import importlib.util
import json
import pathlib
import tempfile
import types
import unittest


MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "assets"
    / "scripts"
    / "delivery_report.py"
)
SPEC = importlib.util.spec_from_file_location("delivery_report", MODULE_PATH)
assert SPEC and SPEC.loader
delivery_report = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(delivery_report)


class DeliveryReportTests(unittest.TestCase):
    def test_init_and_update_render_html_and_css(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            state = root / "state.json"
            result = delivery_report.main(
                [
                    "init",
                    "--state",
                    str(state),
                    "--title",
                    "Ship the feature",
                    "--goal",
                    "Reach verified production",
                    "--repo",
                    "owner/repo",
                    "--bead-id",
                    "xy-123",
                ]
            )
            self.assertEqual(result, 0)
            self.assertTrue((root / "index.html").is_file())
            self.assertTrue((root / "styles.css").is_file())
            result = delivery_report.main(
                [
                    "update",
                    "--state",
                    str(state),
                    "--stage",
                    "deploy",
                    "--status",
                    "passed",
                    "--summary",
                    "Production attested",
                    "--sha",
                    "a" * 40,
                    "--production-url",
                    "https://service.example.test",
                    "--evidence",
                    "health check returned 200",
                ]
            )
            self.assertEqual(result, 0)
            payload = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(payload["stages"]["deploy"]["status"], "passed")
            rendered = (root / "index.html").read_text(encoding="utf-8")
            self.assertIn("Production attested", rendered)
            self.assertIn("health check returned 200", rendered)

    def test_user_content_is_escaped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = pathlib.Path(directory) / "state.json"
            delivery_report.main(
                [
                    "init",
                    "--state",
                    str(state),
                    "--title",
                    "<script>alert(1)</script>",
                    "--goal",
                    "safe & clear",
                ]
            )
            rendered = (state.parent / "index.html").read_text(encoding="utf-8")
            self.assertNotIn("<script>alert(1)</script>", rendered)
            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", rendered)

    def test_reinitialization_preserves_milestones(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = pathlib.Path(directory) / "state.json"
            delivery_report.main(
                ["init", "--state", str(state), "--title", "One", "--goal", "Goal"]
            )
            delivery_report.main(
                [
                    "update",
                    "--state",
                    str(state),
                    "--stage",
                    "plan",
                    "--status",
                    "passed",
                    "--summary",
                    "Approved",
                ]
            )
            delivery_report.main(
                ["init", "--state", str(state), "--title", "Renamed", "--goal", "Goal"]
            )
            payload = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(payload["title"], "Renamed")
            self.assertEqual(payload["stages"]["plan"]["status"], "passed")

    def test_unsafe_link_scheme_is_not_rendered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = pathlib.Path(directory) / "state.json"
            delivery_report.main(
                ["init", "--state", str(state), "--title", "Safe", "--goal", "Goal"]
            )
            delivery_report.main(
                [
                    "update",
                    "--state",
                    str(state),
                    "--stage",
                    "pull-request",
                    "--status",
                    "passed",
                    "--summary",
                    "Published",
                    "--pr-url",
                    "javascript:alert(1)",
                ]
            )
            rendered = (state.parent / "index.html").read_text(encoding="utf-8")
            self.assertNotIn("javascript:", rendered)

    def test_final_validation_requires_every_terminal_milestone_and_matching_sha(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = pathlib.Path(directory) / "state.json"
            delivery_report.main(
                ["init", "--state", str(state_path), "--title", "Safe", "--goal", "Goal"]
            )
            state = delivery_report.load_state(state_path)
            for stage in delivery_report.STAGES:
                state["stages"][stage] = {
                    "status": "passed",
                    "summary": "Verified",
                    "evidence": [],
                }
            merge_sha = "a" * 40
            pr_url = "https://github.com/owner/repo/pull/7"
            production_url = "https://service.example.test"
            state.update(
                sha=merge_sha,
                pr_url=pr_url,
                production_url=production_url,
                next_action="No action required",
            )
            delivery_report.persist(state_path, state)
            args = types.SimpleNamespace(
                state=state_path,
                merge_sha=merge_sha,
                deployed_sha=merge_sha,
                deploy_status="verified",
                pr_url=pr_url,
                production_url=production_url,
            )
            self.assertEqual(delivery_report.validate_final(args)["sha"], merge_sha)

            state = delivery_report.load_state(state_path)
            state["stages"]["external-review"]["status"] = "active"
            delivery_report.persist(state_path, state)
            with self.assertRaises(delivery_report.ReportError):
                delivery_report.validate_final(args)

    def test_not_applicable_deploy_may_be_skipped_but_verification_must_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = pathlib.Path(directory) / "state.json"
            delivery_report.main(
                ["init", "--state", str(state_path), "--title", "Artifact", "--goal", "Goal"]
            )
            state = delivery_report.load_state(state_path)
            for stage in delivery_report.STAGES:
                state["stages"][stage] = {
                    "status": "passed",
                    "summary": "Verified",
                    "evidence": [],
                }
            state["stages"]["deploy"]["status"] = "skipped"
            merge_sha = "b" * 40
            pr_url = "https://github.com/owner/repo/pull/8"
            state.update(sha=merge_sha, pr_url=pr_url)
            delivery_report.persist(state_path, state)
            args = types.SimpleNamespace(
                state=state_path,
                merge_sha=merge_sha,
                deployed_sha="",
                deploy_status="not_applicable",
                pr_url=pr_url,
                production_url="",
            )
            self.assertEqual(delivery_report.validate_final(args)["sha"], merge_sha)


if __name__ == "__main__":
    unittest.main()
