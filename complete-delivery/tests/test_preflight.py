from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import tomllib
import unittest


PACK_ROOT = pathlib.Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PACK_ROOT.parent
SCRIPT = PACK_ROOT / "assets" / "scripts" / "checks" / "delivery-preflight.sh"
RELEASE_VERIFIED_SCRIPT = (
    PACK_ROOT / "assets" / "scripts" / "checks" / "delivery-release-verified.sh"
)
PR_OPEN_SCRIPT = PACK_ROOT / "assets" / "scripts" / "checks" / "delivery-pr-open.sh"
MERGED_SCRIPT = PACK_ROOT / "assets" / "scripts" / "checks" / "delivery-merged.sh"
SOURCE_ARTIFACT_SCRIPT = (
    PACK_ROOT / "assets" / "scripts" / "checks" / "delivery-source-artifact-valid.sh"
)
SOURCE_ARTIFACT_GENERIC_CHECK = (
    REPOSITORY_ROOT / "gascity" / "assets" / "scripts" / "checks" / "build-artifact-valid.sh"
)
FORMULA_PATH = PACK_ROOT / "formulas" / "complete-delivery.formula.toml"
WORKFLOW_ROOT = PACK_ROOT / "assets" / "workflows" / "complete-delivery"
PR_GATE_WORKFLOW_ROOT = (
    PACK_ROOT / "assets" / "workflows" / "complete-delivery-pr-gate"
)


def command_deploy_fixture(
    repository: pathlib.Path,
    merge_sha: str,
    *,
    command: str = "/bin/true",
    timeout: str = "5m",
) -> tuple[dict[str, str], pathlib.Path]:
    delivery_dir = repository / "artifacts" / "delivery"
    delivery_dir.mkdir(parents=True, exist_ok=True)
    deploy_log = delivery_dir / "deploy.log"
    verify_log = delivery_dir / "verify.log"
    stdout = delivery_dir / "deploy.stdout.log"
    stderr = delivery_dir / "deploy.stderr.log"
    stdout.write_text("", encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    verify_log.write_text("verified\n", encoding="utf-8")
    label = "sha256:" + hashlib.sha256(command.encode()).hexdigest()
    deploy_log.write_text(
        "\n".join(
            (
                "schema=complete-delivery.deploy.v1",
                f"command_label={label}",
                f"timeout={timeout}",
                "outcome=passed",
                "child_status=0",
                "wrapper_status=0",
                f"merge_sha={merge_sha}",
                f"stdout_path={stdout}",
                f"stderr_path={stderr}",
                "",
            )
        ),
        encoding="utf-8",
    )
    return (
        {
            "delivery.deployed_sha": merge_sha,
            "delivery.deploy_status": "verified",
            "delivery.deploy_evidence_path": str(deploy_log),
            "delivery.verify_evidence_path": str(verify_log),
            "delivery.deploy_command_label": label,
            "delivery.deploy_timeout": timeout,
            "delivery.deploy_outcome": "passed",
            "delivery.deploy_child_status": "0",
            "delivery.deploy_wrapper_status": "0",
            "delivery.deploy_merge_sha": merge_sha,
            "delivery.deploy_stdout_path": str(stdout),
            "delivery.deploy_stderr_path": str(stderr),
            "gc.var.artifact_root": "artifacts",
            "gc.var.deploy_mode": "command",
            "gc.var.deploy_command": command,
            "gc.var.deploy_timeout": timeout,
        },
        verify_log,
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
        source_json: str | None = None,
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
                "    printf 'simulated gc failure: %s\\n' \"$failures\" >&2\n"
                "    exit 1\n"
                "  fi\n"
                "fi\n"
                "if [ \"${3:-}\" = \"${FAKE_GC_SOURCE_ID:-}\" ]; then\n"
                "  printf '%s\\n' \"$FAKE_GC_SOURCE_JSON\"\n"
                "elif [ \"${3:-}\" = \"${FAKE_GC_ROOT_ID:-}\" ]; then\n"
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
                    "FAKE_GC_SOURCE_ID": metadata.get("gc.var.source_bead_id", ""),
                    "FAKE_GC_SOURCE_JSON": source_json
                    or json.dumps([{"title": metadata.get("gc.var.source_title", "")}]),
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

    def test_source_title_rejects_whitespace_and_control_characters(self) -> None:
        whitespace = self.run_preflight(
            self.metadata(**{"gc.var.source_title": " \t "})
        )
        self.assertEqual(whitespace.returncode, 1)
        self.assertIn("source_title is required", whitespace.stderr)

        for control in ("\n", "\r", "\t", "\x00", "\x7f"):
            with self.subTest(control=repr(control)):
                title = f"Requested{control}delivery"
                result = self.run_preflight(
                    self.metadata(**{"gc.var.source_title": title}),
                    source_json=json.dumps([{"title": title}]),
                )
                self.assertEqual(result.returncode, 1)
                self.assertIn(
                    "source_bead_id must resolve to one durable source with a title",
                    result.stderr,
                )

    def test_invalid_source_bead_id_fails_closed(self) -> None:
        result = self.run_preflight(
            self.metadata(**{"gc.var.source_bead_id": "fi/123"})
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("source_bead_id must be a valid durable bead or convoy ID", result.stderr)

    def test_unreadable_or_mismatched_durable_source_fails_closed(self) -> None:
        unreadable = self.run_preflight(
            self.metadata(), source_json="not json"
        )
        self.assertEqual(unreadable.returncode, 1)
        self.assertIn("source_bead_id must resolve to a readable durable bead or convoy", unreadable.stderr)

        mismatched = self.run_preflight(
            self.metadata(), source_json=json.dumps([{"title": "Different source"}])
        )
        self.assertEqual(mismatched.returncode, 1)
        self.assertIn("source_title must exactly match the resolved durable source title", mismatched.stderr)

    def test_source_fixture_uses_overridden_metadata(self) -> None:
        result = self.run_preflight(
            self.metadata(
                **{
                    "gc.var.source_bead_id": "alternate-456",
                    "gc.var.source_title": "Alternate delivery",
                }
            )
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("source=alternate-456", result.stdout)

    def test_zero_local_gates_fail_closed_without_opt_out(self) -> None:
        result = self.run_preflight(self.metadata(**{"gc.var.setup_command": ""}))
        self.assertEqual(result.returncode, 1)
        self.assertIn("configure at least one repository gate", result.stderr)

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

    def test_whitespace_only_commands_fail_closed(self) -> None:
        for key in (
            "gc.var.deploy_command",
            "gc.var.deploy_verify_command",
            "gc.var.smoke_command",
        ):
            with self.subTest(key=key):
                result = self.run_preflight(self.metadata(**{key: " \t "}))
                self.assertEqual(result.returncode, 1)
                self.assertIn(
                    f"{key.removeprefix('gc.var.')} is required", result.stderr
                )

    def test_timeouts_must_be_positive_finite_and_at_most_one_hour(self) -> None:
        for key in (
            "gc.var.deploy_timeout",
            "gc.var.deploy_verify_timeout",
            "gc.var.smoke_timeout",
        ):
            for value in ("0", "0s", "inf", "infinity", "-1s", "5x", "--foreground", "3601s", "2h"):
                with self.subTest(key=key, value=value):
                    result = self.run_preflight(self.metadata(**{key: value}))
                    self.assertEqual(result.returncode, 1)
                    self.assertIn(
                        f"{key.removeprefix('gc.var.')} must be a positive finite duration no greater than 1h",
                        result.stderr,
                    )

        for value in ("0.01s", "5m", "1h", ".5h"):
            with self.subTest(value=value):
                result = self.run_preflight(
                    self.metadata(
                        **{
                            "gc.var.deploy_timeout": value,
                            "gc.var.deploy_verify_timeout": value,
                            "gc.var.smoke_timeout": value,
                        }
                    )
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_smoke_timeout_is_validated_only_when_smoke_runs(self) -> None:
        for command in ("", " \t "):
            with self.subTest(command=repr(command)):
                without_smoke = self.run_preflight(
                    self.metadata(
                        **{
                            "gc.var.smoke_command": command,
                            "gc.var.allow_no_smoke": "true",
                            "gc.var.no_smoke_reason": "No production endpoint is exposed",
                            "gc.var.smoke_timeout": "0s",
                        }
                    )
                )
                self.assertEqual(without_smoke.returncode, 0, without_smoke.stderr)

        with_smoke = self.run_preflight(
            self.metadata(**{"gc.var.smoke_timeout": "0s"})
        )
        self.assertEqual(with_smoke.returncode, 1)
        self.assertIn("smoke_timeout must be a positive finite duration", with_smoke.stderr)

    def test_allow_no_smoke_requires_a_nonblank_reason(self) -> None:
        for reason in ("", " \t "):
            with self.subTest(reason=repr(reason)):
                result = self.run_preflight(
                    self.metadata(
                        **{
                            "gc.var.allow_no_smoke": "true",
                            "gc.var.no_smoke_reason": reason,
                        }
                    )
                )
                self.assertEqual(result.returncode, 1)
                self.assertIn(
                    "no_smoke_reason is required and must be nonblank",
                    result.stderr,
                )

        valid = self.run_preflight(
            self.metadata(
                **{
                    "gc.var.allow_no_smoke": "true",
                    "gc.var.no_smoke_reason": "No production endpoint is exposed",
                }
            )
        )
        self.assertEqual(valid.returncode, 0, valid.stderr)

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

        for reason in ("", " \t "):
            with self.subTest(reason=repr(reason)):
                invalid = self.run_preflight(
                    {**values, "gc.var.deploy_not_applicable_reason": reason}
                )
                self.assertEqual(invalid.returncode, 1)
                self.assertIn(
                    "deploy_not_applicable_reason is required and must be nonblank",
                    invalid.stderr,
                )

    def test_ci_deploy_requires_exact_workflow_and_environment(self) -> None:
        values = self.metadata(
            **{
                "gc.var.deploy_mode": "ci",
                "gc.var.deploy_command": "",
                "gc.var.deploy_ci_workflow": ".github/workflows/deploy.yml",
                "gc.var.deploy_environment": "production",
            }
        )
        valid = self.run_preflight(values)
        self.assertEqual(valid.returncode, 0, valid.stderr)

        for key, value in (
            ("gc.var.deploy_ci_workflow", ""),
            ("gc.var.deploy_ci_workflow", ".github/workflows/../deploy.yml"),
            ("gc.var.deploy_ci_workflow", "deploy.yml"),
            ("gc.var.deploy_environment", ""),
            ("gc.var.deploy_environment", " production "),
            ("gc.var.deploy_environment", "production\nforged"),
        ):
            with self.subTest(key=key, value=repr(value)):
                invalid = self.run_preflight({**values, key: value})
                self.assertEqual(invalid.returncode, 1)
                self.assertIn(
                    "deploy_mode=ci requires a .github/workflows/*.yml deploy_ci_workflow and nonblank deploy_environment",
                    invalid.stderr,
                )

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
        self.assertIn("simulated gc failure: 1", result.stderr)
        self.assertNotIn("simulated gc failure: 3", result.stderr)

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
                evidence = repository / "evidence.txt"
                evidence.write_text("verified\n", encoding="utf-8")
                command_marker = root / "command-ran"
                metadata = {
                    "gc.step_ref": "complete-delivery.verify-production",
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

    def test_not_applicable_release_verification_requires_evidence_and_no_smoke_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            repository = root / "repo"
            repository.mkdir()
            bin_dir = root / "bin"
            bin_dir.mkdir()
            gc = bin_dir / "gc"
            gc.write_text('#!/bin/sh\nprintf "%s\\n" "$FAKE_GC_JSON"\n', encoding="utf-8")
            gc.chmod(0o755)
            artifact_delivery = repository / "artifacts" / "delivery"
            artifact_delivery.mkdir(parents=True)
            base_metadata = {
                "gc.step_ref": "complete-delivery.verify-production",
                "delivery.merge_sha": "a" * 40,
                "delivery.deploy_status": "not_applicable",
                "gc.var.deploy_mode": "not-applicable",
                "gc.var.deploy_not_applicable_reason": "Documentation-only artifact",
                "gc.var.artifact_root": "artifacts",
            }
            environment = os.environ.copy()
            environment.update({"GC_BEAD_ID": "step-1", "GC_WORK_DIR": str(repository), "PATH": f"{bin_dir}:{environment['PATH']}"})
            def run_for_both_steps(metadata: dict[str, str]) -> list[subprocess.CompletedProcess[str]]:
                results = []
                for step_ref in ("complete-delivery.deploy", "complete-delivery.verify-production"):
                    environment["FAKE_GC_JSON"] = json.dumps([{"metadata": {**metadata, "gc.step_ref": step_ref}}])
                    results.append(subprocess.run(["bash", str(RELEASE_VERIFIED_SCRIPT)], capture_output=True, text=True, env=environment))
                return results
            for evidence, message in (("", "delivery.deploy_evidence_path is missing"), ("artifacts/delivery/empty.log", "deploy evidence is missing, not a file, or empty")):
                with self.subTest(evidence=evidence):
                    metadata = {**base_metadata, "delivery.deploy_evidence_path": evidence}
                    if evidence:
                        (repository / evidence).write_text("", encoding="utf-8")
                    for result in run_for_both_steps(metadata):
                        self.assertEqual(result.returncode, 1)
                        self.assertIn(message, result.stderr)

            directory_evidence = artifact_delivery / "directory-evidence"
            directory_evidence.mkdir()
            (directory_evidence / "entry").write_text("not a regular file\n", encoding="utf-8")
            for result in run_for_both_steps({**base_metadata, "delivery.deploy_evidence_path": str(directory_evidence)}):
                self.assertEqual(result.returncode, 1)
                self.assertIn("deploy evidence is missing, not a file, or empty", result.stderr)

            evidence = artifact_delivery / "deploy.log"
            evidence.write_text("not applicable evidence\n", encoding="utf-8")
            for result in run_for_both_steps({**base_metadata, "delivery.deploy_evidence_path": str(evidence)}):
                self.assertEqual(result.returncode, 0, result.stderr)

            outside_evidence = root / "outside-deploy.log"
            outside_evidence.write_text("foreign evidence\n", encoding="utf-8")
            linked_evidence = artifact_delivery / "linked-deploy.log"
            linked_evidence.symlink_to(outside_evidence)
            for escaped_path in (
                "../outside-deploy.log",
                "nested/../../outside-deploy.log",
                str(outside_evidence),
                str(linked_evidence),
                "artifacts/outside-deploy.log",
            ):
                with self.subTest(escaped_evidence=escaped_path):
                    for escaped in run_for_both_steps({**base_metadata, "delivery.deploy_evidence_path": escaped_path}):
                        self.assertEqual(escaped.returncode, 1)
                        self.assertIn("must resolve within the canonical artifact delivery directory", escaped.stderr)

            for reason in ("", " \t "):
                with self.subTest(deploy_not_applicable_reason=repr(reason)):
                    for invalid_reason in run_for_both_steps({**base_metadata, "delivery.deploy_evidence_path": str(evidence), "gc.var.deploy_not_applicable_reason": reason}):
                        self.assertEqual(invalid_reason.returncode, 1)
                        self.assertIn("not-applicable deployment requires a nonblank deploy_not_applicable_reason", invalid_reason.stderr)

            for reason in ("", " \t "):
                with self.subTest(no_smoke_reason=repr(reason)):
                    environment["FAKE_GC_JSON"] = json.dumps(
                        [{"metadata": {
                            **base_metadata,
                            "delivery.deploy_evidence_path": str(evidence),
                            "gc.var.allow_no_smoke": "true",
                            "gc.var.no_smoke_reason": reason,
                        }}]
                    )
                    result = subprocess.run(
                        ["bash", str(RELEASE_VERIFIED_SCRIPT)],
                        capture_output=True,
                        text=True,
                        env=environment,
                    )
                    self.assertEqual(result.returncode, 1)
                    self.assertIn(
                        "no_smoke_reason is required and must be nonblank",
                        result.stderr,
                    )

            environment["FAKE_GC_JSON"] = json.dumps(
                [{"metadata": {
                    **base_metadata,
                    "delivery.deploy_evidence_path": str(evidence),
                    "gc.var.allow_no_smoke": "true",
                    "gc.var.no_smoke_reason": "No production smoke surface exists",
                    "delivery.no_smoke_reason": "No production smoke surface exists",
                }}]
            )
            result = subprocess.run(
                ["bash", str(RELEASE_VERIFIED_SCRIPT)],
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_release_verification_requires_exact_sha_command_and_bounds_timeouts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            repository = root / "repo"
            repository.mkdir()
            merge_sha = "a" * 40
            deploy_metadata, evidence = command_deploy_fixture(repository, merge_sha)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            gc = bin_dir / "gc"
            gc.write_text('#!/bin/sh\nprintf "%s\\n" "$FAKE_GC_JSON"\n', encoding="utf-8")
            gc.chmod(0o755)
            base_metadata = {
                "gc.step_ref": "complete-delivery.verify-production",
                "delivery.merge_sha": merge_sha,
                **deploy_metadata,
                "delivery.repo": "example/repo",
                "delivery.pr_number": "123",
                "gc.var.allow_no_smoke": "true",
                "gc.var.no_smoke_reason": "No production endpoint is exposed",
                "delivery.no_smoke_reason": "No production endpoint is exposed",
            }
            environment = os.environ.copy()
            environment.update(
                {
                    "GC_BEAD_ID": "step-1",
                    "GC_WORK_DIR": str(repository),
                    "PATH": f"{bin_dir}:{environment['PATH']}",
                }
            )
            outside_verify = root / "outside-verify.log"
            outside_verify.write_text("foreign verification\n", encoding="utf-8")
            linked_verify = repository / "linked-verify.log"
            linked_verify.symlink_to(outside_verify)
            for escaped_path in (
                "../outside-verify.log",
                "nested/../../outside-verify.log",
                str(outside_verify),
                str(linked_verify),
            ):
                with self.subTest(escaped_verify_evidence=escaped_path):
                    environment["FAKE_GC_JSON"] = json.dumps(
                        [{"metadata": {
                            **base_metadata,
                            "delivery.verify_evidence_path": escaped_path,
                            "gc.var.deploy_verify_command": "/bin/true",
                        }}]
                    )
                    escaped = subprocess.run(
                        ["bash", str(RELEASE_VERIFIED_SCRIPT)],
                        capture_output=True,
                        text=True,
                        env=environment,
                    )
                    self.assertEqual(escaped.returncode, 1)
                    self.assertIn(
                        "must resolve within the canonical delivery work directory",
                        escaped.stderr,
                    )

            environment["FAKE_GC_JSON"] = json.dumps([{"metadata": base_metadata}])
            missing = subprocess.run(
                ["bash", str(RELEASE_VERIFIED_SCRIPT)],
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(missing.returncode, 1)
            self.assertIn("deploy_verify_command is required for deploy_mode=command", missing.stderr)

            identity_command = (
                f'test "$DELIVERY_REPO" = example/repo && '
                f'test "$DELIVERY_PR" = 123 && '
                f'test "$DELIVERY_SHA" = {merge_sha}'
            )
            environment["FAKE_GC_JSON"] = json.dumps(
                [{"metadata": {**base_metadata, "gc.var.deploy_verify_command": identity_command}}]
            )
            identity_verified = subprocess.run(
                ["bash", str(RELEASE_VERIFIED_SCRIPT)],
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(identity_verified.returncode, 0, identity_verified.stderr)

            delivery_dir = repository / "artifacts" / "delivery"
            for capture in delivery_dir.glob("*.stdout.log.*"):
                capture.unlink()
            for capture in delivery_dir.glob("*.stderr.log.*"):
                capture.unlink()
            evidence.write_text("verified\n", encoding="utf-8")
            environment["FAKE_GC_JSON"] = json.dumps(
                [{"metadata": {
                    **base_metadata,
                    "gc.var.allow_no_smoke": "false",
                    "gc.var.deploy_verify_command": "printf verify-stdout; printf verify-stderr >&2",
                    "gc.var.smoke_command": "printf smoke-stdout; printf smoke-stderr >&2",
                }}]
            )
            captured = subprocess.run(
                ["bash", str(RELEASE_VERIFIED_SCRIPT)],
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(captured.returncode, 0, captured.stderr)
            recorded_captures = evidence.read_text(encoding="utf-8")
            for name, stdout, stderr in (
                ("deploy_verify", "verify-stdout", "verify-stderr"),
                ("smoke", "smoke-stdout", "smoke-stderr"),
            ):
                self.assertIn(f"command={name}", recorded_captures)
                self.assertIn("stdout_path=", recorded_captures)
                self.assertIn("stderr_path=", recorded_captures)
                self.assertEqual(
                    [path.read_text(encoding="utf-8") for path in delivery_dir.glob(f"{name}.stdout.log.*")],
                    [stdout],
                )
                self.assertEqual(
                    [path.read_text(encoding="utf-8") for path in delivery_dir.glob(f"{name}.stderr.log.*")],
                    [stderr],
                )

            environment["FAKE_GC_JSON"] = json.dumps(
                [{"metadata": {
                    **base_metadata,
                    "gc.var.deploy_verify_command": 'test "$DELIVERY_REPO" = wrong/repo',
                }}]
            )
            wrong_identity = subprocess.run(
                ["bash", str(RELEASE_VERIFIED_SCRIPT)],
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(wrong_identity.returncode, 1)
            self.assertIn("deploy_verify_command failed; see verification evidence", wrong_identity.stderr)
            self.assertIn("outcome=command_failure", evidence.read_text(encoding="utf-8"))

            environment["FAKE_GC_JSON"] = json.dumps(
                [
                    {
                        "metadata": {
                            **base_metadata,
                            "gc.var.deploy_verify_command": "sleep 1",
                            "gc.var.deploy_verify_timeout": "0.01s",
                        }
                    }
                ]
            )
            timed_out = subprocess.run(
                ["bash", str(RELEASE_VERIFIED_SCRIPT)],
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(timed_out.returncode, 1)
            self.assertIn("deploy_verify_command failed; see verification evidence", timed_out.stderr)
            self.assertIn("command=deploy_verify", evidence.read_text(encoding="utf-8"))
            self.assertIn("outcome=timeout", evidence.read_text(encoding="utf-8"))

            for command, expected_outcome in (("   ", "required for deploy_mode"), ("false; true", "command_failure")):
                with self.subTest(command=command):
                    evidence.write_text("verified\n", encoding="utf-8")
                    environment["FAKE_GC_JSON"] = json.dumps([{"metadata": {**base_metadata, "gc.var.deploy_verify_command": command}}])
                    failed = subprocess.run(["bash", str(RELEASE_VERIFIED_SCRIPT)], capture_output=True, text=True, env=environment)
                    self.assertEqual(failed.returncode, 1)
                    if command.isspace():
                        self.assertIn(expected_outcome, failed.stderr)
                    else:
                        self.assertIn(expected_outcome, evidence.read_text(encoding="utf-8"))
                        self.assertNotIn(command, evidence.read_text(encoding="utf-8"))

            environment["FAKE_GC_JSON"] = json.dumps(
                [{"metadata": {
                    **base_metadata,
                    "gc.var.deploy_verify_command": "/bin/true",
                    "gc.var.smoke_command": "\t ",
                }}]
            )
            blank_smoke = subprocess.run(
                ["bash", str(RELEASE_VERIFIED_SCRIPT)],
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(blank_smoke.returncode, 0, blank_smoke.stderr)
            self.assertIn(
                "command=smoke outcome=not_run reason=allow_no_smoke_true",
                evidence.read_text(encoding="utf-8"),
            )

            for timeout_value in ("0", "0s", "inf", "infinity", "--foreground"):
                with self.subTest(timeout_value=timeout_value):
                    environment["FAKE_GC_JSON"] = json.dumps(
                        [
                            {
                                "metadata": {
                                    **base_metadata,
                                    "gc.var.deploy_verify_command": "/bin/true",
                                    "gc.var.deploy_verify_timeout": timeout_value,
                                }
                            }
                        ]
                    )
                    invalid = subprocess.run(
                        ["bash", str(RELEASE_VERIFIED_SCRIPT)],
                        capture_output=True,
                        text=True,
                        env=environment,
                    )
                    self.assertEqual(invalid.returncode, 1)
                    self.assertIn(
                        "deploy_verify_timeout must be a positive finite duration no greater than 1h",
                        invalid.stderr,
                    )

            environment["FAKE_GC_JSON"] = json.dumps(
                [
                    {
                        "metadata": {
                            **base_metadata,
                            "gc.var.deploy_verify_command": "/bin/true",
                            "gc.var.smoke_timeout": "0s",
                        }
                    }
                ]
            )
            without_smoke = subprocess.run(
                ["bash", str(RELEASE_VERIFIED_SCRIPT)],
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(without_smoke.returncode, 0, without_smoke.stderr)
            recorded_evidence = evidence.read_text(encoding="utf-8")
            self.assertIn("command=smoke outcome=not_run reason=allow_no_smoke_true", recorded_evidence)
            self.assertIn("no_smoke_reason_sha256=", recorded_evidence)
            self.assertNotIn("No production endpoint is exposed", recorded_evidence)

            environment["FAKE_GC_JSON"] = json.dumps(
                [{"metadata": {
                    **base_metadata,
                    "gc.var.no_smoke_reason": " \t ",
                    "gc.var.deploy_verify_command": "/bin/true",
                }}]
            )
            missing_reason = subprocess.run(
                ["bash", str(RELEASE_VERIFIED_SCRIPT)],
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(missing_reason.returncode, 1)
            self.assertIn("no_smoke_reason is required and must be nonblank", missing_reason.stderr)

            environment["FAKE_GC_JSON"] = json.dumps(
                [{"metadata": {
                    **base_metadata,
                    "delivery.no_smoke_reason": "A different durable reason",
                    "gc.var.deploy_verify_command": "/bin/true",
                }}]
            )
            mismatched_reason = subprocess.run(
                ["bash", str(RELEASE_VERIFIED_SCRIPT)],
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(mismatched_reason.returncode, 1)
            self.assertIn(
                "delivery.no_smoke_reason must exactly match gc.var.no_smoke_reason",
                mismatched_reason.stderr,
            )

            environment["FAKE_GC_JSON"] = json.dumps(
                [
                    {
                        "metadata": {
                            **base_metadata,
                            "gc.var.deploy_verify_command": "/bin/true",
                            "gc.var.smoke_command": "/bin/true",
                            "gc.var.smoke_timeout": "0s",
                        }
                    }
                ]
            )
            invalid_smoke = subprocess.run(
                ["bash", str(RELEASE_VERIFIED_SCRIPT)],
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(invalid_smoke.returncode, 1)
            self.assertIn(
                "smoke_timeout must be a positive finite duration no greater than 1h",
                invalid_smoke.stderr,
            )

    def test_merged_check_binds_github_base_to_configured_base_branch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            gc = bin_dir / "gc"
            gc.write_text('#!/bin/sh\nprintf "%s\\n" "$FAKE_GC_JSON"\n', encoding="utf-8")
            gc.chmod(0o755)
            gh = bin_dir / "gh"
            gh.write_text(
                "#!/bin/sh\n"
                "if [ \"$3\" = \"--jq\" ]; then\n"
                "  printf '%s\\n' \"$FAKE_COMPARE_STATUS\"\n"
                "else\n"
                "  printf '%s\\n' \"$FAKE_PR_JSON\"\n"
                "fi\n",
                encoding="utf-8",
            )
            gh.chmod(0o755)
            metadata = {
                "delivery.repo": "example/repo",
                "delivery.pr_number": "123",
                "delivery.merge_sha": "a" * 40,
                "delivery.head_sha": "b" * 40,
                "delivery.pr_url": "https://github.com/example/repo/pull/123",
                "gc.var.base_branch": "main",
            }
            environment = os.environ.copy()
            environment.update(
                {
                    "GC_BEAD_ID": "step-1",
                    "GC_WORK_DIR": str(root),
                    "FAKE_GC_JSON": json.dumps([{"metadata": metadata}]),
                    "FAKE_PR_JSON": json.dumps({
                        "merged": True,
                        "state": "closed",
                        "merged_at": "2026-08-02T10:00:00Z",
                        "merge_commit_sha": "a" * 40,
                        "head": {"sha": "b" * 40},
                        "base": {"ref": "release"},
                        "html_url": "https://github.com/example/repo/pull/123",
                    }),
                    "FAKE_COMPARE_STATUS": "ahead",
                    "PATH": f"{bin_dir}:{environment['PATH']}",
                }
            )
            mismatched = subprocess.run(
                ["bash", str(MERGED_SCRIPT)], capture_output=True, text=True, env=environment
            )
            self.assertEqual(mismatched.returncode, 1)
            self.assertIn("does not match configured base_branch main", mismatched.stderr)

            valid_pr = {
                "merged": True,
                "state": "closed",
                "merged_at": "2026-08-02T10:00:00Z",
                "merge_commit_sha": "a" * 40,
                "head": {"sha": "b" * 40},
                "base": {"ref": "main"},
                "html_url": "https://github.com/example/repo/pull/123",
            }
            environment["FAKE_PR_JSON"] = json.dumps(valid_pr)
            verified = subprocess.run(
                ["bash", str(MERGED_SCRIPT)], capture_output=True, text=True, env=environment
            )
            self.assertEqual(verified.returncode, 0, verified.stderr)
            self.assertIn("-> main", verified.stdout)

            for mutation, message in (
                ({"merged": None}, "boolean merged field"),
                ({"merged": "false"}, "boolean merged field"),
                ({"merged": "true"}, "boolean merged field"),
                ({"merged": 0}, "boolean merged field"),
                ({"merged": 1}, "boolean merged field"),
                ({"state": "open"}, "is not closed"),
                ({"merged_at": ""}, "no merged_at timestamp"),
                ({"head": {"sha": "c" * 40}}, "does not match GitHub head"),
                ({"html_url": "https://github.com/example/repo/pull/999"}, "does not match recorded URL"),
            ):
                with self.subTest(mutation=mutation):
                    environment["FAKE_PR_JSON"] = json.dumps({**valid_pr, **mutation})
                    rejected = subprocess.run(
                        ["bash", str(MERGED_SCRIPT)],
                        capture_output=True,
                        text=True,
                        env=environment,
                    )
                    self.assertEqual(rejected.returncode, 1)
                    self.assertIn(message, rejected.stderr)

    def test_release_verification_disambiguates_child_and_timeout_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            repository = root / "repo"
            repository.mkdir()
            merge_sha = "a" * 40
            deploy_metadata, evidence = command_deploy_fixture(repository, merge_sha)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            gc = bin_dir / "gc"
            gc.write_text('#!/bin/sh\nprintf "%s\\n" "$FAKE_GC_JSON"\n', encoding="utf-8")
            gc.chmod(0o755)
            base_metadata = {
                "gc.step_ref": "complete-delivery.verify-production",
                "delivery.merge_sha": merge_sha,
                **deploy_metadata,
                "delivery.repo": "example/repo",
                "delivery.pr_number": "123",
                "gc.var.allow_no_smoke": "true",
                "gc.var.no_smoke_reason": "No production endpoint is exposed",
                "delivery.no_smoke_reason": "No production endpoint is exposed",
            }
            environment = os.environ.copy()
            environment.update(
                {
                    "GC_BEAD_ID": "step-1",
                    "GC_WORK_DIR": str(repository),
                    "PATH": f"{bin_dir}:{environment['PATH']}",
                }
            )

            def run(command: str, timeout: str = "1s") -> tuple[subprocess.CompletedProcess[str], str]:
                evidence.write_text("verified\n", encoding="utf-8")
                environment["FAKE_GC_JSON"] = json.dumps(
                    [
                        {
                            "metadata": {
                                **base_metadata,
                                "gc.var.deploy_verify_command": command,
                                "gc.var.deploy_verify_timeout": timeout,
                            }
                        }
                    ]
                )
                result = subprocess.run(
                    ["bash", str(RELEASE_VERIFIED_SCRIPT)],
                    capture_output=True,
                    text=True,
                    env=environment,
                )
                return result, evidence.read_text(encoding="utf-8")

            for child_status in (124, 125, 137):
                command = f"exit {child_status}"
                with self.subTest(child_status=child_status):
                    result, recorded_evidence = run(command)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn("outcome=command_failure", recorded_evidence)
                    self.assertIn(f"status={child_status}", recorded_evidence)
                    self.assertNotIn(command, recorded_evidence)

            timed_out, recorded_evidence = run("sleep 1", "0.01s")
            self.assertEqual(timed_out.returncode, 1)
            self.assertIn("outcome=timeout", recorded_evidence)
            self.assertNotIn("sleep 1", recorded_evidence)

            succeeded, recorded_evidence = run("/bin/true")
            self.assertEqual(succeeded.returncode, 0, succeeded.stderr)
            self.assertIn("outcome=passed", recorded_evidence)
            self.assertNotIn("/bin/true", recorded_evidence)

            fake_timeout = bin_dir / "timeout"
            fake_timeout.write_text("#!/bin/sh\nexit 125\n", encoding="utf-8")
            fake_timeout.chmod(0o755)
            wrapper_failed, recorded_evidence = run("/bin/true")
            self.assertEqual(wrapper_failed.returncode, 1)
            self.assertIn("outcome=timeout_utility_failure", recorded_evidence)
            self.assertNotIn("/bin/true", recorded_evidence)

    def test_deploy_check_executes_once_and_release_rejects_forged_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            repository = root / "repo"
            repository.mkdir()
            bin_dir = root / "bin"
            bin_dir.mkdir()
            updates = root / "updates.log"
            marker = root / "command-ran"
            gc = bin_dir / "gc"
            gc.write_text(
                "#!/bin/sh\n"
                "if [ \"${1:-}\" = bd ] && [ \"${2:-}\" = update ]; then\n"
                "  printf '%s\\n' \"$*\" >> \"$FAKE_GC_UPDATES\"\n"
                "  if [ \"${FAKE_GC_FAIL_STARTED_UPDATE:-}\" = true ] && printf '%s\\n' \"$*\" | grep -Fq 'delivery.deploy_status=started'; then\n"
                "    exit 1\n"
                "  fi\n"
                "  if [ \"${FAKE_GC_FAIL_FINAL_UPDATE:-}\" = true ] && printf '%s\\n' \"$*\" | grep -Fq 'delivery.deploy_evidence_path='; then\n"
                "    exit 1\n"
                "  fi\n"
                "  exit 0\n"
                "fi\n"
                "if [ \"${3:-}\" = root-1 ]; then\n"
                "  printf '%s\\n' \"$FAKE_GC_ROOT_JSON\"\n"
                "else\n"
                "  printf '%s\\n' \"$FAKE_GC_STEP_JSON\"\n"
                "fi\n",
                encoding="utf-8",
            )
            gc.chmod(0o755)
            mv = bin_dir / "mv"
            mv.write_text(
                "#!/bin/sh\n"
                "if [ -n \"${FAKE_MV_LOG:-}\" ]; then\n"
                "  if [ -n \"${FAKE_MV_FAIL_DEST:-}\" ] && [ \"${2:-}\" = \"$FAKE_MV_FAIL_DEST\" ]; then\n"
                "    printf 'failed %s\\n' \"$2\" >> \"$FAKE_MV_LOG\"\n"
                "    exit 1\n"
                "  fi\n"
                "  printf 'published %s\\n' \"${2:-}\" >> \"$FAKE_MV_LOG\"\n"
                "fi\n"
                "exec /bin/mv \"$@\"\n",
                encoding="utf-8",
            )
            mv.chmod(0o755)
            merge_sha = "a" * 40
            base_metadata = {
                "delivery.merge_sha": merge_sha,
                "delivery.repo": "example/repo",
                "delivery.pr_number": "123",
                "gc.var.artifact_root": "artifacts",
                "gc.var.deploy_mode": "command",
                "gc.var.deploy_timeout": "1s",
            }
            environment = os.environ.copy()
            environment.update(
                {
                    "GC_BEAD_ID": "deploy-step",
                    "GC_WORK_DIR": str(repository),
                    "FAKE_GC_UPDATES": str(updates),
                    "FAKE_GC_STEP_JSON": json.dumps(
                        [{"metadata": {"gc.root_bead_id": "root-1", "gc.step_ref": "complete-delivery.deploy"}}]
                    ),
                    "PATH": f"{bin_dir}:{environment['PATH']}",
                    "COMMAND_MARKER": str(marker),
                    "FAKE_MV_LOG": str(root / "mv.log"),
                }
            )

            environment["FAKE_GC_ROOT_JSON"] = json.dumps(
                [{"metadata": {**base_metadata, "gc.var.deploy_mode": "unknown"}}]
            )
            unsupported_mode = subprocess.run(
                ["bash", str(RELEASE_VERIFIED_SCRIPT)],
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(unsupported_mode.returncode, 1)
            self.assertIn(
                "deploy_mode must be command, ci, or not-applicable",
                unsupported_mode.stderr,
            )
            self.assertFalse(marker.exists())

            outside_artifacts = root / "outside-artifacts"
            outside_artifacts.mkdir()
            linked_artifacts = repository / "linked-artifacts"
            linked_artifacts.symlink_to(outside_artifacts, target_is_directory=True)
            for escaped_root in (
                "../outside-artifacts",
                "nested/../../outside-artifacts",
                str(outside_artifacts),
                str(linked_artifacts),
            ):
                with self.subTest(escaped_artifact_root=escaped_root):
                    environment["FAKE_GC_ROOT_JSON"] = json.dumps(
                        [{"metadata": {
                            **base_metadata,
                            "gc.var.artifact_root": escaped_root,
                            "gc.var.deploy_command": "/bin/true",
                        }}]
                    )
                    escaped = subprocess.run(
                        ["bash", str(RELEASE_VERIFIED_SCRIPT)],
                        capture_output=True,
                        text=True,
                        env=environment,
                    )
                    self.assertEqual(escaped.returncode, 1)
                    self.assertIn(
                        "must resolve within the canonical delivery work directory",
                        escaped.stderr,
                    )
                    self.assertFalse(marker.exists())

            linked_delivery_root = repository / "artifacts-with-linked-delivery"
            linked_delivery_root.mkdir()
            (linked_delivery_root / "delivery").symlink_to(
                outside_artifacts, target_is_directory=True
            )
            environment["FAKE_GC_ROOT_JSON"] = json.dumps(
                [{"metadata": {
                    **base_metadata,
                    "gc.var.artifact_root": "artifacts-with-linked-delivery",
                    "gc.var.deploy_command": "/bin/true",
                }}]
            )
            escaped_delivery_dir = subprocess.run(
                ["bash", str(RELEASE_VERIFIED_SCRIPT)],
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(escaped_delivery_dir.returncode, 1)
            self.assertIn(
                "deployment evidence directory must resolve within the canonical delivery work directory",
                escaped_delivery_dir.stderr,
            )
            self.assertFalse(marker.exists())

            environment["FAKE_GC_ROOT_JSON"] = json.dumps(
                [{"metadata": {**base_metadata, "gc.var.deploy_command": "/bin/true"}}]
            )
            for step_metadata, diagnostic in (
                ({"gc.root_bead_id": "root-1"}, "gc.step_ref is required"),
                (
                    {
                        "gc.root_bead_id": "root-1",
                        "gc.step_ref": "complete-delivery.other",
                    },
                    "unexpected deployment lifecycle gc.step_ref",
                ),
            ):
                with self.subTest(step_metadata=step_metadata):
                    environment["FAKE_GC_STEP_JSON"] = json.dumps(
                        [{"metadata": step_metadata}]
                    )
                    invalid_step = subprocess.run(
                        ["bash", str(RELEASE_VERIFIED_SCRIPT)],
                        capture_output=True,
                        text=True,
                        env=environment,
                    )
                    self.assertEqual(invalid_step.returncode, 1)
                    self.assertIn(diagnostic, invalid_step.stderr)
                    self.assertFalse(marker.exists())
            environment["FAKE_GC_STEP_JSON"] = json.dumps(
                [
                    {
                        "metadata": {
                            "gc.root_bead_id": "root-1",
                            "gc.step_ref": "complete-delivery.deploy",
                        }
                    }
                ]
            )

            def run_deploy(
                command: str,
                timeout: str = "1s",
                *,
                allow_missing_evidence: bool = False,
            ) -> tuple[subprocess.CompletedProcess[str], str]:
                delivery_dir = repository / "artifacts" / "delivery"
                if delivery_dir.exists():
                    for path in delivery_dir.iterdir():
                        path.unlink()
                if marker.exists():
                    marker.unlink()
                if updates.exists():
                    updates.unlink()
                environment["FAKE_GC_ROOT_JSON"] = json.dumps(
                    [{"metadata": {**base_metadata, "gc.var.deploy_command": command, "gc.var.deploy_timeout": timeout}}]
                )
                result = subprocess.run(
                    ["bash", str(RELEASE_VERIFIED_SCRIPT)],
                    capture_output=True,
                    text=True,
                    env=environment,
                )
                evidence = delivery_dir / "deploy.log"
                if not evidence.exists():
                    self.assertTrue(
                        allow_missing_evidence,
                        "deploy.log unexpectedly missing outside an injected publication failure",
                    )
                    return result, ""
                return result, evidence.read_text(encoding="utf-8")

            success_command = 'printf "%s\\n" "$DELIVERY_SHA" >> "$COMMAND_MARKER"'
            succeeded, evidence = run_deploy(success_command)
            self.assertEqual(succeeded.returncode, 0, succeeded.stderr + evidence)
            self.assertEqual(marker.read_text(encoding="utf-8"), f"{merge_sha}\n")
            self.assertEqual(evidence.count("schema=complete-delivery.deploy.v1"), 1)
            self.assertIn("outcome=passed", evidence)
            self.assertIn("child_status=0", evidence)
            self.assertIn("wrapper_status=0", evidence)
            self.assertIn("merge_sha=" + merge_sha, evidence)
            self.assertIn("delivery.deploy_status=deployed", updates.read_text(encoding="utf-8"))

            nonzero, evidence = run_deploy("exit 42")
            self.assertEqual(nonzero.returncode, 1)
            self.assertIn("outcome=command_failure", evidence)
            self.assertIn("child_status=42", evidence)

            timed_out, evidence = run_deploy("sleep 1", "0.01s")
            self.assertEqual(timed_out.returncode, 1)
            self.assertIn("outcome=timeout", evidence)
            self.assertIn("child_status=unavailable", evidence)

            for status in (124, 125, 137):
                with self.subTest(child_status=status):
                    child_failed, evidence = run_deploy(f"exit {status}")
                    self.assertEqual(child_failed.returncode, 1)
                    self.assertIn("outcome=command_failure", evidence)
                    self.assertIn(f"child_status={status}", evidence)

            delivery_dir = repository / "artifacts" / "delivery"
            deploy_log = delivery_dir / "deploy.log"
            stdout = delivery_dir / "deploy.stdout.log"
            stderr = delivery_dir / "deploy.stderr.log"
            for path in delivery_dir.iterdir():
                path.unlink()
            for status in ("started", "deployed", "failed", "verified"):
                with self.subTest(existing_deploy_status=status):
                    if marker.exists():
                        marker.unlink()
                    environment["FAKE_GC_ROOT_JSON"] = json.dumps([{"metadata": {
                        **base_metadata,
                        "gc.var.deploy_command": success_command,
                        "delivery.deploy_status": status,
                        "delivery.deploy_merge_sha": merge_sha,
                    }}])
                    repeated = subprocess.run(
                        ["bash", str(RELEASE_VERIFIED_SCRIPT)],
                        capture_output=True,
                        text=True,
                        env=environment,
                    )
                    self.assertEqual(repeated.returncode, 1)
                    self.assertIn("exact-once deployment forbids a rerun", repeated.stderr)
                    self.assertFalse(marker.exists())

            for capture_name in ("deploy.log", "deploy.stdout.log", "deploy.stderr.log"):
                with self.subTest(existing_final_capture=capture_name):
                    if marker.exists():
                        marker.unlink()
                    for path in delivery_dir.iterdir():
                        path.unlink()
                    (delivery_dir / capture_name).write_text("partial evidence\n", encoding="utf-8")
                    environment["FAKE_GC_ROOT_JSON"] = json.dumps([{"metadata": {
                        **base_metadata,
                        "gc.var.deploy_command": success_command,
                    }}])
                    repeated = subprocess.run(
                        ["bash", str(RELEASE_VERIFIED_SCRIPT)],
                        capture_output=True,
                        text=True,
                        env=environment,
                    )
                    self.assertEqual(repeated.returncode, 1)
                    self.assertIn("deploy evidence or capture already exists", repeated.stderr)
                    self.assertFalse(marker.exists())

            for path in delivery_dir.iterdir():
                path.unlink()
            if marker.exists():
                marker.unlink()
            environment["FAKE_GC_FAIL_STARTED_UPDATE"] = "true"
            environment["FAKE_GC_ROOT_JSON"] = json.dumps([{"metadata": {
                **base_metadata,
                "gc.var.deploy_command": success_command,
            }}])
            guard_failure = subprocess.run(
                ["bash", str(RELEASE_VERIFIED_SCRIPT)],
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(guard_failure.returncode, 1)
            self.assertIn("failed to atomically record deployment execution-started guard", guard_failure.stderr)
            self.assertFalse(marker.exists())
            self.assertFalse(any(delivery_dir.iterdir()))
            environment.pop("FAKE_GC_FAIL_STARTED_UPDATE")

            # Each publication boundary must clean up all final evidence after
            # the command has run.  The started guard remains durable so a
            # same-SHA replay fails before it can execute the command again.
            for failure_name, failed_destination in (
                ("stderr capture", "deploy.stderr.log"),
                ("deploy evidence", "deploy.log"),
            ):
                with self.subTest(publication_failure=failure_name):
                    for path in delivery_dir.iterdir():
                        path.unlink()
                    if marker.exists():
                        marker.unlink()
                    if updates.exists():
                        updates.unlink()
                    mv_log = pathlib.Path(environment["FAKE_MV_LOG"])
                    if mv_log.exists():
                        mv_log.unlink()
                    environment["FAKE_MV_FAIL_DEST"] = str(
                        delivery_dir / failed_destination
                    )
                    failed, _ = run_deploy(
                        success_command, allow_missing_evidence=True
                    )
                    environment.pop("FAKE_MV_FAIL_DEST")
                    self.assertEqual(failed.returncode, 1)
                    self.assertFalse(
                        any(
                            path.exists()
                            for path in (
                                deploy_log,
                                stdout,
                                stderr,
                            )
                        )
                    )
                    self.assertEqual(
                        marker.read_text(encoding="utf-8"), f"{merge_sha}\n"
                    )
                    self.assertIn(
                        "delivery.deploy_status=started", updates.read_text(encoding="utf-8")
                    )
                    publications = mv_log.read_text(encoding="utf-8").splitlines()
                    self.assertIn(f"published {stdout}", publications)
                    self.assertIn(
                        f"failed {delivery_dir / failed_destination}", publications
                    )

                    environment["FAKE_GC_ROOT_JSON"] = json.dumps([{"metadata": {
                        **base_metadata,
                        "gc.var.deploy_command": success_command,
                        "delivery.deploy_status": "started",
                        "delivery.deploy_merge_sha": merge_sha,
                    }}])
                    marker_before_replay = marker.read_text(encoding="utf-8")
                    replay = subprocess.run(
                        ["bash", str(RELEASE_VERIFIED_SCRIPT)],
                        capture_output=True,
                        text=True,
                        env=environment,
                    )
                    self.assertEqual(replay.returncode, 1)
                    self.assertIn("exact-once deployment forbids a rerun", replay.stderr)
                    self.assertEqual(
                        marker.read_text(encoding="utf-8"), marker_before_replay
                    )

            with self.subTest(publication_failure="final metadata update"):
                for path in delivery_dir.iterdir():
                    path.unlink()
                if marker.exists():
                    marker.unlink()
                if updates.exists():
                    updates.unlink()
                mv_log = pathlib.Path(environment["FAKE_MV_LOG"])
                if mv_log.exists():
                    mv_log.unlink()
                environment["FAKE_GC_FAIL_FINAL_UPDATE"] = "true"
                failed, _ = run_deploy(
                    success_command, allow_missing_evidence=True
                )
                environment.pop("FAKE_GC_FAIL_FINAL_UPDATE")
                self.assertEqual(failed.returncode, 1)
                self.assertFalse(
                    any(
                        path.exists()
                        for path in (
                            deploy_log,
                            stdout,
                            stderr,
                        )
                    )
                )
                self.assertEqual(
                    marker.read_text(encoding="utf-8"), f"{merge_sha}\n"
                )
                self.assertIn(
                    "delivery.deploy_status=started", updates.read_text(encoding="utf-8")
                )
                environment["FAKE_GC_ROOT_JSON"] = json.dumps([{"metadata": {
                    **base_metadata,
                    "gc.var.deploy_command": success_command,
                    "delivery.deploy_status": "started",
                    "delivery.deploy_merge_sha": merge_sha,
                }}])
                marker_before_replay = marker.read_text(encoding="utf-8")
                replay = subprocess.run(
                    ["bash", str(RELEASE_VERIFIED_SCRIPT)],
                    capture_output=True,
                    text=True,
                    env=environment,
                )
                self.assertEqual(replay.returncode, 1)
                self.assertIn("exact-once deployment forbids a rerun", replay.stderr)
                self.assertEqual(
                    marker.read_text(encoding="utf-8"), marker_before_replay
                )

            succeeded, evidence = run_deploy(success_command)
            self.assertEqual(succeeded.returncode, 0, succeeded.stderr)
            delivery_dir = repository / "artifacts" / "delivery"
            deploy_log = delivery_dir / "deploy.log"
            stdout = delivery_dir / "deploy.stdout.log"
            stderr = delivery_dir / "deploy.stderr.log"
            verify_log = delivery_dir / "verify.log"
            verify_log.write_text("verification evidence\n", encoding="utf-8")
            command_label = "sha256:" + hashlib.sha256(success_command.encode()).hexdigest()
            verified_metadata = {
                **base_metadata,
                "delivery.deployed_sha": merge_sha,
                "delivery.deploy_status": "verified",
                "delivery.deploy_evidence_path": str(deploy_log),
                "delivery.verify_evidence_path": str(verify_log),
                "delivery.deploy_command_label": command_label,
                "delivery.deploy_timeout": "1s",
                "delivery.deploy_outcome": "passed",
                "delivery.deploy_child_status": "0",
                "delivery.deploy_wrapper_status": "0",
                "delivery.deploy_merge_sha": merge_sha,
                "delivery.deploy_stdout_path": str(stdout),
                "delivery.deploy_stderr_path": str(stderr),
                "gc.var.deploy_command": success_command,
                "gc.var.deploy_verify_command": "/bin/true",
                "gc.var.allow_no_smoke": "true",
                "gc.var.no_smoke_reason": "No production endpoint is exposed",
                "delivery.no_smoke_reason": "No production endpoint is exposed",
            }
            environment["GC_BEAD_ID"] = "verify-step"
            environment["FAKE_GC_STEP_JSON"] = json.dumps(
                [{"metadata": {"gc.root_bead_id": "root-1", "gc.step_ref": "complete-delivery.verify-production"}}]
            )
            environment["FAKE_GC_ROOT_JSON"] = json.dumps([{"metadata": verified_metadata}])
            release = subprocess.run(
                ["bash", str(RELEASE_VERIFIED_SCRIPT)],
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(release.returncode, 0, release.stderr)

            outside_evidence = root / "outside-real-deploy-evidence.log"
            outside_evidence.write_text("outside\n", encoding="utf-8")
            for metadata_key in (
                "delivery.deploy_evidence_path",
                "delivery.verify_evidence_path",
                "delivery.deploy_stdout_path",
                "delivery.deploy_stderr_path",
            ):
                with self.subTest(real_deployment_evidence_path=metadata_key):
                    environment["FAKE_GC_ROOT_JSON"] = json.dumps([{"metadata": {
                        **verified_metadata,
                        metadata_key: str(outside_evidence),
                    }}])
                    escaped = subprocess.run(
                        ["bash", str(RELEASE_VERIFIED_SCRIPT)],
                        capture_output=True,
                        text=True,
                        env=environment,
                    )
                    self.assertEqual(escaped.returncode, 1)
                    self.assertIn("must resolve within", escaped.stderr)
            environment["FAKE_GC_ROOT_JSON"] = json.dumps([{"metadata": verified_metadata}])

            deploy_log.write_text("verified\n", encoding="utf-8")
            forged = subprocess.run(
                ["bash", str(RELEASE_VERIFIED_SCRIPT)],
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(forged.returncode, 1)
            self.assertIn("deploy evidence is forged, stale, or incomplete", forged.stderr)

    def test_ci_deploy_is_github_attested_and_revalidated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            repository = root / "repo"
            repository.mkdir()
            bin_dir = root / "bin"
            bin_dir.mkdir()
            updates = root / "updates.log"
            gc = bin_dir / "gc"
            gc.write_text(
                "#!/bin/sh\n"
                "if [ \"${1:-}\" = bd ] && [ \"${2:-}\" = update ]; then\n"
                "  printf '%s\\n' \"$*\" >> \"$FAKE_GC_UPDATES\"\n"
                "  exit 0\n"
                "fi\n"
                "if [ \"${3:-}\" = root-1 ]; then\n"
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
                "endpoint=\n"
                "for argument in \"$@\"; do\n"
                "  case \"$argument\" in repos/*) endpoint=$argument ;; esac\n"
                "done\n"
                "case \"$endpoint\" in\n"
                "  repos/example/repo/actions/runs/456) printf '%s\\n' \"$FAKE_RUN_JSON\" ;;\n"
                "  repos/example/repo/pulls/123) printf '%s\\n' \"$FAKE_PR_JSON\" ;;\n"
                "  repos/example/repo/deployments) printf '%s\\n' \"$FAKE_DEPLOYMENTS_JSON\" ;;\n"
                "  repos/example/repo/deployments/789/statuses*) printf '%s\\n' \"$FAKE_STATUSES_JSON\" ;;\n"
                "  *) printf 'unexpected endpoint: %s\\n' \"$endpoint\" >&2; exit 1 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            gh.chmod(0o755)

            merge_sha = "a" * 40
            workflow = ".github/workflows/deploy.yml"
            run = {
                "id": 456,
                "repository": {"full_name": "example/repo"},
                "head_sha": merge_sha,
                "head_branch": "main",
                "path": f"{workflow}@refs/heads/main",
                "status": "completed",
                "conclusion": "success",
                "workflow_id": 99,
                "html_url": "https://github.com/example/repo/actions/runs/456",
                "created_at": "2026-08-02T10:01:00Z",
            }
            pull = {
                "number": 123,
                "base": {"ref": "main", "repo": {"full_name": "example/repo"}},
                "merge_commit_sha": merge_sha,
                "merged_at": "2026-08-02T10:00:00Z",
            }
            deployments = [{
                "id": 789,
                "sha": merge_sha,
                "environment": "production",
                "created_at": "2026-08-02T10:02:00Z",
            }]
            statuses = [{
                "id": 790,
                "state": "success",
                "environment": "production",
                "log_url": "https://github.com/example/repo/actions/runs/456",
                "created_at": "2026-08-02T10:03:00Z",
            }]
            base_metadata = {
                "delivery.merge_sha": merge_sha,
                "delivery.repo": "example/repo",
                "delivery.pr_number": "123",
                "delivery.deploy_run_id": "456",
                "gc.var.artifact_root": "artifacts",
                "gc.var.base_branch": "main",
                "gc.var.deploy_mode": "ci",
                "gc.var.deploy_ci_workflow": workflow,
                "gc.var.deploy_environment": "production",
            }
            environment = os.environ.copy()
            environment.update(
                {
                    "GC_BEAD_ID": "deploy-step",
                    "GC_WORK_DIR": str(repository),
                    "FAKE_GC_UPDATES": str(updates),
                    "FAKE_GC_STEP_JSON": json.dumps(
                        [{"metadata": {
                            "gc.root_bead_id": "root-1",
                            "gc.step_ref": "complete-delivery.deploy",
                        }}]
                    ),
                    "FAKE_GC_ROOT_JSON": json.dumps([{"metadata": base_metadata}]),
                    "FAKE_RUN_JSON": json.dumps(run),
                    "FAKE_PR_JSON": json.dumps(pull),
                    "FAKE_DEPLOYMENTS_JSON": json.dumps(deployments),
                    "FAKE_STATUSES_JSON": json.dumps(statuses),
                    "PATH": f"{bin_dir}:{environment['PATH']}",
                }
            )

            for workflow_run_path in (
                workflow,
                f"{workflow}@main",
                f"{workflow}@refs/heads/main",
            ):
                with self.subTest(workflow_run_path=workflow_run_path):
                    environment["FAKE_RUN_JSON"] = json.dumps(
                        {**run, "path": workflow_run_path}
                    )
                    deployed = subprocess.run(
                        ["bash", str(RELEASE_VERIFIED_SCRIPT)],
                        capture_output=True,
                        text=True,
                        env=environment,
                    )
                    self.assertEqual(
                        deployed.returncode, 0, deployed.stdout + deployed.stderr
                    )
            environment["FAKE_DEPLOYMENTS_JSON"] = json.dumps([deployments])
            environment["FAKE_STATUSES_JSON"] = json.dumps([statuses])
            paginated = subprocess.run(
                ["bash", str(RELEASE_VERIFIED_SCRIPT)],
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(paginated.returncode, 0, paginated.stdout + paginated.stderr)
            environment["FAKE_DEPLOYMENTS_JSON"] = json.dumps(deployments)
            environment["FAKE_STATUSES_JSON"] = json.dumps(statuses)
            environment["FAKE_RUN_JSON"] = json.dumps(run)
            deploy_log = repository / "artifacts" / "delivery" / "deploy.log"
            recorded = deploy_log.read_text(encoding="utf-8")
            self.assertIn("schema=complete-delivery.ci-deploy.v1", recorded)
            self.assertIn(f"merge_sha={merge_sha}", recorded)
            self.assertIn("workflow_id=99", recorded)
            self.assertIn(
                f"workflow_run_path={workflow}@refs/heads/main", recorded
            )
            self.assertIn("run_id=456", recorded)
            self.assertIn("run_conclusion=success", recorded)
            self.assertIn("environment=production", recorded)
            self.assertIn("deployment_status=success", recorded)
            self.assertIn(
                "deployment_log_url=https://github.com/example/repo/actions/runs/456",
                recorded,
            )
            update_record = updates.read_text(encoding="utf-8")
            self.assertIn("delivery.deploy_status=deployed", update_record)
            self.assertIn("delivery.deploy_run_url=https://github.com/example/repo/actions/runs/456", update_record)
            self.assertIn(
                f"delivery.deploy_workflow_run_path={workflow}@refs/heads/main",
                update_record,
            )

            for label, log_url in (
                ("missing", None),
                ("different deployment URL", "https://deploy.example.test/logs/789"),
            ):
                with self.subTest(deployment_status_log_url=label):
                    status = {
                        "id": 790,
                        "state": "success",
                        "environment": "production",
                        "created_at": "2026-08-02T10:03:00Z",
                    }
                    if log_url is not None:
                        status["log_url"] = log_url
                    environment["FAKE_STATUSES_JSON"] = json.dumps([status])
                    deployed = subprocess.run(
                        ["bash", str(RELEASE_VERIFIED_SCRIPT)],
                        capture_output=True,
                        text=True,
                        env=environment,
                    )
                    self.assertEqual(
                        deployed.returncode, 0, deployed.stdout + deployed.stderr
                    )
                    recorded_status = deploy_log.read_text(encoding="utf-8")
                    self.assertIn(
                        f"deployment_log_url={log_url or ''}", recorded_status
                    )
            environment["FAKE_STATUSES_JSON"] = json.dumps(statuses)
            restored = subprocess.run(
                ["bash", str(RELEASE_VERIFIED_SCRIPT)],
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(restored.returncode, 0, restored.stdout + restored.stderr)

            verify_log = repository / "artifacts" / "delivery" / "verify.log"
            verify_log.write_text("verification evidence\n", encoding="utf-8")
            verified_metadata = {
                **base_metadata,
                "delivery.deployed_sha": merge_sha,
                "delivery.deploy_status": "verified",
                "delivery.deploy_evidence_path": str(deploy_log),
                "delivery.verify_evidence_path": str(verify_log),
                "delivery.deploy_run_url": run["html_url"],
                "delivery.deploy_workflow_id": "99",
                "delivery.deploy_workflow": workflow,
                "delivery.deploy_workflow_run_path": f"{workflow}@refs/heads/main",
                "delivery.deploy_environment": "production",
                "delivery.deploy_merge_sha": merge_sha,
                "delivery.deploy_conclusion": "success",
                "delivery.deploy_deployment_id": "789",
                "delivery.deploy_deployment_status_id": "790",
                "gc.var.deploy_verify_command": "/bin/true",
                "gc.var.allow_no_smoke": "true",
                "gc.var.no_smoke_reason": "No production endpoint is exposed",
                "delivery.no_smoke_reason": "No production endpoint is exposed",
            }
            environment["GC_BEAD_ID"] = "verify-step"
            environment["FAKE_GC_STEP_JSON"] = json.dumps(
                [{"metadata": {
                    "gc.root_bead_id": "root-1",
                    "gc.step_ref": "complete-delivery.verify-production",
                }}]
            )
            environment["FAKE_GC_ROOT_JSON"] = json.dumps(
                [{"metadata": verified_metadata}]
            )
            verified = subprocess.run(
                ["bash", str(RELEASE_VERIFIED_SCRIPT)],
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)

            environment["FAKE_RUN_JSON"] = json.dumps({**run, "conclusion": "failure"})
            stale_ci_evidence = subprocess.run(
                ["bash", str(RELEASE_VERIFIED_SCRIPT)],
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(stale_ci_evidence.returncode, 1)
            self.assertEqual(
                list((repository / "artifacts" / "delivery").glob("ci-deploy-current.tmp.*")),
                [],
            )
            environment["FAKE_RUN_JSON"] = json.dumps(run)

            deploy_log.write_text("arbitrary nonempty evidence\n", encoding="utf-8")
            forged = subprocess.run(
                ["bash", str(RELEASE_VERIFIED_SCRIPT)],
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(forged.returncode, 1)
            self.assertIn("CI deployment evidence is forged, stale, or incomplete", forged.stderr)
            deploy_log.write_text(recorded, encoding="utf-8")

            environment["GC_BEAD_ID"] = "deploy-step"
            environment["FAKE_GC_STEP_JSON"] = json.dumps(
                [{"metadata": {
                    "gc.root_bead_id": "root-1",
                    "gc.step_ref": "complete-delivery.deploy",
                }}]
            )
            environment["FAKE_GC_ROOT_JSON"] = json.dumps([{"metadata": base_metadata}])
            for name, run_mutation in (
                ("wrong sha", {"head_sha": "b" * 40}),
                ("wrong repository", {"repository": {"full_name": "other/repo"}}),
                ("failed run", {"conclusion": "failure"}),
                ("stale run", {"created_at": "2026-08-02T09:59:00Z"}),
                ("wrong workflow", {"path": ".github/workflows/other.yml@main"}),
                ("wrong short ref", {"path": f"{workflow}@release"}),
                ("wrong qualified ref", {"path": f"{workflow}@refs/heads/release"}),
                ("tag ref", {"path": f"{workflow}@refs/tags/main"}),
                ("workflow prefix", {"path": f"x{workflow}@main"}),
                ("ref suffix", {"path": f"{workflow}@main-extra"}),
            ):
                with self.subTest(name=name):
                    environment["FAKE_RUN_JSON"] = json.dumps({**run, **run_mutation})
                    rejected = subprocess.run(
                        ["bash", str(RELEASE_VERIFIED_SCRIPT)],
                        capture_output=True,
                        text=True,
                        env=environment,
                    )
                    self.assertEqual(rejected.returncode, 1)
                    self.assertIn(
                        "GitHub CI deployment run is failed, stale, or does not bind",
                        rejected.stderr,
                    )
                    self.assertEqual(
                        list((repository / "artifacts" / "delivery").glob("deploy.log.tmp.*")),
                        [],
                    )
            environment["FAKE_RUN_JSON"] = json.dumps(run)

            environment["FAKE_GC_ROOT_JSON"] = json.dumps(
                [{"metadata": {**base_metadata, "gc.var.deploy_environment": ""}}]
            )
            missing_environment = subprocess.run(
                ["bash", str(RELEASE_VERIFIED_SCRIPT)],
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(missing_environment.returncode, 1)
            self.assertIn("deploy_environment", missing_environment.stderr)

    def test_pr_open_validates_full_recorded_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            gc = bin_dir / "gc"
            gc.write_text('#!/bin/sh\nprintf "%s\\n" "$FAKE_GC_JSON"\n', encoding="utf-8")
            gc.chmod(0o755)
            gh = bin_dir / "gh"
            gh.write_text('#!/bin/sh\nprintf "%s\\n" "$FAKE_PR_JSON"\n', encoding="utf-8")
            gh.chmod(0o755)
            metadata = {
                "delivery.repo": "example/repo",
                "delivery.pr_number": "123",
                "delivery.head_sha": "a" * 40,
                "delivery.pr_url": "https://github.com/example/repo/pull/123",
                "delivery.branch": "feature/delivery",
                "gc.var.base_branch": "main",
            }
            pull = {
                "state": "open",
                "draft": False,
                "head": {"sha": "a" * 40, "ref": "feature/delivery"},
                "base": {"ref": "main", "repo": {"full_name": "example/repo"}},
                "number": 123,
                "html_url": metadata["delivery.pr_url"],
            }
            environment = os.environ.copy()
            environment.update(
                {
                    "GC_BEAD_ID": "step-1",
                    "GC_WORK_DIR": str(root),
                    "FAKE_GC_JSON": json.dumps([{"metadata": metadata}]),
                    "FAKE_PR_JSON": json.dumps(pull),
                    "PATH": f"{bin_dir}:{environment['PATH']}",
                }
            )
            result = subprocess.run(
                ["bash", str(PR_OPEN_SCRIPT)],
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            for mutation, message in (
                ({"state": "closed"}, "PR is not open"),
                ({"draft": True}, "PR is still a draft"),
                ({"draft": "false"}, "PR response has no boolean draft field"),
                ({"head": {"sha": "b" * 40}}, "does not match GitHub head"),
                ({"head": {"sha": "a" * 40, "ref": "wrong-branch"}}, "does not match GitHub head branch"),
                ({"base": {"ref": "wrong-base", "repo": {"full_name": "example/repo"}}}, "does not match configured base branch"),
                ({"base": {"ref": "main", "repo": {"full_name": "other/repo"}}}, "does not match recorded repository"),
                ({"number": 456}, "does not match recorded PR number"),
                ({"html_url": "https://github.com/example/repo/pull/456"}, "does not match recorded URL"),
            ):
                with self.subTest(message=message):
                    environment["FAKE_PR_JSON"] = json.dumps({**pull, **mutation})
                    result = subprocess.run(
                        ["bash", str(PR_OPEN_SCRIPT)],
                        capture_output=True,
                        text=True,
                        env=environment,
                    )
                    self.assertEqual(result.returncode, 1)
                    self.assertIn(message, result.stderr)

            missing_draft = {key: value for key, value in pull.items() if key != "draft"}
            environment["FAKE_PR_JSON"] = json.dumps(missing_draft)
            result = subprocess.run(
                ["bash", str(PR_OPEN_SCRIPT)],
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("PR response has no boolean draft field", result.stderr)

            environment["FAKE_GC_JSON"] = json.dumps(
                [{"metadata": {**metadata, "gc.var.base_branch": ""}}]
            )
            missing_base = subprocess.run(
                ["bash", str(PR_OPEN_SCRIPT)],
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(missing_base.returncode, 1)
            self.assertIn("configured base_branch is required", missing_base.stderr)

    def test_lifecycle_prompts_preserve_current_head_and_mode_contracts(self) -> None:
        with FORMULA_PATH.open("rb") as formula_file:
            formula = tomllib.load(formula_file)
        steps = {step["id"]: step for step in formula["steps"]}
        self.assertEqual(
            steps["delivery-preflight"]["check"]["check"]["path"],
            ".gc/scripts/checks/delivery-preflight.sh",
        )
        for step_id in ("requirements", "plan", "decompose"):
            self.assertEqual(
                steps[step_id]["check"]["check"]["path"],
                ".gc/scripts/checks/delivery-source-artifact-valid.sh",
            )
        self.assertEqual(
            steps["finalize"]["check"]["check"]["path"],
            ".gc/scripts/checks/delivery-source-artifact-valid.sh",
        )
        self.assertEqual(
            steps["finalize"]["metadata"]["gc.build.artifact_path_keys"],
            "gc.build.final_report_path,gc.var.final_report_path",
        )

        for prompt, expected_path, materialized in (
            (WORKFLOW_ROOT / "delivery-preflight.md", ".gc/scripts/checks/delivery-preflight.sh", True),
            (WORKFLOW_ROOT / "local-gates.md", ".gc/scripts/checks/delivery-local-gates.sh", True),
            (
                PR_GATE_WORKFLOW_ROOT / "{target}.rerun-local-gates.md",
                "{{pack_root}}/assets/scripts/checks/delivery-local-gates.sh",
                False,
            ),
        ):
            text = prompt.read_text(encoding="utf-8")
            self.assertIn(f"`{expected_path}`", text)
            if materialized:
                self.assertNotIn("{{pack_root}}/assets/scripts/checks/", text)

        external_review = (WORKFLOW_ROOT / "external-review.md").read_text(encoding="utf-8")
        for term in ("delivery.head_sha", "delivery.repo", "delivery.branch", "delivery.pr_number", "delivery.pr_url", "local_gates.status: \"passed\"", "tested_commit", "single UTC", "two hours", "non-resettable UTC"):
            self.assertIn(term, external_review)

        local_gates = (WORKFLOW_ROOT / "local-gates.md").read_text(encoding="utf-8")
        self.assertIn("status=passed", local_gates)
        self.assertIn("full final `tested_commit`", local_gates)
        for term in ("delivery.local_gate_summary_path", "canonicalize", "non-symlink", "nonempty regular"):
            self.assertIn(term, local_gates)

        readiness = (WORKFLOW_ROOT / "release-readiness.md").read_text(encoding="utf-8")
        for term in ("deploy_mode=command", "deploy_mode=not-applicable", "deploy_not_applicable_reason", "allow_no_smoke=true", "no_smoke_reason", "verify-production"):
            self.assertIn(term, readiness)

        verification = (WORKFLOW_ROOT / "verify-production.md").read_text(encoding="utf-8")
        for term in (
            "allow_no_smoke=false",
            "no_smoke_reason",
            "smoke_timeout",
            "DELIVERY_REPO=delivery.repo",
            "DELIVERY_PR=delivery.pr_number",
            "delivery.deployed_sha == delivery.merge_sha",
            "independently verifiable",
            "repository rollback guidance",
            "separately authorized repository-owned bounded workflow",
            "<artifact_root>/delivery",
            "whenever `smoke_command` is nonblank",
            "structured summary",
            "sibling stdout/stderr",
        ):
            self.assertIn(term, verification)

        requirements = (WORKFLOW_ROOT / "requirements.md").read_text(encoding="utf-8")
        self.assertIn("Write requirements to `{{requirements_path}}`", requirements)
        self.assertIn("gc.build.requirements_path", requirements)
        self.assertIn("status: approved", requirements)

        plan = (WORKFLOW_ROOT / "plan.md").read_text(encoding="utf-8")
        self.assertIn("Write the plan to `{{plan_path}}`", plan)
        self.assertIn("gc.build.plan_path", plan)

        deploy = (WORKFLOW_ROOT / "deploy.md").read_text(encoding="utf-8")
        self.assertIn("deploy_timeout", deploy)
        self.assertIn("timeout --kill-after=5s", deploy)
        self.assertIn("DELIVERY_SHA", deploy)
        self.assertIn("delivery.deploy_run_id", deploy)
        self.assertIn("deploy_ci_workflow", deploy)
        self.assertIn("deploy_environment", deploy)
        self.assertEqual(
            steps["deploy"]["check"]["check"]["path"],
            ".gc/scripts/checks/delivery-release-verified.sh",
        )
        self.assertEqual(steps["deploy"]["check"]["max_attempts"], 1)
        self.assertEqual(steps["verify-production"]["check"]["max_attempts"], 4)

        self.assertEqual(formula["vars"]["deploy_timeout"]["default"], "5m")
        self.assertEqual(formula["vars"]["deploy_ci_workflow"]["default"], "")
        self.assertEqual(formula["vars"]["deploy_environment"]["default"], "")
        self.assertEqual(steps["verify-production"]["check"]["check"]["timeout"], "125m")

        merge = (WORKFLOW_ROOT / "merge.md").read_text(encoding="utf-8")
        self.assertIn("DELIVERY_PR_URL", merge)
        self.assertIn('gh pr merge "$DELIVERY_PR_URL"', merge)
        self.assertIn("gc.var.merge_method", merge)
        self.assertIn("MERGE_METHOD", merge)
        for flag in ("--squash", "--merge", "--rebase"):
            self.assertIn(flag, merge)
        self.assertLess(merge.index("re-read the PR's `base.ref`"), merge.index('gh pr merge "$DELIVERY_PR_URL"'))
        self.assertIn("without requiring a mutable current head", merge)

        self.assertIn("no repair-redeployment lane", deploy)
        self.assertIn("completed successful deployment for the exact merge SHA", deploy)
        self.assertIn('"") DEPLOY_MODE="command"', RELEASE_VERIFIED_SCRIPT.read_text(encoding="utf-8"))

        source_artifact = SOURCE_ARTIFACT_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("python3 -c 'import yaml'", source_artifact)
        self.assertIn("build_artifact_valid_path", source_artifact)
        self.assertIn('DELIVERY_SOURCE_FIELDS="$SOURCE_FIELDS"', source_artifact)
        self.assertNotIn('"$SOURCE_ID" "$SOURCE_TITLE" "$SCHEMA"', source_artifact)

        finalizer = (WORKFLOW_ROOT / "finalize.md").read_text(encoding="utf-8")
        self.assertIn("returned `id` to equal", finalizer)
        self.assertIn("nonblank acceptance criteria", finalizer)
        self.assertIn("source trace is resolved", finalizer)
        self.assertIn("Acceptance criteria SHA-256", finalizer)
        self.assertIn("source.acceptance_criteria_sha256", finalizer)
        self.assertIn("source-artifact validator", finalizer)
        self.assertIn("no blockers remain", finalizer)
        self.assertIn("exact raw acceptance-criteria", finalizer)
        self.assertIn("safe YAML string serialization", finalizer)

        release_verified = RELEASE_VERIFIED_SCRIPT.read_text(encoding="utf-8")
        self.assertEqual(release_verified.count("timeout --kill-after=5s 30s gh api"), 4)
        self.assertIn("trap - HUP INT TERM", release_verified)

        for prompt in ("requirements.md", "plan.md", "decompose.md"):
            prompt_text = (WORKFLOW_ROOT / prompt).read_text(encoding="utf-8")
            self.assertIn("exact unfenced H2 heading `## Source Intent`", prompt_text)
            self.assertIn("source.acceptance_criteria_sha256", prompt_text)
            self.assertIn("exact JSON", prompt_text)
            self.assertIn("`acceptance_criteria`", prompt_text)
            self.assertIn("byte-for-byte", prompt_text)
            self.assertIn("trimming", prompt_text)
            self.assertIn("reformatting", prompt_text)


class SourceArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not SOURCE_ARTIFACT_GENERIC_CHECK.is_file():
            raise RuntimeError(
                "required inherited gascity checker is unavailable: "
                f"{SOURCE_ARTIFACT_GENERIC_CHECK}"
            )

    def artifact(
        self,
        *,
        artifact_kind: str = "requirements",
        source_id: str = "fi-123",
        source_title: str = "Requested delivery",
        source_anchor: str = "gc:fi-123",
        include_source: bool = True,
        acceptance_criteria: str = "The requested outcome is delivered.",
        source_acceptance_hash: str | None = None,
        include_source_trace: bool = True,
    ) -> str:
        source = ""
        if include_source:
            source_acceptance = (
                "  acceptance_criteria_sha256: "
                f"{source_acceptance_hash if source_acceptance_hash is not None else 'sha256:' + hashlib.sha256(acceptance_criteria.encode('utf-8')).hexdigest()}\n"
            )
            source = (
                "source:\n"
                f"  id: {source_id}\n"
                f"  title: {json.dumps(source_title)}\n"
                f"  anchor: {source_anchor}\n"
                f"{source_acceptance}"
            )
        required_sections = {
            "requirements": (
                "Problem Statement",
                "W6H",
                "User Stories",
                "Technical Stories",
                "Behavior Requirements",
                "Example Mapping",
                "Acceptance Criteria",
                "Out Of Scope",
                "Open Questions",
            ),
            "plan": (
                "Summary",
                "Current System",
                "Proposed Implementation",
                "Non-Goals",
                "Verification",
            ),
            "decomposition": (
                "Summary",
                "Selected Downstream Formulas",
                "Implementation Convoy",
                "Work Items",
            ),
            "final-report": (
                "Summary",
                "Outcome",
                "Artifacts",
                "Remaining Risks",
            ),
        }
        sections: list[str] = []
        if artifact_kind == "final-report":
            if include_source_trace:
                acceptance_hash = (
                    source_acceptance_hash
                    if source_acceptance_hash is not None
                    else "sha256:" + hashlib.sha256(acceptance_criteria.encode("utf-8")).hexdigest()
                )
                sections.append(
                    "## Source trace\n\n"
                    f"Source ID: {source_id}\n"
                    f"Source title: {source_title}\n"
                    f"Acceptance criteria SHA-256: {acceptance_hash}"
                )
        else:
            sections.append(f"## Source Intent\n\n{source_id} — {source_title}")
        sections.extend(
            f"## {name}\n\n{name} content."
            for name in required_sections[artifact_kind]
        )
        sections.append(
            "## Coverage\n\n| ID | Status |\n| --- | --- |\n| REQ-1 | covered |"
        )
        return (
            "---\n"
            f"schema: gc.build.{artifact_kind}.v1\n"
            "workflow:\n"
            "  id: workflow-1\n"
            "  formula: complete-delivery\n"
            "methodology:\n"
            "  pack: complete-delivery\n"
            "  name: complete-delivery\n"
            "producer:\n"
            "  formula: complete-delivery\n"
            f"  stage: {artifact_kind}\n"
            "  attempt: 1\n"
            "status: approved\n"
            f"{source}"
            "trace:\n"
            "  upstream:\n"
            "    - path: source.md\n"
            "      hash: sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
            "      ids: [REQ-1]\n"
            "  coverage:\n"
            "    - id: REQ-1\n"
            "      status: covered\n"
            "---\n\n"
            + "\n\n".join(sections)
            + "\n"
        )

    def run_check(
        self,
        artifact_text: str,
        *,
        artifact_kind: str = "requirements",
        missing_pyyaml: bool = False,
        generic_check: pathlib.Path | None = SOURCE_ARTIFACT_GENERIC_CHECK,
        source_json: object | None = None,
        source_title: str = "Requested delivery",
        upstream_overrides: dict[str, str | None] | None = None,
        artifact_path_variant: str = "contained",
        upstream_path_variant: tuple[str, str] | None = None,
        artifact_path_keys: str | None = None,
        use_variable_artifact_path: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            work_dir = root / "work"
            work_dir.mkdir()

            def write_artifact(
                kind: str, text: str, variant: str
            ) -> tuple[pathlib.Path, str]:
                if variant == "contained":
                    path = work_dir / f"{kind}.md"
                    value = str(path)
                elif variant == "parent":
                    path = root / f"outside-{kind}.md"
                    value = f"../outside-{kind}.md"
                elif variant == "nested-parent":
                    (work_dir / "nested").mkdir(exist_ok=True)
                    path = root / f"outside-{kind}.md"
                    value = f"nested/../../outside-{kind}.md"
                elif variant == "absolute-outside":
                    path = root / f"outside-{kind}.md"
                    value = str(path)
                elif variant == "symlink":
                    outside_dir = root / f"outside-{kind}"
                    outside_dir.mkdir()
                    path = outside_dir / f"{kind}.md"
                    link = work_dir / f"linked-{kind}"
                    link.symlink_to(outside_dir, target_is_directory=True)
                    value = f"linked-{kind}/{kind}.md"
                else:
                    raise AssertionError(f"unknown artifact path variant: {variant}")
                path.write_text(text, encoding="utf-8")
                return path, value

            _artifact, artifact_path_value = write_artifact(
                artifact_kind, artifact_text, artifact_path_variant
            )
            bin_dir = root / "bin"
            bin_dir.mkdir()
            gc = bin_dir / "gc"
            gc.write_text(
                "#!/bin/sh\n"
                "case \"${3:-}\" in\n"
                "  step-1) printf '%s\\n' \"$FAKE_STEP_JSON\" ;;\n"
                "  root-1) printf '%s\\n' \"$FAKE_ROOT_JSON\" ;;\n"
                "  fi-123) printf '%s\\n' \"$FAKE_SOURCE_JSON\" ;;\n"
                "  *) exit 1 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            gc.chmod(0o755)
            if missing_pyyaml:
                python3 = bin_dir / "python3"
                python3.write_text(
                    "#!/bin/sh\n"
                    "if [ \"${1:-}\" = \"-c\" ] && [ \"${2:-}\" = \"import yaml\" ]; then\n"
                    "  exit 1\n"
                    "fi\n"
                    f"exec {sys.executable} \"$@\"\n",
                    encoding="utf-8",
                )
                python3.chmod(0o755)
            artifact_path_key = f"gc.build.{artifact_kind}_path"
            step = [{"id": "step-1", "metadata": {
                "gc.root_bead_id": "root-1",
                "gc.build.artifact_schema": f"gc.build.{artifact_kind}.v1",
                "gc.build.artifact_path_keys": artifact_path_keys or artifact_path_key,
            }}]
            root_metadata = {
                artifact_path_key: artifact_path_value,
                "gc.var.source_bead_id": "fi-123",
                "gc.var.source_title": source_title,
                **(
                    {"gc.var.build_artifact_valid_path": str(generic_check)}
                    if generic_check is not None
                    else {}
                ),
            }
            if use_variable_artifact_path:
                root_metadata.pop(artifact_path_key)
                root_metadata[f"gc.var.{artifact_kind.replace('-', '_')}_path"] = (
                    artifact_path_value
                )
            if artifact_kind == "final-report":
                upstream_overrides = upstream_overrides or {}
                for upstream_kind in ("requirements", "plan", "decomposition"):
                    upstream_text = upstream_overrides.get(
                        upstream_kind,
                        self.artifact(
                            artifact_kind=upstream_kind,
                            source_title=source_title,
                        ),
                    )
                    if upstream_text is None:
                        continue
                    upstream_variant = (
                        upstream_path_variant[1]
                        if upstream_path_variant
                        and upstream_path_variant[0] == upstream_kind
                        else "contained"
                    )
                    _, upstream_path_value = write_artifact(
                        upstream_kind, upstream_text, upstream_variant
                    )
                    root_metadata[f"gc.build.{upstream_kind}_path"] = (
                        upstream_path_value
                    )
            workflow_root = [{"id": "root-1", "metadata": root_metadata}]
            source = (
                source_json
                if source_json is not None
                else [{
                    "id": "fi-123",
                    "title": source_title,
                    "acceptance_criteria": "The requested outcome is delivered.",
                }]
            )
            environment = os.environ.copy()
            environment.update({
                "GC_BEAD_ID": "step-1",
                "GC_WORK_DIR": str(work_dir),
                "FAKE_STEP_JSON": json.dumps(step),
                "FAKE_ROOT_JSON": json.dumps(workflow_root),
                "FAKE_SOURCE_JSON": json.dumps(source),
                "PATH": f"{bin_dir}:{environment['PATH']}",
            })
            return subprocess.run(
                ["bash", str(SOURCE_ARTIFACT_SCRIPT)],
                capture_output=True,
                text=True,
                env=environment,
            )

    def test_source_artifact_requires_exact_durable_binding(self) -> None:
        for artifact_kind in ("requirements", "plan", "decomposition"):
            with self.subTest(artifact_kind=artifact_kind):
                valid = self.run_check(
                    self.artifact(artifact_kind=artifact_kind),
                    artifact_kind=artifact_kind,
                )
                self.assertEqual(valid.returncode, 0, valid.stdout + valid.stderr)
                self.assertIn("source artifact valid", valid.stdout)

        for artifact, message in (
            (self.artifact(include_source=False), "requires a source mapping"),
            (self.artifact(source_id="fi-wrong"), "source.id must equal"),
            (self.artifact(source_title="Wrong title"), "source.title must equal"),
            (self.artifact(source_anchor="gc:fi-wrong"), "source.anchor must equal"),
            (
                self.artifact(source_acceptance_hash="sha256:" + "0" * 64),
                "source.acceptance_criteria_sha256 must equal",
            ),
        ):
            with self.subTest(message=message):
                result = self.run_check(artifact)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)

    def test_source_and_upstream_artifacts_cannot_escape_work_directory(self) -> None:
        for variant in ("parent", "nested-parent", "absolute-outside", "symlink"):
            with self.subTest(source_artifact=variant):
                result = self.run_check(
                    self.artifact(), artifact_path_variant=variant
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    "must resolve within the canonical delivery work directory",
                    result.stderr,
                )

        for variant in ("parent", "nested-parent", "absolute-outside", "symlink"):
            with self.subTest(upstream_artifact=variant):
                result = self.run_check(
                    self.artifact(artifact_kind="final-report"),
                    artifact_kind="final-report",
                    upstream_path_variant=("plan", variant),
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    "must resolve within the canonical delivery work directory",
                    result.stderr,
                )

    def test_source_artifact_requires_pyyaml_at_runtime(self) -> None:
        result = self.run_check(self.artifact(), missing_pyyaml=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PyYAML is required for Complete Delivery", result.stderr)

    def test_all_source_artifacts_require_durable_acceptance_criteria(self) -> None:
        for artifact_kind in ("requirements", "plan", "decomposition", "final-report"):
            with self.subTest(artifact_kind=artifact_kind):
                result = self.run_check(
                    self.artifact(artifact_kind=artifact_kind),
                    artifact_kind=artifact_kind,
                    source_json=[{"id": "fi-123", "title": "Requested delivery"}],
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    "does not exactly match configured source identity",
                    result.stderr,
                )

    def test_source_artifact_fails_closed_without_adjacent_or_explicit_generic_checker(self) -> None:
        result = self.run_check(self.artifact(), generic_check=None)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("build-artifact-valid.sh is unavailable", result.stderr)

    def test_source_intent_heading_inside_fence_is_rejected(self) -> None:
        source_id = "fi-123"
        source_title = "Requested delivery"
        heading = f"## Source Intent\n\n{source_id} — {source_title}"
        for artifact_kind in ("requirements", "plan", "decomposition"):
            artifact = self.artifact(
                artifact_kind=artifact_kind,
                source_id=source_id,
                source_title=source_title,
            ).replace(
                heading,
                f"```markdown\n{heading}\n```",
            )
            with self.subTest(artifact_kind=artifact_kind):
                result = self.run_check(artifact, artifact_kind=artifact_kind)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("requires a Source Intent section", result.stderr)

    def test_source_intent_heading_cannot_span_lines(self) -> None:
        source_id = "fi-123"
        source_title = "Requested delivery"
        heading = f"## Source Intent\n\n{source_id} — {source_title}"
        for artifact_kind in ("requirements", "plan", "decomposition"):
            artifact = self.artifact(
                artifact_kind=artifact_kind,
                source_id=source_id,
                source_title=source_title,
            ).replace(
                heading,
                f"##\nSource Intent\n\n{source_id} — {source_title}",
            )
            with self.subTest(artifact_kind=artifact_kind):
                result = self.run_check(artifact, artifact_kind=artifact_kind)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("requires a Source Intent section", result.stderr)

    def test_final_report_requires_exact_visible_source_trace(self) -> None:
        valid = self.run_check(
            self.artifact(artifact_kind="final-report"),
            artifact_kind="final-report",
        )
        self.assertEqual(valid.returncode, 0, valid.stdout + valid.stderr)
        self.assertIn("final-report", valid.stdout)

        fallback = self.run_check(
            self.artifact(artifact_kind="final-report"),
            artifact_kind="final-report",
            artifact_path_keys="gc.build.final_report_path,gc.var.final_report_path",
            use_variable_artifact_path=True,
        )
        self.assertEqual(fallback.returncode, 0, fallback.stdout + fallback.stderr)

        missing_trace = self.run_check(
            self.artifact(artifact_kind="final-report", include_source_trace=False),
            artifact_kind="final-report",
        )
        self.assertNotEqual(missing_trace.returncode, 0)
        self.assertIn("requires an unfenced Source trace section", missing_trace.stderr)

        fenced_trace = self.artifact(artifact_kind="final-report").replace(
            "## Source trace",
            "```markdown\n## Source trace",
        ).replace(
            "## Summary",
            "```\n\n## Summary",
        )
        fenced = self.run_check(fenced_trace, artifact_kind="final-report")
        self.assertNotEqual(fenced.returncode, 0)
        self.assertIn("requires an unfenced Source trace section", fenced.stderr)

        exact_artifact = self.artifact(artifact_kind="final-report")
        exact_hash = "sha256:" + hashlib.sha256(
            b"The requested outcome is delivered."
        ).hexdigest()
        source_id_line = "Source ID: fi-123"
        source_title_line = "Source title: Requested delivery"
        source_hash_line = f"Acceptance criteria SHA-256: {exact_hash}"
        malformed_lines = {
            "prefixed source id": exact_artifact.replace(
                "Source ID: fi-123",
                "prefix Source ID: fi-123",
            ),
            "suffixed source title": exact_artifact.replace(
                "Source title: Requested delivery",
                "Source title: Requested delivery suffix",
            ),
            "trailing whitespace source id": exact_artifact.replace(
                source_id_line,
                source_id_line + " ",
            ),
            "wrong visible hash": exact_artifact.replace(
                f"Acceptance criteria SHA-256: {exact_hash}",
                "Acceptance criteria SHA-256: sha256:" + "0" * 64,
            ),
            "duplicate source id": exact_artifact.replace(
                source_id_line,
                f"{source_id_line}\n{source_id_line}",
            ),
            "conflicting source id": exact_artifact.replace(
                source_id_line,
                f"{source_id_line}\nSource ID: fi-forged",
            ),
            "duplicate source title": exact_artifact.replace(
                source_title_line,
                f"{source_title_line}\n{source_title_line}",
            ),
            "conflicting source title": exact_artifact.replace(
                source_title_line,
                f"{source_title_line}\nSource title: Forged delivery",
            ),
            "duplicate source hash": exact_artifact.replace(
                source_hash_line,
                f"{source_hash_line}\n{source_hash_line}",
            ),
            "conflicting source hash": exact_artifact.replace(
                source_hash_line,
                source_hash_line
                + "\nAcceptance criteria SHA-256: sha256:"
                + "0" * 64,
            ),
            "indented source id": exact_artifact.replace(
                source_id_line,
                f"  {source_id_line}",
            ),
            "malformed source id label": exact_artifact.replace(
                source_id_line,
                "Source ID : fi-123",
            ),
        }
        for name, malformed in malformed_lines.items():
            with self.subTest(name=name):
                result = self.run_check(malformed, artifact_kind="final-report")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Source trace must contain exactly one", result.stderr)

        reserved_label_title = "Clarify Source ID: handling"
        reserved_label = self.run_check(
            self.artifact(
                artifact_kind="final-report",
                source_title=reserved_label_title,
            ),
            artifact_kind="final-report",
            source_title=reserved_label_title,
        )
        self.assertEqual(
            reserved_label.returncode,
            0,
            reserved_label.stdout + reserved_label.stderr,
        )

    def test_final_report_requires_exact_acceptance_hash(self) -> None:
        for hash_value in ("", "sha256:" + "0" * 64):
            with self.subTest(hash_value=hash_value or "missing"):
                result = self.run_check(
                    self.artifact(
                        artifact_kind="final-report",
                        source_acceptance_hash=hash_value,
                    ),
                    artifact_kind="final-report",
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("acceptance_criteria_sha256 must equal", result.stderr)

    def test_final_report_requires_all_approved_upstream_source_bindings(self) -> None:
        wrong_hash = "sha256:" + "0" * 64
        mismatched_plan = self.run_check(
            self.artifact(artifact_kind="final-report"),
            artifact_kind="final-report",
            upstream_overrides={
                "plan": self.artifact(
                    artifact_kind="plan",
                    source_acceptance_hash=wrong_hash,
                )
            },
        )
        self.assertNotEqual(mismatched_plan.returncode, 0)
        self.assertIn(
            "approved gc.build.plan.v1 artifact source.acceptance_criteria_sha256 must equal",
            mismatched_plan.stderr,
        )

        missing_decomposition = self.run_check(
            self.artifact(artifact_kind="final-report"),
            artifact_kind="final-report",
            upstream_overrides={"decomposition": None},
        )
        self.assertNotEqual(missing_decomposition.returncode, 0)
        self.assertIn(
            "gc.build.decomposition_path is required to finalize source traceability",
            missing_decomposition.stderr,
        )

        unapproved_requirements = self.run_check(
            self.artifact(artifact_kind="final-report"),
            artifact_kind="final-report",
            upstream_overrides={
                "requirements": self.artifact(artifact_kind="requirements").replace(
                    "status: approved",
                    "status: draft",
                    1,
                )
            },
        )
        self.assertNotEqual(unapproved_requirements.returncode, 0)
        self.assertIn(
            "approved gc.build.requirements.v1 artifact status must equal 'approved'",
            unapproved_requirements.stderr,
        )

    def test_final_report_requires_exact_complete_source_record(self) -> None:
        artifact = self.artifact(artifact_kind="final-report")
        cases = (
            (
                [{
                    "title": "Requested delivery",
                    "acceptance_criteria": "The requested outcome is delivered.",
                }],
                "missing id",
            ),
            (
                [{
                    "id": "fi-wrong",
                    "title": "Requested delivery",
                    "acceptance_criteria": "The requested outcome is delivered.",
                }],
                "mismatched id",
            ),
            (
                [{
                    "id": "fi-123",
                    "title": "Requested delivery",
                    "acceptance_criteria": " \t\n",
                }],
                "blank acceptance criteria",
            ),
            (
                [
                    {
                        "id": "fi-123",
                        "title": "Requested delivery",
                        "acceptance_criteria": "The requested outcome is delivered.",
                    },
                    {
                        "id": "fi-123",
                        "title": "Requested delivery",
                        "acceptance_criteria": "The requested outcome is delivered.",
                    },
                ],
                "ambiguous source",
            ),
        )
        for source_json, name in cases:
            with self.subTest(name=name):
                result = self.run_check(
                    artifact,
                    artifact_kind="final-report",
                    source_json=source_json,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("does not exactly match configured source identity", result.stderr)


if __name__ == "__main__":
    unittest.main()
