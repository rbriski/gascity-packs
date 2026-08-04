#!/usr/bin/env python3
"""Audit a Gas City for the local Gstack Lite operating contract."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import tomllib


STALE_SKILL_NAME = "complete-delivery.complete-delivery"
REQUIRED_PROVIDERS = {
    "sol-fast",
    "luna-economy",
    "claude-careful",
    "claude-review",
    "sol-rescue",
}


def load_toml(path: Path) -> dict:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def contains_complete_delivery(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            "complete-delivery" in str(key)
            or contains_complete_delivery(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(contains_complete_delivery(child) for child in value)
    return isinstance(value, str) and "complete-delivery" in value


def stale_skill_links(city: Path) -> list[Path]:
    found: list[Path] = []
    roots = [city / ".gc", city / ".agents", city / ".codex", city / ".claude"]
    for root in roots:
        if not root.exists():
            continue
        for directory, names, files in os.walk(root, followlinks=False):
            names[:] = [name for name in names if name not in {"cache", ".git"}]
            for name in names + files:
                if name != STALE_SKILL_NAME:
                    continue
                path = Path(directory) / name
                if not path.is_symlink():
                    continue
                target = os.readlink(path)
                if "/complete-delivery/skills/complete-delivery" in target:
                    found.append(path)
    return sorted(set(found))


def audit(city: Path, fix_stale_skills: bool) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    notes: list[str] = []
    city_toml_path = city / "city.toml"
    pack_toml_path = city / "pack.toml"
    if not city_toml_path.is_file() or not pack_toml_path.is_file():
        return [f"{city} is not a Gas City root with city.toml and pack.toml"], notes

    try:
        city_toml = load_toml(city_toml_path)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return [f"cannot read {city_toml_path}: {exc}"], notes
    try:
        pack_toml = load_toml(pack_toml_path)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return [f"cannot read {pack_toml_path}: {exc}"], notes

    imports = pack_toml.get("imports", {})
    if "gstack" not in imports:
        errors.append("city pack must import gstack")
    if contains_complete_delivery(imports):
        errors.append("city pack still contains an active Complete Delivery import")

    if contains_complete_delivery(city_toml):
        errors.append("city.toml still contains active Complete Delivery configuration")

    providers = city_toml.get("providers", {})
    missing_providers = sorted(REQUIRED_PROVIDERS - set(providers))
    if missing_providers:
        errors.append(f"missing provider aliases: {', '.join(missing_providers)}")

    fragments = city_toml.get("agent_defaults", {}).get("append_fragments", [])
    if "gstack-lite-policy" not in fragments:
        errors.append("agent_defaults.append_fragments must include gstack-lite-policy")

    default_imports = city_toml.get("defaults", {}).get("rig", {}).get("imports", {})
    if "gc" not in default_imports:
        errors.append("future rigs must inherit the gc roles import")

    rigs = city_toml.get("rigs", [])
    invalid_rigs = [rig for rig in rigs if not isinstance(rig, dict)] if isinstance(rigs, list) else [rigs]
    if invalid_rigs:
        errors.append("city.toml rigs must be an array of tables")
        rigs = []
    missing_gc = sorted(
        rig.get("name", "<unnamed>")
        for rig in rigs
        if "gc" not in rig.get("imports", {})
    )
    if missing_gc:
        errors.append(f"current rigs missing explicit gc roles import: {', '.join(missing_gc)}")

    unsafe_start = sorted(
        rig.get("name", "<unnamed>")
        for rig in rigs
        if rig.get("suspended_on_start") is not True
    )
    if unsafe_start:
        errors.append(
            "current rigs must set suspended_on_start=true: "
            + ", ".join(unsafe_start)
        )

    unsafe_session_caps = sorted(
        rig.get("name", "<unnamed>")
        for rig in rigs
        if type(rig.get("max_active_sessions")) is not int
        or not 1 <= rig["max_active_sessions"] <= 5
    )
    if unsafe_session_caps:
        errors.append(
            "current rigs must cap max_active_sessions between 1 and 5: "
            + ", ".join(unsafe_session_caps)
        )

    unsafe_writer_caps: list[str] = []
    for rig in rigs:
        polecat_patches = [
            patch
            for patch in rig.get("patches", [])
            if isinstance(patch, dict) and patch.get("agent") == "polecat"
        ]
        caps = []
        for patch in polecat_patches:
            pool = patch.get("pool")
            caps.append(pool.get("max") if isinstance(pool, dict) else None)
        if len(caps) != 1 or type(caps[0]) is not int or not 1 <= caps[0] <= 2:
            unsafe_writer_caps.append(rig.get("name", "<unnamed>"))
    if unsafe_writer_caps:
        errors.append(
            "current rigs must set one polecat pool cap between 1 and 2: "
            + ", ".join(sorted(unsafe_writer_caps))
        )

    patches = city_toml.get("patches", {})
    agent_patches = patches.get("agent", []) if isinstance(patches, dict) else []
    reviewer_patches = [
        patch
        for patch in agent_patches
        if isinstance(patch, dict)
        and patch.get("name") == "gc.implementation-reviewer"
    ]
    unsafe_reviewer_rigs = []
    for rig in rigs:
        rig_name = rig.get("name", "<unnamed>")
        matching = [patch for patch in reviewer_patches if patch.get("dir") == rig_name]
        if (
            len(matching) != 1
            or matching[0].get("provider") != "claude-review"
            or type(matching[0].get("max_active_sessions")) is not int
            or matching[0]["max_active_sessions"] != 1
        ):
            unsafe_reviewer_rigs.append(rig_name)
    if unsafe_reviewer_rigs:
        errors.append(
            "current rigs need exactly one Claude gc.implementation-reviewer patch: "
            + ", ".join(sorted(unsafe_reviewer_rigs))
        )

    known_rigs = {rig.get("name", "<unnamed>") for rig in rigs}
    unexpected_reviewer_scopes = sorted(
        str(patch.get("dir", "<city>"))
        for patch in reviewer_patches
        if patch.get("dir") not in known_rigs
    )
    if unexpected_reviewer_scopes:
        errors.append(
            "gc.implementation-reviewer patches target unknown scopes: "
            + ", ".join(unexpected_reviewer_scopes)
        )

    stale = stale_skill_links(city)
    if stale and fix_stale_skills:
        for path in stale:
            try:
                if not path.is_symlink() or (
                    "/complete-delivery/skills/complete-delivery"
                    not in os.readlink(path)
                ):
                    notes.append(f"skipped changed skill projection: {path}")
                    continue
                path.unlink()
            except FileNotFoundError:
                notes.append(f"stale skill symlink already absent: {path}")
            except OSError as exc:
                errors.append(f"could not remove stale skill symlink {path}: {exc}")
            else:
                notes.append(f"removed stale skill symlink: {path}")
        stale = stale_skill_links(city)
    if stale:
        errors.append(f"{len(stale)} stale Complete Delivery skill symlink(s) remain")
        notes.extend(str(path) for path in stale)

    return errors, notes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city", type=Path, default=Path(os.environ.get("GC_CITY", ".")))
    parser.add_argument(
        "--fix-stale-skills",
        action="store_true",
        help="unlink only exact stale Complete Delivery skill projections",
    )
    args = parser.parse_args()
    city = args.city.expanduser().resolve()
    errors, notes = audit(city, args.fix_stale_skills)
    for note in notes:
        print(f"NOTE {note}")
    if errors:
        for error in errors:
            print(f"FAIL {error}", file=sys.stderr)
        return 1
    print(f"PASS Gstack Lite city contract: {city}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
