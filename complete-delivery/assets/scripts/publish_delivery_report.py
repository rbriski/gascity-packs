#!/usr/bin/env python3
"""Publish only a rendered delivery report bundle to a curated directory."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from pathlib import Path


SAFE_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
FILES = ("index.html", "styles.css")
MAX_FILE_BYTES = 2_000_000


class PublishError(RuntimeError):
    pass


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


def publish(source: Path, destination_root: Path, slug: str = "") -> Path:
    source = source.expanduser()
    destination_root = destination_root.expanduser()
    if source.is_symlink():
        raise PublishError(f"report source must not be a symlink: {source}")
    if destination_root.is_symlink():
        raise PublishError(
            f"report destination root must not be a symlink: {destination_root}"
        )
    source = source.resolve()
    destination_root = destination_root.resolve()
    if not source.is_dir():
        raise PublishError(f"report source is not a directory: {source}")
    slug = slug.strip() or source.parent.name
    if not SAFE_SLUG.fullmatch(slug):
        raise PublishError(f"unsafe report slug: {slug!r}")
    destination_root.mkdir(parents=True, exist_ok=True)
    unresolved_destination = destination_root / slug
    if unresolved_destination.is_symlink():
        raise PublishError(f"report destination must not be a symlink: {unresolved_destination}")
    destination = unresolved_destination.resolve()
    try:
        destination.relative_to(destination_root)
    except ValueError as exc:
        raise PublishError("report destination escapes destination root") from exc
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
    for name, content in payloads.items():
        atomic_bytes(destination / name, content)
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
