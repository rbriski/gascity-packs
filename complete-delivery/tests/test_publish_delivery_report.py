from __future__ import annotations

import importlib.util
import os
import pathlib
import tempfile
import unittest
from unittest import mock


MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "assets"
    / "scripts"
    / "publish_delivery_report.py"
)
SPEC = importlib.util.spec_from_file_location("publish_delivery_report", MODULE_PATH)
assert SPEC and SPEC.loader
publisher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publisher)


class PublishDeliveryReportTests(unittest.TestCase):
    def write_bundle(self, source: pathlib.Path, marker: str = "") -> None:
        source.mkdir(parents=True, exist_ok=True)
        (source / "index.html").write_text(f"<html>{marker}</html>", encoding="utf-8")
        (source / "styles.css").write_text(f"body{{{marker}}}", encoding="utf-8")

    def test_publishes_only_rendered_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "plans" / "fi-123" / "delivery-report"
            source.mkdir(parents=True)
            (source / "index.html").write_text("<html>safe</html>", encoding="utf-8")
            (source / "styles.css").write_text("body{}", encoding="utf-8")
            (source / "state.json").write_text('{"secret":"not published"}', encoding="utf-8")
            destination = publisher.publish(source, root / "public")
            self.assertEqual(destination.name, "fi-123")
            self.assertTrue((destination / "index.html").is_file())
            self.assertTrue((destination / "styles.css").is_file())
            self.assertFalse((destination / "state.json").exists())

    def test_rejects_unsafe_slug(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "source"
            source.mkdir()
            for name in publisher.FILES:
                (source / name).write_text("x", encoding="utf-8")
            with self.assertRaises(publisher.PublishError):
                publisher.publish(source, root / "public", "../escape")

    def test_rejects_symlinked_bundle_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "source"
            source.mkdir()
            target = root / "outside.html"
            target.write_text("outside", encoding="utf-8")
            (source / "index.html").symlink_to(target)
            (source / "styles.css").write_text("body{}", encoding="utf-8")
            with self.assertRaises(publisher.PublishError):
                publisher.publish(source, root / "public", "safe")

    def test_rejects_non_regular_bundle_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "source"
            source.mkdir()
            os.mkfifo(source / "index.html")
            (source / "styles.css").write_text("body{}", encoding="utf-8")

            with self.assertRaises(publisher.PublishError):
                publisher.publish(source, root / "public", "safe")

    def test_rejects_empty_bundle_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "index.html").write_bytes(b"")
            (source / "styles.css").write_text("body{}", encoding="utf-8")

            with self.assertRaises(publisher.PublishError):
                publisher.publish(source, root / "public", "safe")

    def test_rejects_oversized_bundle_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "index.html").write_bytes(b"x" * (publisher.MAX_FILE_BYTES + 1))
            (source / "styles.css").write_text("body{}", encoding="utf-8")

            with self.assertRaises(publisher.PublishError):
                publisher.publish(source, root / "public", "safe")

    def test_rejects_growth_after_fstat_without_unbounded_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "source"
            source.mkdir()
            report = source / "index.html"
            report.write_bytes(b"x" * publisher.MAX_FILE_BYTES)
            (source / "styles.css").write_text("body{}", encoding="utf-8")
            original_fstat = publisher.os.fstat
            original_read = publisher.os.read
            grew = False
            bytes_read = 0

            def grow_after_fstat(descriptor: int) -> os.stat_result:
                nonlocal grew
                result = original_fstat(descriptor)
                if not grew:
                    grew = True
                    with report.open("ab") as handle:
                        handle.write(b"y")
                return result

            def count_read(descriptor: int, size: int) -> bytes:
                nonlocal bytes_read
                chunk = original_read(descriptor, size)
                bytes_read += len(chunk)
                return chunk

            with (
                mock.patch.object(publisher.os, "fstat", side_effect=grow_after_fstat),
                mock.patch.object(publisher.os, "read", side_effect=count_read),
            ):
                with self.assertRaises(publisher.PublishError):
                    publisher.publish(source, root / "public", "safe")

            self.assertLessEqual(bytes_read, publisher.MAX_FILE_BYTES + 1)

    def test_rejects_symlinked_source_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "source"
            source.mkdir()
            for name in publisher.FILES:
                (source / name).write_text("x", encoding="utf-8")
            linked_source = root / "linked-source"
            linked_source.symlink_to(source, target_is_directory=True)
            with self.assertRaises(publisher.PublishError):
                publisher.publish(linked_source, root / "public", "safe")

    def test_rejects_symlinked_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "source"
            source.mkdir()
            for name in publisher.FILES:
                (source / name).write_text("x", encoding="utf-8")
            destination_root = root / "public"
            destination_root.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (destination_root / "safe").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(publisher.PublishError):
                publisher.publish(source, destination_root, "safe")

    def test_rejects_symlinked_destination_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "source"
            self.write_bundle(source)
            outside = root / "outside"
            outside.mkdir()
            ancestor = root / "ancestor-link"
            ancestor.symlink_to(outside, target_is_directory=True)

            with self.assertRaises(publisher.PublishError):
                publisher.publish(source, ancestor / "curated", "proof")

            self.assertFalse((outside / "curated" / "proof").exists())

    def test_preserves_existing_bundle_when_staging_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "source"
            self.write_bundle(source, "new")
            destination = root / "public" / "safe"
            self.write_bundle(destination, "old")
            original_write_stage_file = publisher.write_stage_file
            calls = 0

            def fail_second_write(stage_fd: int, name: str, content: bytes) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated second write failure")
                original_write_stage_file(stage_fd, name, content)

            with mock.patch.object(publisher, "write_stage_file", side_effect=fail_second_write):
                with self.assertRaises(OSError):
                    publisher.publish(source, root / "public", "safe")

            self.assertEqual((destination / "index.html").read_text(encoding="utf-8"), "<html>old</html>")
            self.assertEqual((destination / "styles.css").read_text(encoding="utf-8"), "body{old}")
            self.assertEqual(list((root / "public").glob(".safe.*")), [])

    def test_staging_failure_leaves_no_first_publish_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "source"
            self.write_bundle(source, "new")
            original_write_stage_file = publisher.write_stage_file
            calls = 0

            def fail_second_write(stage_fd: int, name: str, content: bytes) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated second write failure")
                original_write_stage_file(stage_fd, name, content)

            with mock.patch.object(publisher, "write_stage_file", side_effect=fail_second_write):
                with self.assertRaises(OSError):
                    publisher.publish(source, root / "public", "safe")

            self.assertFalse((root / "public" / "safe").exists())
            self.assertEqual(list((root / "public").iterdir()), [])

    def test_commit_failure_leaves_no_first_publish_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "source"
            self.write_bundle(source, "new")
            destination = root / "public" / "safe"
            original_rename = publisher.os.rename

            def fail_staged_commit(source_path: object, destination_path: object, **kwargs: object) -> None:
                if str(source_path).startswith(".safe.stage-"):
                    raise OSError("simulated bundle commit failure")
                original_rename(source_path, destination_path, **kwargs)

            with mock.patch.object(publisher.os, "rename", side_effect=fail_staged_commit):
                with self.assertRaises(OSError):
                    publisher.publish(source, root / "public", "safe")

            self.assertFalse(destination.exists())
            self.assertEqual(list((root / "public").iterdir()), [])

    def test_unsupported_atomic_replacement_leaves_existing_bundle_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "source"
            self.write_bundle(source, "new")
            destination = root / "public" / "safe"
            self.write_bundle(destination, "old")

            def unsupported_exchange(*args: object) -> int:
                return -1

            with (
                mock.patch.object(publisher, "_RENAMEAT2", side_effect=unsupported_exchange),
                mock.patch.object(publisher.ctypes, "get_errno", return_value=publisher.errno.EOPNOTSUPP),
            ):
                with self.assertRaisesRegex(publisher.PublishError, "unsupported"):
                    publisher.publish(source, root / "public", "safe")

            self.assertEqual((destination / "index.html").read_text(encoding="utf-8"), "<html>old</html>")
            self.assertEqual((destination / "styles.css").read_text(encoding="utf-8"), "body{old}")
            self.assertEqual(list((root / "public").glob(".safe.*")), [])

            with (
                mock.patch.object(publisher, "_RENAMEAT2", side_effect=unsupported_exchange),
                mock.patch.object(publisher.ctypes, "get_errno", return_value=publisher.errno.EINVAL),
                self.assertRaises(OSError) as raised,
            ):
                publisher.rename_exchange(0, "left", "right")

            self.assertEqual(raised.exception.errno, publisher.errno.EINVAL)

    def test_atomic_exchange_never_removes_canonical_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "source"
            self.write_bundle(source, "new")
            destination = root / "public" / "safe"
            self.write_bundle(destination, "old")
            original_exchange = publisher.rename_exchange
            observations: list[dict[str, bytes]] = []

            def observe_exchange(parent_fd: int, stage: str, live: str) -> None:
                observations.append(
                    {name: (destination / name).read_bytes() for name in publisher.FILES}
                )
                original_exchange(parent_fd, stage, live)
                observations.append(
                    {name: (destination / name).read_bytes() for name in publisher.FILES}
                )

            with mock.patch.object(publisher, "rename_exchange", side_effect=observe_exchange):
                publisher.publish(source, root / "public", "safe")

            self.assertEqual(observations[0]["index.html"], b"<html>old</html>")
            self.assertEqual(observations[1]["index.html"], b"<html>new</html>")
            self.assertTrue(destination.is_dir())

    def test_parent_sync_failure_retains_complete_prior_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "source"
            self.write_bundle(source, "new")
            destination = root / "public" / "safe"
            self.write_bundle(destination, "old")
            prior_bundle = {
                name: (destination / name).read_bytes() for name in publisher.FILES
            }
            original_fsync = publisher.os.fsync
            original_exchange = publisher.rename_exchange
            exchange_completed = False
            parent_sync_failed = False

            def observe_exchange(parent_fd: int, stage: str, live: str) -> None:
                nonlocal exchange_completed
                original_exchange(parent_fd, stage, live)
                exchange_completed = True

            def fail_parent_sync(descriptor: int) -> None:
                nonlocal parent_sync_failed
                if exchange_completed and not parent_sync_failed:
                    parent_sync_failed = True
                    raise OSError("simulated parent fsync failure")
                original_fsync(descriptor)

            with (
                mock.patch.object(publisher, "rename_exchange", side_effect=observe_exchange),
                mock.patch.object(publisher.os, "fsync", side_effect=fail_parent_sync),
            ):
                with self.assertRaisesRegex(
                    publisher.PublishError,
                    r"parent durability sync failed; prior bundle retained as recoverable backup",
                ) as raised:
                    publisher.publish(source, root / "public", "safe")

            backups = list((root / "public").glob(".safe.stage-*"))
            self.assertTrue(destination.is_dir())
            self.assertEqual((destination / "index.html").read_bytes(), b"<html>new</html>")
            self.assertEqual(len(backups), 1)
            self.assertIn(backups[0].name, str(raised.exception))
            self.assertEqual(
                {name: (backups[0] / name).read_bytes() for name in publisher.FILES},
                prior_bundle,
            )

    def test_cleanup_failure_retains_prior_bundle_as_recovery_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "source"
            self.write_bundle(source, "new")
            destination = root / "public" / "safe"
            self.write_bundle(destination, "old")

            with mock.patch.object(publisher, "remove_tree", side_effect=OSError("cleanup failed")):
                with self.assertRaisesRegex(publisher.PublishError, "cleanup failed") as raised:
                    publisher.publish(source, root / "public", "safe")

            backups = list((root / "public").glob(".safe.stage-*"))
            self.assertTrue(destination.is_dir())
            self.assertEqual((destination / "index.html").read_bytes(), b"<html>new</html>")
            self.assertEqual(len(backups), 1)
            self.assertIn(backups[0].name, str(raised.exception))
            self.assertEqual((backups[0] / "index.html").read_bytes(), b"<html>old</html>")

    def test_destination_root_swap_before_staging_cannot_redirect_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "source"
            self.write_bundle(source, "new")
            destination_root = root / "public"
            destination_root.mkdir()
            self.write_bundle(destination_root / "safe", "old")
            outside = root / "outside"
            outside.mkdir()
            displaced_root = root / "displaced-public"
            original_create = publisher.create_private_directory
            swapped = False

            def swap_before_staging(parent_fd: int, prefix: str) -> tuple[str, int]:
                nonlocal swapped
                if prefix == "safe.stage-" and not swapped:
                    swapped = True
                    destination_root.rename(displaced_root)
                    destination_root.symlink_to(outside, target_is_directory=True)
                return original_create(parent_fd, prefix)

            with mock.patch.object(
                publisher, "create_private_directory", side_effect=swap_before_staging
            ):
                publisher.publish(source, destination_root, "safe")

            self.assertEqual(list(outside.iterdir()), [])
            self.assertEqual(
                (displaced_root / "safe" / "index.html").read_text(encoding="utf-8"),
                "<html>new</html>",
            )

    def test_destination_root_swap_during_commit_cannot_redirect_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "source"
            self.write_bundle(source, "new")
            destination_root = root / "public"
            destination_root.mkdir()
            self.write_bundle(destination_root / "safe", "old")
            outside = root / "outside"
            outside.mkdir()
            displaced_root = root / "displaced-public"
            original_exchange = publisher.rename_exchange
            swapped = False

            def swap_during_exchange(parent_fd: int, stage: str, live: str) -> None:
                nonlocal swapped
                if not swapped:
                    swapped = True
                    destination_root.rename(displaced_root)
                    destination_root.symlink_to(outside, target_is_directory=True)
                original_exchange(parent_fd, stage, live)

            with mock.patch.object(publisher, "rename_exchange", side_effect=swap_during_exchange):
                publisher.publish(source, destination_root, "safe")

            self.assertEqual(list(outside.iterdir()), [])
            self.assertEqual(
                (displaced_root / "safe" / "styles.css").read_text(encoding="utf-8"),
                "body{new}",
            )


if __name__ == "__main__":
    unittest.main()
