from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "gascity/roles/assets/scripts/p0_notice.py"


class NoticeControlPlaneTests(unittest.TestCase):
    def run_notice(self, *args: str, index: pathlib.Path) -> subprocess.CompletedProcess[str]:
        env = os.environ | {"GC_P0_NOTICE_INDEX": str(index), "PATH": "/nonexistent"}
        return subprocess.run([sys.executable, str(SCRIPT), *args], text=True, capture_output=True, env=env)

    def test_invalid_arguments_are_rejected_before_subprocess(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_notice("send", "--to-role", "bad role", "--work-ref", "gp-1",
                "--state-fingerprint", "head", "--subject", "x", "--message", "x",
                index=pathlib.Path(directory) / "index.json")
        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid role", result.stderr)

    def test_ack_nonrecipient_does_not_mutate_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            index = pathlib.Path(directory) / "index.json"
            nid = "a" * 24
            index.write_text(json.dumps({"schema_version": 1, "notices": {nid: {
                "recipient_role": "gastown.mayor", "disposition": "accepted"}}}))
            result = self.run_notice("ack", nid, "--actor-role", "gastown.witness",
                "--disposition", "accepted", index=index)
            record = json.loads(index.read_text())["notices"][nid]
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("processed_at", record)

    def test_accepted_but_unprocessed_notice_remains_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            index = pathlib.Path(directory) / "index.json"
            nid = "f" * 24
            index.write_text(json.dumps({"schema_version": 1, "notices": {nid: {
                "recipient_role": "gastown.mayor", "work_reference": "gp-1",
                "state_fingerprint": "head", "disposition": "accepted",
                "occurrence_count": 1}}}))
            # Use the script's deterministic id so the second send reaches the
            # existing durable receipt rather than attempting a new gc process.
            import hashlib
            nid = hashlib.sha256(b"gastown.mayor\0gp-1\0head").hexdigest()[:24]
            record = json.loads(index.read_text())["notices"].pop("f" * 24)
            index.write_text(json.dumps({"schema_version": 1, "notices": {nid: record}}))
            result = self.run_notice("send", "--to-role", "gastown.mayor", "--work-ref", "gp-1",
                "--state-fingerprint", "head", "--subject", "x", "--message", "x", index=index)
            record = json.loads(index.read_text())["notices"][nid]
        self.assertEqual(result.returncode, 0)
        self.assertEqual(record["occurrence_count"], 2)

    def test_ack_requires_a_durable_mail_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            index = pathlib.Path(directory) / "index.json"
            nid = "a" * 24
            index.write_text(json.dumps({"schema_version": 1, "notices": {nid: {
                "recipient_role": "gastown.mayor", "disposition": "accepted"}}}))
            result = self.run_notice("ack", nid, "--actor-role", "gastown.mayor",
                "--disposition", "accepted", index=index)
        self.assertEqual(result.returncode, 2)
        self.assertIn("no durable mail receipt", result.stderr)

    def test_reconcile_prunes_old_processed_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            index = pathlib.Path(directory) / "index.json"
            nid = "a" * 24
            index.write_text(json.dumps({"schema_version": 1, "notices": {nid: {
                "processed_at": "2000-01-01T00:00:00Z", "disposition": "accepted"}}}))
            result = self.run_notice("reconcile", "--retention-seconds", "1", index=index)
            records = json.loads(index.read_text())["notices"]
        self.assertEqual(result.returncode, 0)
        self.assertEqual(records, {})

    def test_contract_files_describe_bounded_durable_policy(self) -> None:
        command = (ROOT / "gascity/roles/commands/notice/command.toml").read_text()
        order = (ROOT / "gascity/roles/orders/p0-notice-reconcile.toml").read_text()
        prompt = (ROOT / "gascity/roles/template-fragments/p0-control-plane.template.md").read_text()
        self.assertIn("p0_notice.py", command)
        self.assertIn("--limit 50", order)
        self.assertIn("five minutes", prompt)
        self.assertIn("durable completion, handoff, review,", prompt)

    def test_implementation_uses_atomic_locked_argument_vector_io(self) -> None:
        source = SCRIPT.read_text()
        self.assertIn("fcntl.LOCK_EX", source)
        self.assertIn("os.replace", source)
        self.assertIn("shell=False", source)
        self.assertIn("0o640", source)
        self.assertIn("occurrence_count", source)
        self.assertIn("processed_at", source)
        self.assertIn("recover_mail_id", source)
        self.assertIn("title=notice:", source)


if __name__ == "__main__":
    unittest.main()
