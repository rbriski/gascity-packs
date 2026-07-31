#!/usr/bin/env python3
"""Prepare a rig for Complete Delivery before a workflow is created.

Formula checks execute from the target repository, not from an installed pack.
This launcher-owned bootstrap makes that contract explicit and fail-closed: it
installs the small, declared set of managed check assets and validates the
target rig's resolved delivery profile before ``gc sling`` can pour a graph.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


class LaunchPreflightError(Exception):
    pass


DELIVERY_CHECKS = (
    "delivery-common.sh",
    "delivery-local-gates.sh",
    "delivery-merged.sh",
    "delivery-preflight.sh",
    "delivery-pr-approved.sh",
    "delivery-pr-open.sh",
    "delivery-release-verified.sh",
    "delivery-report-valid.sh",
)
GASCITY_CHECKS = ("build-artifact-valid.sh",)
GASCITY_SCRIPTS = ("validate_build_artifact.py",)
GASCITY_SCHEMAS = (
    "decomposition.v1.yaml",
    "final-report.v1.yaml",
    "implementation-summary.v1.yaml",
    "plan.v1.yaml",
    "requirements.v1.yaml",
    "review.v1.yaml",
)
MANIFEST_NAME = "complete-delivery-assets.json"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rig", required=True, help="target rig name")
    parser.add_argument(
        "--pack-root",
        type=Path,
        default=Path(os.environ.get("GC_PACK_DIR", "")),
        help="resolved Complete Delivery pack root (defaults to GC_PACK_DIR)",
    )
    return parser.parse_args(argv)


def run_gc_config() -> dict[str, Any]:
    result = subprocess.run(
        ["gc", "config", "show", "--json"], capture_output=True, text=True
    )
    if result.returncode:
        raise LaunchPreflightError(
            "could not resolve the durable city configuration: "
            + (result.stderr.strip() or result.stdout.strip())
        )
    try:
        payload = json.loads(result.stdout)
        return payload["config"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise LaunchPreflightError("gc config show --json returned an invalid config") from exc


def target_rig(config: dict[str, Any], name: str) -> dict[str, Any]:
    rigs = config.get("Rigs")
    if not isinstance(rigs, list):
        raise LaunchPreflightError("resolved city configuration has no rig list")
    for rig in rigs:
        if isinstance(rig, dict) and rig.get("Name") == name:
            return rig
    raise LaunchPreflightError(f"rig {name!r} is not registered in the resolved city configuration")


def inherited_gascity_root(pack_root: Path) -> Path:
    # complete-delivery -> gstack -> gascity are relative Pack V2 imports.
    # The source tree and the pack cache preserve that sibling layout.
    candidate = pack_root.parent / "gascity"
    if not (candidate / "assets" / "scripts" / "checks" / "build-artifact-valid.sh").is_file():
        raise LaunchPreflightError(
            "the inherited gascity validation assets are unavailable beside the "
            f"Complete Delivery pack ({candidate}); reinstall or repair the pack imports"
        )
    return candidate


def copy_managed(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise LaunchPreflightError(f"required managed asset is missing from the pack: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as handle:
        temporary = Path(handle.name)
        with source.open("rb") as input_handle:
            shutil.copyfileobj(input_handle, handle)
    source_mode = stat.S_IMODE(source.stat().st_mode)
    temporary.chmod(source_mode)
    temporary.replace(destination)


def materialize_assets(pack_root: Path, rig_root: Path) -> list[str]:
    if not pack_root.is_dir():
        raise LaunchPreflightError("GC_PACK_DIR must point to the Complete Delivery pack")
    gascity_root = inherited_gascity_root(pack_root)
    scripts_root = rig_root / ".gc" / "scripts"
    checks_root = scripts_root / "checks"
    installed: list[str] = []

    for name in DELIVERY_CHECKS:
        copy_managed(pack_root / "assets" / "scripts" / "checks" / name, checks_root / name)
        installed.append(f".gc/scripts/checks/{name}")
    for name in GASCITY_CHECKS:
        copy_managed(gascity_root / "assets" / "scripts" / "checks" / name, checks_root / name)
        installed.append(f".gc/scripts/checks/{name}")
    for name in GASCITY_SCRIPTS:
        copy_managed(gascity_root / "assets" / "scripts" / name, scripts_root / name)
        installed.append(f".gc/scripts/{name}")
    for name in GASCITY_SCHEMAS:
        copy_managed(gascity_root / "schemas" / "build" / name, rig_root / "schemas" / "build" / name)
        installed.append(f"schemas/build/{name}")

    manifest = {
        "owner": "complete-delivery",
        "version": 1,
        "assets": installed,
        "inherited_from": "gascity",
    }
    manifest_path = rig_root / ".gc" / MANIFEST_NAME
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return installed


def string_var(profile: dict[str, Any], name: str, default: str) -> str:
    value = profile.get(name, default)
    return value if isinstance(value, str) else str(value)


def validate_profile(profile: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    def require_bool(name: str, default: str) -> str:
        value = string_var(profile, name, default)
        if value not in {"true", "false"}:
            errors.append(f"{name} must be true or false (got {value or 'empty'})")
        return value

    def require_enum(name: str, default: str, allowed: set[str]) -> str:
        value = string_var(profile, name, default)
        if value not in allowed:
            errors.append(f"{name} must be one of: {', '.join(sorted(allowed))} (got {value or 'empty'})")
        return value

    push = require_bool("push", "true")
    open_pr = require_bool("open_pr", "true")
    allow_no_local = require_bool("allow_no_local_gates", "false")
    allow_no_smoke = require_bool("allow_no_smoke", "false")
    require_bool("allow_no_ci", "false")
    coderabbit = require_enum("coderabbit", "required", {"required", "optional", "off"})
    del coderabbit
    deploy_mode = require_enum("deploy_mode", "command", {"command", "ci", "not-applicable"})
    merge_method = require_enum("merge_method", "squash", {"squash", "merge", "rebase"})
    del merge_method

    if push != "true":
        errors.append("push must be true for Complete Delivery")
    if open_pr != "true":
        errors.append("open_pr must be true for Complete Delivery")

    required_checks = string_var(profile, "required_checks", "auto")
    if not required_checks:
        errors.append("required_checks must be auto or an exact comma-separated list")
    elif required_checks != "auto":
        checks = [part.strip() for part in required_checks.split(",")]
        if not all(checks) or len(checks) != len(set(checks)):
            errors.append("required_checks must contain unique, nonempty exact check names")

    local_gates = (
        "setup_command", "lint_command", "typecheck_command", "test_command",
        "build_command", "browser_test_command", "security_command", "extra_gate_command",
    )
    if not any(string_var(profile, name, "") for name in local_gates) and allow_no_local != "true":
        errors.append("configure at least one repository gate or explicitly set allow_no_local_gates=true")

    deploy_command = string_var(profile, "deploy_command", "")
    verify_command = string_var(profile, "deploy_verify_command", "")
    smoke_command = string_var(profile, "smoke_command", "")
    if deploy_mode == "command":
        if not deploy_command:
            errors.append("deploy_command is required for deploy_mode=command")
        if not verify_command:
            errors.append("deploy_verify_command is required for deploy_mode=command")
    elif deploy_mode == "ci" and not verify_command:
        errors.append("deploy_verify_command is required for deploy_mode=ci")
    elif deploy_mode == "not-applicable" and not string_var(profile, "deploy_not_applicable_reason", ""):
        errors.append("deploy_not_applicable_reason is required for deploy_mode=not-applicable")
    if deploy_mode != "not-applicable" and not smoke_command and allow_no_smoke != "true":
        errors.append("smoke_command is required unless allow_no_smoke=true")

    production_url = string_var(profile, "production_url", "")
    if production_url and (not production_url.startswith("https://") or any(char.isspace() for char in production_url)):
        errors.append("production_url must be an https URL")
    return errors


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        config = run_gc_config()
        rig = target_rig(config, args.rig)
        rig_path = rig.get("Path")
        if not isinstance(rig_path, str) or not rig_path:
            raise LaunchPreflightError(f"rig {args.rig!r} has no usable path")
        rig_root = Path(rig_path).resolve()
        if not rig_root.is_dir():
            raise LaunchPreflightError(f"rig {args.rig!r} path is not a directory: {rig_root}")
        installed = materialize_assets(args.pack_root.resolve(), rig_root)
        profile = rig.get("FormulaVars")
        if not isinstance(profile, dict):
            profile = {}
        errors = validate_profile(profile)
        if errors:
            joined = "\n  - ".join(errors)
            raise LaunchPreflightError(f"Complete Delivery profile is invalid:\n  - {joined}")
    except LaunchPreflightError as exc:
        print(f"gc complete-delivery delivery start: {exc}", file=sys.stderr)
        return 1
    print(
        f"complete-delivery launch preflight passed: materialized {len(installed)} managed asset(s)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
