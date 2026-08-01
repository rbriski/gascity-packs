#!/usr/bin/env python3
"""Publish only a rendered delivery report bundle to a curated directory."""

from __future__ import annotations

import argparse
import ctypes
import errno
import json
import os
import re
import secrets
import stat
from pathlib import Path


SAFE_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
FILES = ("index.html", "styles.css")
MAX_FILE_BYTES = 2_000_000
DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
SOURCE_FILE_FLAGS = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
READ_CHUNK_BYTES = 64 * 1024
RENAME_EXCHANGE = 0x2
_LIBC = ctypes.CDLL(None, use_errno=True)
try:
    _RENAMEAT2 = _LIBC.renameat2
except AttributeError:
    _RENAMEAT2 = None
else:
    _RENAMEAT2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    _RENAMEAT2.restype = ctypes.c_int


class PublishError(RuntimeError):
    pass


def rename_exchange(parent_fd: int, left: str, right: str) -> None:
    """Atomically exchange two names below one trusted directory descriptor.

    Replacement needs exchange semantics rather than two ordinary renames: the
    latter leaves the published name absent between moving the old bundle out
    of the way and moving the staged bundle in.  Do not emulate this operation
    when the platform lacks it; retaining the live bundle is safer than a
    non-atomic publish.
    """
    if _RENAMEAT2 is None:
        raise PublishError("atomic report bundle replacement is unsupported")

    if _RENAMEAT2(parent_fd, os.fsencode(left), parent_fd, os.fsencode(right), RENAME_EXCHANGE) == 0:
        return

    error_number = ctypes.get_errno()
    if error_number in (errno.ENOSYS, errno.EOPNOTSUPP):
        raise PublishError("atomic report bundle replacement is unsupported")
    raise OSError(error_number, os.strerror(error_number))


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


def open_destination_root(path: Path) -> tuple[Path, int]:
    """Create and open a destination directory without following path links.

    Each component is opened relative to its already-trusted parent descriptor.
    This deliberately avoids validating a pathname and then using it later: a
    caller can replace the visible pathname at any point, but never redirect an
    operation performed through the returned descriptor.
    """
    path = reject_symlinked_ancestors(path)
    descriptor = os.open(path.anchor, DIRECTORY_FLAGS)
    try:
        for part in path.parts[1:]:
            try:
                child = os.open(part, DIRECTORY_FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    # Another process created it. Opening with O_NOFOLLOW is
                    # the authoritative check that it is a real directory.
                    pass
                child = os.open(part, DIRECTORY_FLAGS, dir_fd=descriptor)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise PublishError(
                        f"report destination must not have a symlinked or non-directory ancestor: {path}"
                    ) from exc
                raise
            os.close(descriptor)
            descriptor = child
    except BaseException:
        os.close(descriptor)
        raise
    return path, descriptor


def create_private_directory(parent_fd: int, prefix: str) -> tuple[str, int]:
    """Create a private child directory using a trusted parent descriptor."""
    for _ in range(100):
        name = f".{prefix}{secrets.token_hex(16)}"
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            continue
        try:
            return name, os.open(name, DIRECTORY_FLAGS, dir_fd=parent_fd)
        except BaseException:
            try:
                os.rmdir(name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            raise
    raise PublishError("could not allocate private report staging directory")


def remove_tree(name: str, parent_fd: int) -> None:
    """Remove a publisher-owned directory tree without resolving its pathname."""
    try:
        descriptor = os.open(name, DIRECTORY_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        return
    try:
        for child in os.listdir(descriptor):
            child_stat = os.stat(child, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISDIR(child_stat.st_mode):
                remove_tree(child, descriptor)
            else:
                os.unlink(child, dir_fd=descriptor)
    finally:
        os.close(descriptor)
    os.rmdir(name, dir_fd=parent_fd)


def write_stage_file(stage_fd: int, name: str, content: bytes) -> None:
    descriptor = os.open(name, FILE_FLAGS, 0o600, dir_fd=stage_fd)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.unlink(name, dir_fd=stage_fd)
        except FileNotFoundError:
            pass
        raise


def read_source_file(source_fd: int, source: Path, name: str) -> bytes:
    """Read one required bundle file through a no-follow descriptor.

    The size checks deliberately happen on the opened descriptor, rather than
    a path that could be swapped after validation.  Reads are capped at one
    byte beyond MAX_FILE_BYTES so an input that grows after fstat cannot cause
    an unbounded allocation.
    """
    path = source / name
    try:
        descriptor = os.open(name, SOURCE_FILE_FLAGS, dir_fd=source_fd)
    except OSError as exc:
        raise PublishError(f"required report file is missing or unsafe: {path}") from exc
    try:
        initial = os.fstat(descriptor)
        if not stat.S_ISREG(initial.st_mode):
            raise PublishError(f"required report file is missing or unsafe: {path}")
        if initial.st_size <= 0 or initial.st_size > MAX_FILE_BYTES:
            raise PublishError(f"report file has invalid size: {path}")

        content = bytearray()
        while True:
            remaining = MAX_FILE_BYTES + 1 - len(content)
            if remaining == 0:
                raise PublishError(f"report file has invalid size: {path}")
            chunk = os.read(descriptor, min(READ_CHUNK_BYTES, remaining))
            if not chunk:
                break
            content.extend(chunk)

        final = os.fstat(descriptor)
        if (
            len(content) != initial.st_size
            or final.st_size != initial.st_size
            or not stat.S_ISREG(final.st_mode)
        ):
            raise PublishError(f"report file changed while reading: {path}")
        return bytes(content)
    finally:
        os.close(descriptor)


def publish_bundle(destination_name: str, destination_root_fd: int, payloads: dict[str, bytes]) -> None:
    """Publish a complete bundle, retaining the previous bundle on write failure."""
    stage, stage_fd = create_private_directory(destination_root_fd, f"{destination_name}.stage-")
    preserve_stage = False
    try:
        for name, content in payloads.items():
            write_stage_file(stage_fd, name, content)
        os.fsync(stage_fd)
        os.close(stage_fd)
        stage_fd = -1

        try:
            target_stat = os.stat(
                destination_name, dir_fd=destination_root_fd, follow_symlinks=False
            )
        except FileNotFoundError:
            target_stat = None
        if target_stat is not None and not stat.S_ISDIR(target_stat.st_mode):
            raise PublishError(f"report destination is not a directory: {destination_name}")
        if target_stat is None:
            os.rename(stage, destination_name, src_dir_fd=destination_root_fd, dst_dir_fd=destination_root_fd)
            os.fsync(destination_root_fd)
            return

        # This is a single namespace operation: pathname readers see either
        # the complete previous bundle or the complete staged bundle, never
        # an absent canonical name.  The old bundle is now at ``stage``.
        rename_exchange(destination_root_fd, stage, destination_name)
        try:
            # The exchange is not durable until the containing directory has
            # been synced.  Keep the old bundle if this step cannot complete.
            os.fsync(destination_root_fd)
        except BaseException as sync_error:
            preserve_stage = True
            raise PublishError(
                "report bundle exchange completed but parent durability sync failed; "
                f"prior bundle retained as recoverable backup: {stage}"
            ) from sync_error

        try:
            remove_tree(stage, destination_root_fd)
        except BaseException as cleanup_error:
            preserve_stage = True
            raise PublishError(
                "report bundle exchange completed but prior-bundle cleanup failed; "
                f"remaining recovery evidence: {stage}"
            ) from cleanup_error
    finally:
        if stage_fd != -1:
            os.close(stage_fd)
        if not preserve_stage:
            remove_tree(stage, destination_root_fd)


def publish(source: Path, destination_root: Path, slug: str = "") -> Path:
    source = source.expanduser()
    if source.is_symlink():
        raise PublishError(f"report source must not be a symlink: {source}")
    source = source.resolve()
    if not source.is_dir():
        raise PublishError(f"report source is not a directory: {source}")
    slug = slug.strip() or source.parent.name
    if not SAFE_SLUG.fullmatch(slug):
        raise PublishError(f"unsafe report slug: {slug!r}")
    source_fd = os.open(source, DIRECTORY_FLAGS)
    try:
        payloads = {name: read_source_file(source_fd, source, name) for name in FILES}
    finally:
        os.close(source_fd)

    destination_root, destination_root_fd = open_destination_root(destination_root)
    destination = destination_root / slug
    try:
        publish_bundle(slug, destination_root_fd, payloads)
    finally:
        os.close(destination_root_fd)
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
