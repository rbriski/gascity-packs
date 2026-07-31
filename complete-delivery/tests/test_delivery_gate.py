from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys
import unittest
from unittest import mock


MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "assets"
    / "scripts"
    / "delivery_gate.py"
)
SPEC = importlib.util.spec_from_file_location("delivery_gate", MODULE_PATH)
assert SPEC and SPEC.loader
delivery_gate = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = delivery_gate
SPEC.loader.exec_module(delivery_gate)


class FakeClient:
    def __init__(self) -> None:
        self.head = "a" * 40
        self.base = "main"
        self.runs = [
            {
                "name": "verify",
                "status": "completed",
                "conclusion": "success",
                "completed_at": "2026-07-31T01:00:00Z",
                "app": {"slug": "github-actions", "name": "GitHub Actions"},
            },
            {
                "name": "CodeRabbit",
                "status": "completed",
                "conclusion": "success",
                "completed_at": "2026-07-31T01:01:00Z",
                "app": {"slug": "coderabbitai", "name": "CodeRabbit"},
            },
        ]
        self.commit_statuses = []
        self.required = ["verify"]
        self.protected = True
        self.review_items = []
        self.threads = []
        self.draft = False
        self.pull_state = "open"
        self.refreshed_head = self.head
        self.pull_calls = 0

    def pull_request(self, repo: str, number: int):
        self.pull_calls += 1
        head = self.head if self.pull_calls == 1 else self.refreshed_head
        return {
            "state": self.pull_state,
            "draft": self.draft,
            "head": {"sha": head},
            "base": {"ref": self.base},
        }

    def check_runs(self, repo: str, sha: str):
        return self.runs

    def statuses(self, repo: str, sha: str):
        return self.commit_statuses

    def branch_protection(self, repo: str, branch: str):
        return delivery_gate.BranchProtection(
            protected=self.protected,
            required_contexts=tuple(self.required),
        )

    def reviews(self, repo: str, number: int):
        return self.review_items

    def review_threads(self, repo: str, number: int):
        return self.threads


class DeliveryGateTests(unittest.TestCase):
    def evaluate(self, client: FakeClient, **overrides):
        values = {
            "repo": "owner/repo",
            "pr_number": 7,
            "required_checks": "auto",
            "coderabbit_mode": "required",
        }
        values.update(overrides)
        return delivery_gate.evaluate(client, **values)

    def test_green_current_head_passes(self) -> None:
        result = self.evaluate(FakeClient())
        self.assertTrue(result["passed"])
        self.assertEqual(result["required_checks_source"], "branch_protection")
        self.assertTrue(result["coderabbit"]["completed"])

    def test_missing_required_check_fails_closed(self) -> None:
        client = FakeClient()
        client.required = ["verify", "secret-scan"]
        result = self.evaluate(client)
        self.assertFalse(result["passed"])
        self.assertIn("Required check is missing: secret-scan", result["blockers"])

    def test_unresolved_coderabbit_thread_blocks_even_after_success_signal(self) -> None:
        client = FakeClient()
        client.threads = [
            delivery_gate.ReviewThread(
                thread_id="T1",
                author="coderabbitai[bot]",
                path="app.py",
                url="https://example.test/thread/1",
                body="Fix this",
                is_resolved=False,
                is_outdated=False,
            )
        ]
        result = self.evaluate(client)
        self.assertFalse(result["passed"])
        self.assertEqual(result["coderabbit"]["unresolved_threads"], 1)

    def test_resolved_and_outdated_threads_do_not_block(self) -> None:
        client = FakeClient()
        client.threads = [
            delivery_gate.ReviewThread("T1", "coderabbitai[bot]", "a", "", "", True, False),
            delivery_gate.ReviewThread("T2", "human", "b", "", "", False, True),
        ]
        self.assertTrue(self.evaluate(client)["passed"])

    def test_human_change_request_on_current_head_blocks(self) -> None:
        client = FakeClient()
        client.review_items = [
            {
                "user": {"login": "reviewer"},
                "commit_id": client.head,
                "state": "CHANGES_REQUESTED",
                "submitted_at": "2026-07-31T01:00:00Z",
            }
        ]
        result = self.evaluate(client)
        self.assertFalse(result["passed"])
        self.assertEqual(result["human_change_requests"], ["reviewer"])

    def test_stale_human_change_request_does_not_block_current_head(self) -> None:
        client = FakeClient()
        client.review_items = [
            {
                "user": {"login": "reviewer"},
                "commit_id": "b" * 40,
                "state": "CHANGES_REQUESTED",
                "submitted_at": "2026-07-31T01:00:00Z",
            }
        ]
        self.assertTrue(self.evaluate(client)["passed"])

    def test_head_move_during_evaluation_blocks(self) -> None:
        client = FakeClient()
        client.refreshed_head = "c" * 40
        result = self.evaluate(client)
        self.assertFalse(result["passed"])
        self.assertTrue(any("head moved" in item for item in result["blockers"]))

    def test_auto_without_protection_infers_non_coderabbit_checks(self) -> None:
        client = FakeClient()
        client.required = []
        result = self.evaluate(client)
        self.assertTrue(result["passed"])
        self.assertEqual(result["required_checks_source"], "head_checks")
        self.assertEqual([item["name"] for item in result["required_checks"]], ["verify"])

    def test_no_ci_fails_unless_explicitly_allowed(self) -> None:
        client = FakeClient()
        client.required = []
        client.runs = [client.runs[1]]
        self.assertFalse(self.evaluate(client)["passed"])
        client.pull_calls = 0
        self.assertTrue(self.evaluate(client, allow_no_ci=True)["passed"])

    def test_unprotected_base_branch_fails_even_when_ci_is_green(self) -> None:
        client = FakeClient()
        client.protected = False
        client.required = []
        result = self.evaluate(client, required_checks="verify")
        self.assertFalse(result["passed"])
        self.assertIn("Base branch is not protected: main", result["blockers"])
        self.assertFalse(result["branch_protection"]["protected"])

    def test_configured_checks_include_every_branch_protected_context(self) -> None:
        client = FakeClient()
        client.required = ["security"]
        client.runs.append(
            {
                "name": "security",
                "status": "completed",
                "conclusion": "success",
                "completed_at": "2026-07-31T01:02:00Z",
                "app": {"slug": "github-actions", "name": "GitHub Actions"},
            }
        )
        result = self.evaluate(client, required_checks="verify")
        self.assertTrue(result["passed"])
        self.assertEqual(result["required_checks_source"], "configured+branch_protection")
        self.assertEqual(
            [item["name"] for item in result["required_checks"]],
            ["security", "verify"],
        )

    def test_spoofed_coderabbit_check_name_is_not_trusted(self) -> None:
        client = FakeClient()
        client.runs[1]["app"] = {"slug": "untrusted-app", "name": "CodeRabbit"}
        result = self.evaluate(client)
        self.assertFalse(result["passed"])
        self.assertEqual(result["coderabbit"]["state"], "missing")

    def test_current_head_coderabbit_review_is_a_trusted_completion_signal(self) -> None:
        client = FakeClient()
        client.runs = client.runs[:1]
        client.review_items = [
            {
                "user": {"login": "coderabbitai[bot]"},
                "commit_id": client.head,
                "state": "COMMENTED",
                "submitted_at": "2026-07-31T01:02:00Z",
            }
        ]
        result = self.evaluate(client)
        self.assertTrue(result["passed"])
        self.assertEqual(result["coderabbit"]["signal"], "review")

    def test_coderabbit_changes_requested_review_does_not_pass(self) -> None:
        client = FakeClient()
        client.runs = client.runs[:1]
        client.review_items = [
            {
                "user": {"login": "coderabbitai[bot]"},
                "commit_id": client.head,
                "state": "CHANGES_REQUESTED",
                "submitted_at": "2026-07-31T01:02:00Z",
            }
        ]
        result = self.evaluate(client)
        self.assertFalse(result["passed"])
        self.assertEqual(result["coderabbit"]["state"], "changes_requested")

    def test_unresolved_human_thread_blocks(self) -> None:
        client = FakeClient()
        client.threads = [
            delivery_gate.ReviewThread(
                "T-human", "reviewer", "app.py", "", "Please fix", False, False
            )
        ]
        result = self.evaluate(client)
        self.assertFalse(result["passed"])
        self.assertEqual(len(result["unresolved_threads"]), 1)

    def test_green_fixture_cli_smoke(self) -> None:
        fixture = pathlib.Path(__file__).with_name("fixtures") / "green-pr.json"
        result = subprocess.run(
            [
                sys.executable,
                str(MODULE_PATH),
                "--repo",
                "owner/repo",
                "--pr",
                "1",
                "--fixture",
                str(fixture),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"passed": true', result.stdout)


class GhClientTests(unittest.TestCase):
    def test_check_run_pagination_works_without_slurp(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            calls.append(command)
            endpoint = command[2]
            page = [
                {"name": f"check-{index}"}
                for index in range(delivery_gate.REST_PAGE_SIZE)
            ]
            if "page=2" in endpoint:
                page = [{"name": "last"}]
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=__import__("json").dumps({"check_runs": page}),
                stderr="",
            )

        with mock.patch.object(delivery_gate.subprocess, "run", side_effect=fake_run):
            runs = delivery_gate.GhClient().check_runs("owner/repo", "a" * 40)

        self.assertEqual(len(runs), delivery_gate.REST_PAGE_SIZE + 1)
        self.assertEqual(runs[-1]["name"], "last")
        self.assertTrue(all("--slurp" not in call for call in calls))
        self.assertEqual(len(calls), 2)

    def test_missing_branch_protection_is_explicit(self) -> None:
        completed = subprocess.CompletedProcess(
            ["gh", "api"],
            1,
            stdout="",
            stderr="gh: Branch not protected (HTTP 404)",
        )
        with mock.patch.object(delivery_gate.subprocess, "run", return_value=completed):
            protection = delivery_gate.GhClient().branch_protection(
                "owner/repo", "main"
            )
        self.assertFalse(protection.protected)
        self.assertEqual(protection.required_contexts, ())


if __name__ == "__main__":
    unittest.main()
