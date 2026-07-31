from __future__ import annotations

import importlib.util
import pathlib
import tempfile
import unittest


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


if __name__ == "__main__":
    unittest.main()
