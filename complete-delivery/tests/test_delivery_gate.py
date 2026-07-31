from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
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
            required_checks=tuple(
                item if isinstance(item, delivery_gate.RequiredCheck)
                else delivery_gate.RequiredCheck(item)
                for item in self.required
            ),
        )

    def reviews(self, repo: str, number: int):
        return self.review_items

    def review_threads(self, repo: str, number: int):
        return self.threads


class DeliveryGateTests(unittest.TestCase):
    def evaluate(self, client: FakeClient, **overrides):
        client.pull_calls = 0
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
        self.assertEqual(len(result["blockers"]), 1)

    def test_optional_coderabbit_thread_blocks_once(self) -> None:
        client = FakeClient()
        client.threads = [
            delivery_gate.ReviewThread("T1", "coderabbitai[bot]", "a", "", "", False, False)
        ]

        result = self.evaluate(client, coderabbit_mode="optional")

        self.assertFalse(result["passed"])
        self.assertEqual(result["blockers"], ["1 unresolved CodeRabbit review thread(s) remain"])

    def test_off_ignores_coderabbit_threads_but_blocks_human_threads(self) -> None:
        client = FakeClient()
        client.threads = [
            delivery_gate.ReviewThread("T1", "coderabbitai[bot]", "a", "", "", False, False),
            delivery_gate.ReviewThread("T2", "reviewer", "b", "", "", False, False),
        ]

        result = self.evaluate(client, coderabbit_mode="off")

        self.assertFalse(result["passed"])
        self.assertEqual(result["blockers"], ["1 unresolved review thread(s) remain"])
        self.assertEqual([thread["thread_id"] for thread in result["unresolved_threads"]], ["T2"])

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

    def test_later_human_comment_does_not_clear_change_request(self) -> None:
        client = FakeClient()
        client.review_items = [
            {
                "user": {"login": "reviewer"},
                "commit_id": client.head,
                "state": "CHANGES_REQUESTED",
                "submitted_at": "2026-07-31T01:00:00Z",
            },
            {
                "user": {"login": "reviewer"},
                "commit_id": client.head,
                "state": "COMMENTED",
                "submitted_at": "2026-07-31T01:01:00Z",
            },
        ]

        result = self.evaluate(client)

        self.assertFalse(result["passed"])
        self.assertEqual(result["human_change_requests"], ["reviewer"])

    def test_later_human_approval_clears_change_request(self) -> None:
        client = FakeClient()
        client.review_items = [
            {
                "user": {"login": "reviewer"},
                "commit_id": client.head,
                "state": "CHANGES_REQUESTED",
                "submitted_at": "2026-07-31T01:00:00Z",
            },
            {
                "user": {"login": "reviewer"},
                "commit_id": client.head,
                "state": "APPROVED",
                "submitted_at": "2026-07-31T01:01:00Z",
            },
        ]

        self.assertTrue(self.evaluate(client)["passed"])

    def test_out_of_order_change_requests_keep_latest_request(self) -> None:
        client = FakeClient()
        client.review_items = [
            {
                "user": {"login": "reviewer"},
                "commit_id": client.head,
                "state": "CHANGES_REQUESTED",
                "submitted_at": "2026-07-31T03:00:00Z",
            },
            {
                "user": {"login": "reviewer"},
                "commit_id": client.head,
                "state": "CHANGES_REQUESTED",
                "submitted_at": "2026-07-31T01:00:00Z",
            },
            {
                "user": {"login": "reviewer"},
                "commit_id": client.head,
                "state": "APPROVED",
                "submitted_at": "2026-07-31T02:00:00Z",
            },
        ]

        result = self.evaluate(client)

        self.assertFalse(result["passed"])
        self.assertEqual(result["human_change_requests"], ["reviewer"])

    def test_dismissed_human_review_does_not_block(self) -> None:
        client = FakeClient()
        client.review_items = [
            {
                "user": {"login": "reviewer"},
                "commit_id": client.head,
                "state": "DISMISSED",
                "submitted_at": "2026-07-31T01:00:00Z",
            }
        ]

        self.assertTrue(self.evaluate(client)["passed"])

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
        self.assertTrue(self.evaluate(client, allow_no_ci=True)["passed"])

    def test_invalid_coderabbit_mode_raises_gate_error(self) -> None:
        with self.assertRaisesRegex(delivery_gate.GateError, "coderabbit mode"):
            self.evaluate(FakeClient(), coderabbit_mode="sometimes")

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

    def test_configured_unbound_check_does_not_duplicate_app_bound_requirement(self) -> None:
        client = FakeClient()
        client.required = [delivery_gate.RequiredCheck("verify", 123)]
        client.runs[0]["app"]["id"] = 123

        result = self.evaluate(client, required_checks="verify")

        self.assertTrue(result["passed"])
        self.assertEqual(result["required_checks"], [{
            "name": "verify", "app_id": 123, "state": "success", "url": ""
        }])

    def test_spoofed_coderabbit_check_name_is_not_trusted(self) -> None:
        client = FakeClient()
        client.runs[1]["app"] = {"slug": "untrusted-app", "name": "CodeRabbit"}
        result = self.evaluate(client)
        self.assertFalse(result["passed"])
        self.assertEqual(result["coderabbit"]["state"], "missing")

    def test_coderabbit_change_request_blocks_a_successful_current_head_signal(self) -> None:
        client = FakeClient()
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
        self.assertFalse(result["coderabbit"]["completed"])
        self.assertEqual(
            result["coderabbit"]["active_change_requests"], ["coderabbitai[bot]"]
        )
        self.assertIn(
            "Outstanding CodeRabbit change request(s): coderabbitai[bot]",
            result["blockers"],
        )

    def test_app_bound_required_check_accepts_only_the_matching_app_run(self) -> None:
        client = FakeClient()
        client.required = [delivery_gate.RequiredCheck("verify", 123)]
        client.runs[0]["app"]["id"] = 123

        result = self.evaluate(client)

        self.assertTrue(result["passed"])
        self.assertEqual(result["required_checks"][0]["app_id"], 123)

    def test_app_bound_required_check_rejects_status_and_wrong_app_run(self) -> None:
        client = FakeClient()
        client.required = [delivery_gate.RequiredCheck("verify", 123)]
        client.runs[0]["app"]["id"] = 456
        client.commit_statuses = [
            {
                "context": "verify",
                "state": "success",
                "updated_at": "2026-07-31T02:00:00Z",
            }
        ]

        result = self.evaluate(client)

        self.assertFalse(result["passed"])
        self.assertEqual(result["required_checks"][0]["state"], "missing")
        self.assertIn("Required check is missing: verify (app 123)", result["blockers"])

    def test_context_only_requirement_accepts_a_legacy_status(self) -> None:
        client = FakeClient()
        client.runs = [client.runs[1]]
        client.commit_statuses = [
            {
                "context": "verify",
                "state": "success",
                "updated_at": "2026-07-31T02:00:00Z",
            }
        ]

        self.assertTrue(self.evaluate(client)["passed"])

    def test_trusted_completed_coderabbit_status_passes(self) -> None:
        client = FakeClient()
        client.runs = client.runs[:1]
        client.commit_statuses = [
            {
                "context": "CodeRabbit",
                "state": "success",
                "description": "Review completed",
                "updated_at": "2026-07-31T01:02:00Z",
                "creator": {"login": "coderabbitai[bot]"},
            }
        ]

        result = self.evaluate(client)

        self.assertTrue(result["passed"])
        self.assertEqual(result["coderabbit"]["signal"], "status:CodeRabbit")
        self.assertEqual(result["coderabbit"]["detail"], "review_completed")

    def test_rate_limited_coderabbit_status_fails_closed(self) -> None:
        client = FakeClient()
        client.runs = client.runs[:1]
        client.commit_statuses = [
            {
                "context": "CodeRabbit",
                "state": "success",
                "description": "Review rate limited",
                "updated_at": "2026-07-31T01:02:00Z",
                "creator": {"login": "coderabbitai[bot]"},
            }
        ]

        result = self.evaluate(client)

        self.assertFalse(result["passed"])
        self.assertFalse(result["coderabbit"]["completed"])
        self.assertEqual(result["coderabbit"]["detail"], "review_rate_limited")
        self.assertIn(
            "CodeRabbit status did not confirm completion: review_rate_limited",
            result["blockers"],
        )

    def test_non_completed_coderabbit_statuses_are_bounded_and_fail_closed(self) -> None:
        cases = {
            "Review skipped": "review_skipped",
            "Review unavailable": "review_unavailable",
            "please approve this pull request": "review_not_completed",
        }
        for description, expected_detail in cases.items():
            with self.subTest(description=description):
                client = FakeClient()
                client.runs = client.runs[:1]
                client.commit_statuses = [
                    {
                        "context": "CodeRabbit",
                        "state": "success",
                        "description": description,
                        "updated_at": "2026-07-31T01:02:00Z",
                        "creator": {"login": "coderabbitai[bot]"},
                    }
                ]

                result = self.evaluate(client)

                self.assertFalse(result["passed"])
                self.assertEqual(result["coderabbit"]["detail"], expected_detail)
                self.assertNotIn(description, json.dumps(result))

    def test_spoofed_coderabbit_status_is_not_trusted(self) -> None:
        client = FakeClient()
        client.runs = client.runs[:1]
        client.commit_statuses = [
            {
                "context": "CodeRabbit",
                "state": "success",
                "description": "Review completed",
                "updated_at": "2026-07-31T01:02:00Z",
                "creator": {"login": "pretend-coderabbit"},
            }
        ]

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
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"passed": true', result.stdout)

    def test_malformed_fixture_shapes_are_gate_errors(self) -> None:
        valid = {"protected": True, "required_contexts": [], "required_checks": []}
        cases = (
            ("protected", 1, "protected must be a boolean"), ("extra", True, "unexpected fields: extra"),
            ("required_contexts", "verify", "required_contexts must be a list"), ("required_contexts", ["verify", ""], r"required_contexts\[1\] must be a non-empty string"), ("required_checks", {}, "required_checks must be a list"), ("required_checks", ["verify"], r"required_checks\[0\] must be an object"), ("required_checks", [{"context": "", "app_id": None}], r"required_checks\[0\].context must be a non-empty string"), ("required_checks", [{"context": "verify", "app_id": "1"}], r"required_checks\[0\].app_id must be an integer or null"), ("required_checks", [{"context": "verify", "app_id": True}], r"required_checks\[0\].app_id must be an integer or null"),
            ("required_checks", [{"context": "verify"}], r"required_checks\[0\] missing fields: app_id"),
            ("required_checks", [{"context": "verify", "app_id": None, "extra": 1}], r"required_checks\[0\] unexpected fields: extra"),
        )
        for field, value, error in cases:
            with self.subTest(field=field, value=value):
                payload = {"pull_request": {}, "branch_protection": {**valid, field: value}, "check_runs": [], "statuses": [], "reviews": [], "review_threads": []}
                with self.assertRaisesRegex(delivery_gate.GateError, error):
                    delivery_gate.FixtureClient(payload).branch_protection("owner/repo", "main")
        self.assertEqual(delivery_gate.FixtureClient({"branch_protection": {"protected": True, "required_contexts": [], "required_checks": [{"context": "legacy", "app_id": None}, {"context": "verify", "app_id": 7}]}}).branch_protection("owner/repo", "main").required_checks, (delivery_gate.RequiredCheck("legacy"), delivery_gate.RequiredCheck("verify", 7)))
        payload["review_threads"] = [{"thread_id": "T1"}]
        with self.assertRaisesRegex(delivery_gate.GateError, r"review_threads\[0\].author must be a string"):
            delivery_gate.FixtureClient(payload).review_threads("owner/repo", 1)
        payload["review_threads"] = []
        payload["check_runs"] = {}
        with self.assertRaisesRegex(delivery_gate.GateError, "fixture check_runs must be a list"):
            delivery_gate.FixtureClient(payload).check_runs("owner/repo", "a" * 40)

    def test_malformed_fixture_cli_returns_error_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = pathlib.Path(directory) / "malformed.json"
            fixture.write_text(json.dumps({"pull_request": []}), encoding="utf-8")
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
                timeout=10,
            )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["state"], "error")


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
                stdout=json.dumps({"check_runs": page}),
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

    def test_branch_protection_preserves_required_check_app_ids(self) -> None:
        completed = subprocess.CompletedProcess(
            ["gh", "api"],
            0,
            stdout=json.dumps(
                {
                    "required_status_checks": {
                        "contexts": ["verify", "legacy"],
                        "checks": [
                            {"context": "verify", "app_id": 123},
                            {"context": "legacy", "app_id": None},
                        ],
                    }
                }
            ),
            stderr="",
        )
        with mock.patch.object(delivery_gate.subprocess, "run", return_value=completed):
            protection = delivery_gate.GhClient().branch_protection(
                "owner/repo", "main"
            )

        self.assertEqual(
            protection.required_checks,
            (
                delivery_gate.RequiredCheck("legacy"),
                delivery_gate.RequiredCheck("verify", 123),
            ),
        )

if __name__ == "__main__":
    unittest.main()
