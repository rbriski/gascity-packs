#!/usr/bin/env python3
"""Publish only a rendered delivery report bundle to a curated directory."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path


SAFE_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
FILES = ("index.html", "styles.css")
MAX_FILE_BYTES = 2_000_000


class PublishError(RuntimeError):
    pass


def reject_symlinked_ancestors(path: Path) -> Path:
    """Return a lexical absolute path after rejecting every symlink component."""
    path = path.expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if ".." in path.parts:
        raise PublishError(f"report destination must not contain parent traversal: {path}")

    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            # No descendant can exist while this component is absent outside a race.
            break
        if stat.S_ISLNK(mode):
            raise PublishError(
                f"report destination must not have a symlinked ancestor: {current}"
            )
    return path


def atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def publish_bundle(destination: Path, payloads: dict[str, bytes]) -> None:
    """Publish a complete bundle, retaining the previous bundle on write failure."""
    stage = Path(tempfile.mkdtemp(prefix=f".{destination.name}.stage-", dir=destination.parent))
    try:
        for name, content in payloads.items():
            atomic_bytes(stage / name, content)

        # The staging writes can take long enough for a caller-controlled path
        # to change. Validate the final parent and target again before swapping.
        reject_symlinked_ancestors(destination.parent)
        if destination.is_symlink():
            raise PublishError(f"report destination must not be a symlink: {destination}")
        if destination.exists() and not destination.is_dir():
            raise PublishError(f"report destination is not a directory: {destination}")
        if not destination.exists():
            os.replace(stage, destination)
            return

        backup = Path(
            tempfile.mkdtemp(prefix=f".{destination.name}.backup-", dir=destination.parent)
        )
        backup.rmdir()
        try:
            os.replace(destination, backup)
            try:
                os.replace(stage, destination)
            except BaseException:
                os.replace(backup, destination)
                raise
        except BaseException:
            raise
        else:
            shutil.rmtree(backup)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def publish(source: Path, destination_root: Path, slug: str = "") -> Path:
    source = source.expanduser()
    destination_root = reject_symlinked_ancestors(destination_root)
    if source.is_symlink():
        raise PublishError(f"report source must not be a symlink: {source}")
    source = source.resolve()
    if not source.is_dir():
        raise PublishError(f"report source is not a directory: {source}")
    slug = slug.strip() or source.parent.name
    if not SAFE_SLUG.fullmatch(slug):
        raise PublishError(f"unsafe report slug: {slug!r}")
    destination_root.mkdir(parents=True, exist_ok=True)
    destination_root = reject_symlinked_ancestors(destination_root)
    unresolved_destination = destination_root / slug
    if unresolved_destination.is_symlink():
        raise PublishError(f"report destination must not be a symlink: {unresolved_destination}")
    destination = unresolved_destination
    if destination.exists() and not destination.is_dir():
        raise PublishError(f"report destination is not a directory: {destination}")

    payloads: dict[str, bytes] = {}
    for name in FILES:
        path = source / name
        if path.is_symlink() or not path.is_file():
            raise PublishError(f"required report file is missing or unsafe: {path}")
        content = path.read_bytes()
        if not content or len(content) > MAX_FILE_BYTES:
            raise PublishError(f"report file has invalid size: {path}")
        payloads[name] = content
    publish_bundle(destination, payloads)
    return destination


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination-root", type=Path, required=True)
    parser.add_argument("--slug", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        destination = publish(args.source, args.destination_root, args.slug)
    except (OSError, PublishError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps({"ok": True, "destination": str(destination)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
