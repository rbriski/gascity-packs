from __future__ import annotations

import json
import os
import pathlib
import subprocess
import tempfile
import unittest


PACK_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = PACK_ROOT / "assets" / "scripts" / "checks" / "delivery-preflight.sh"
RELEASE_VERIFIED_SCRIPT = (
    PACK_ROOT / "assets" / "scripts" / "checks" / "delivery-release-verified.sh"
)


class PreflightTests(unittest.TestCase):
    def metadata(self, **overrides: str) -> dict[str, str]:
        values = {
            "gc.var.push": "true",
            "gc.var.open_pr": "true",
            "gc.var.required_checks": "verify",
            "gc.var.coderabbit": "required",
            "gc.var.allow_no_ci": "false",
            "gc.var.allow_no_local_gates": "false",
            "gc.var.allow_no_smoke": "false",
            "gc.var.setup_command": "/bin/true",
            "gc.var.base_branch": "main",
            "gc.var.merge_method": "squash",
            "gc.var.deploy_mode": "command",
            "gc.var.deploy_command": "/bin/true",
            "gc.var.deploy_verify_command": "/bin/true",
            "gc.var.smoke_command": "/bin/true",
            "gc.var.production_url": "https://service.example.test",
            "gc.var.source_bead_id": "fi-123",
            "gc.var.source_title": "Requested delivery",
        }
        values.update(overrides)
        return values

    def run_preflight(
        self,
        metadata: dict[str, str],
        *,
        protected: bool = True,
        step_json: str | None = None,
        root_json: str | None = None,
        transient_gc_failures: int = 0,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            repository = root / "repo"
            repository.mkdir()
            subprocess.run(
                ["git", "init", "-q", str(repository)], check=True, capture_output=True
            )
            bin_dir = root / "bin"
            bin_dir.mkdir()
            gc = bin_dir / "gc"
            gc.write_text(
                "#!/bin/sh\n"
                "if [ -n \"${FAKE_GC_FAILURES_FILE:-}\" ] && [ -f \"$FAKE_GC_FAILURES_FILE\" ]; then\n"
                "  failures=$(cat \"$FAKE_GC_FAILURES_FILE\")\n"
                "  if [ \"$failures\" -gt 0 ]; then\n"
                "    printf '%s\\n' $((failures - 1)) > \"$FAKE_GC_FAILURES_FILE\"\n"
                "    exit 1\n"
                "  fi\n"
                "fi\n"
                "if [ \"${3:-}\" = \"${FAKE_GC_ROOT_ID:-}\" ]; then\n"
                "  printf '%s\\n' \"$FAKE_GC_ROOT_JSON\"\n"
                "else\n"
                "  printf '%s\\n' \"$FAKE_GC_STEP_JSON\"\n"
                "fi\n",
                encoding="utf-8",
            )
            gc.chmod(0o755)
            gh = bin_dir / "gh"
            gh.write_text(
                "#!/bin/sh\n"
                "if [ \"${1:-}\" = api ] && [ \"$FAKE_GH_PROTECTED\" != true ]; then\n"
                "  printf '%s\\n' 'gh: Branch not protected (HTTP 404)' >&2\n"
                "  exit 1\n"
                "fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            gh.chmod(0o755)
            environment = os.environ.copy()
            failures_file = root / "gc-failures"
            failures_file.write_text(str(transient_gc_failures), encoding="utf-8")
            environment.update(
                {
                    "GC_BEAD_ID": "step-1",
                    "GC_WORK_DIR": str(repository),
                    "FAKE_GC_STEP_JSON": step_json
                    or json.dumps([{"metadata": metadata}]),
                    "FAKE_GC_ROOT_ID": "root-1" if root_json is not None else "",
                    "FAKE_GC_ROOT_JSON": root_json or "",
                    "FAKE_GH_PROTECTED": str(protected).lower(),
                    "FAKE_GC_FAILURES_FILE": str(failures_file),
                    "PATH": f"{bin_dir}:{environment['PATH']}",
                }
            )
            return subprocess.run(
                ["bash", str(SCRIPT)],
                capture_output=True,
                text=True,
                env=environment,
            )

    def test_complete_command_profile_passes(self) -> None:
        result = self.run_preflight(self.metadata())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CodeRabbit=required", result.stdout)
        self.assertIn("deploy=command", result.stdout)
        self.assertIn("source=fi-123", result.stdout)

    def test_missing_source_intent_fails_before_planning(self) -> None:
        result = self.run_preflight(
            self.metadata(**{"gc.var.source_bead_id": "", "gc.var.source_title": ""})
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("source_bead_id is required", result.stderr)
        self.assertIn("source_title is required", result.stderr)

    def test_missing_deploy_and_verify_are_reported_together(self) -> None:
        result = self.run_preflight(
            self.metadata(
                **{
                    "gc.var.deploy_command": "",
                    "gc.var.deploy_verify_command": "",
                }
            )
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("deploy_command is required", result.stderr)
        self.assertIn("deploy_verify_command is required", result.stderr)

    def test_not_applicable_requires_reason_but_not_smoke(self) -> None:
        values = self.metadata(
            **{
                "gc.var.deploy_mode": "not-applicable",
                "gc.var.deploy_command": "",
                "gc.var.deploy_verify_command": "",
                "gc.var.smoke_command": "",
                "gc.var.deploy_not_applicable_reason": "Documentation-only artifact",
            }
        )
        result = self.run_preflight(values)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("deploy=not-applicable", result.stdout)

    def test_unsafe_production_url_fails(self) -> None:
        result = self.run_preflight(
            self.metadata(**{"gc.var.production_url": "javascript:alert(1)"})
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("production_url must be an https URL", result.stderr)

    def test_production_url_without_authority_fails(self) -> None:
        result = self.run_preflight(
            self.metadata(**{"gc.var.production_url": "https:///release"})
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("production_url must be an https URL", result.stderr)

    def test_production_url_requires_hostname_and_valid_port(self) -> None:
        for production_url in (
            "https://@/release",
            "https://:443/release",
            "https://service.example.test:not-a-port/release",
        ):
            with self.subTest(production_url=production_url):
                result = self.run_preflight(
                    self.metadata(**{"gc.var.production_url": production_url})
                )
                self.assertEqual(result.returncode, 1)
                self.assertIn("production_url must be an https URL", result.stderr)

    def test_invalid_step_or_root_json_fails_closed(self) -> None:
        for metadata, kwargs, bead_id in (
            (self.metadata(), {"step_json": "not json"}, "step-1"),
            (
                self.metadata(**{"gc.root_bead_id": "root-1"}),
                {"root_json": "not json"},
                "root-1",
            ),
        ):
            with self.subTest(bead_id=bead_id):
                result = self.run_preflight(metadata, **kwargs)
                self.assertEqual(result.returncode, 1)
                self.assertIn(
                    f"gc bd show {bead_id} returned invalid JSON", result.stderr
                )

    def test_transient_bead_read_failure_is_retried(self) -> None:
        result = self.run_preflight(self.metadata(), transient_gc_failures=1)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_persistent_bead_read_failure_fails_closed(self) -> None:
        result = self.run_preflight(self.metadata(), transient_gc_failures=3)
        self.assertEqual(result.returncode, 1)
        self.assertIn("gc bd show step-1 failed", result.stderr)

    def test_malformed_required_check_list_fails_early(self) -> None:
        result = self.run_preflight(
            self.metadata(**{"gc.var.required_checks": "verify,,verify"})
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("unique, nonempty exact check names", result.stderr)

    def test_unprotected_base_branch_fails_preflight(self) -> None:
        result = self.run_preflight(self.metadata(), protected=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("base branch must be protected", result.stderr)

    def test_release_verification_requires_metadata_before_running_commands(self) -> None:
        for missing_key in ("delivery.repo", "delivery.pr_number"):
            with self.subTest(missing_key=missing_key), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                repository = root / "repo"
                repository.mkdir()
                evidence = root / "evidence.txt"
                evidence.write_text("verified\n", encoding="utf-8")
                command_marker = root / "command-ran"
                metadata = {
                    "delivery.merge_sha": "a" * 40,
                    "delivery.deployed_sha": "a" * 40,
                    "delivery.deploy_status": "verified",
                    "delivery.deploy_evidence_path": str(evidence),
                    "delivery.verify_evidence_path": str(evidence),
                    "delivery.repo": "example/repo",
                    "delivery.pr_number": "123",
                    "gc.var.deploy_mode": "command",
                    "gc.var.deploy_command": "/bin/true",
                    "gc.var.deploy_verify_command": "touch \"$COMMAND_MARKER\"",
                    "gc.var.smoke_command": "touch \"$COMMAND_MARKER\"",
                }
                metadata[missing_key] = ""
                bin_dir = root / "bin"
                bin_dir.mkdir()
                gc = bin_dir / "gc"
                gc.write_text(
                    '#!/bin/sh\nprintf "%s\\n" "$FAKE_GC_JSON"\n', encoding="utf-8"
                )
                gc.chmod(0o755)
                environment = os.environ.copy()
                environment.update(
                    {
                        "GC_BEAD_ID": "step-1",
                        "GC_WORK_DIR": str(repository),
                        "FAKE_GC_JSON": json.dumps([{"metadata": metadata}]),
                        "COMMAND_MARKER": str(command_marker),
                        "PATH": f"{bin_dir}:{environment['PATH']}",
                    }
                )
                result = subprocess.run(
                    ["bash", str(RELEASE_VERIFIED_SCRIPT)],
                    capture_output=True,
                    text=True,
                    env=environment,
                )
                self.assertEqual(result.returncode, 1)
                self.assertIn(f"{missing_key} is missing", result.stderr)
                self.assertFalse(command_marker.exists())


if __name__ == "__main__":
    unittest.main()
