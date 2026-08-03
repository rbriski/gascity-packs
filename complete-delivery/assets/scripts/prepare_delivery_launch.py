#!/usr/bin/env python3
"""Validate and prepare a rig before pouring a Complete Delivery workflow.

Formula checks execute from the target repository rather than from an installed
pack.  The launcher therefore validates every launch input first, then
atomically installs the declared runtime assets inside the registered rig root.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit


class LaunchPreflightError(RuntimeError):
    """A launch prerequisite failed before a workflow was poured."""


DELIVERY_CHECKS = (
    "delivery-common.sh",
    "delivery-external-review-deadline.sh",
    "delivery-local-gates.sh",
    "delivery-merged.sh",
    "delivery-preflight.sh",
    "delivery-pr-approved.sh",
    "delivery-pr-open.sh",
    "delivery-release-verified.sh",
    "delivery-report-green.sh",
    "delivery-report-valid.sh",
    "delivery-source-artifact-valid.sh",
)
DELIVERY_SCRIPTS = ("delivery_gate.py", "delivery_report.py")
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
GC_CONFIG_TIMEOUT_SECONDS = 15
MAX_DURATION_SECONDS = Decimal(3600)
DURATION = re.compile(r"([0-9]+(?:\.[0-9]+)?|\.[0-9]+)([smhd]?)")
CI_WORKFLOW = re.compile(r"\.github/workflows/[A-Za-z0-9_./-]+\.ya?ml")
HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rig", required=True, help="target rig name")
    parser.add_argument("--artifact-root", required=True, help="workflow artifact root")
    parser.add_argument(
        "--pack-root",
        type=Path,
        default=Path(os.environ.get("GC_PACK_DIR", "")),
        help="resolved Complete Delivery pack root (defaults to GC_PACK_DIR)",
    )
    return parser.parse_args(argv)


def has_control_characters(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def is_nonblank(value: str) -> bool:
    return bool(value and any(not character.isspace() for character in value))


def gc_config_timeout_seconds() -> float:
    value = os.environ.get(
        "GC_COMPLETE_DELIVERY_CONFIG_TIMEOUT_SECONDS", str(GC_CONFIG_TIMEOUT_SECONDS)
    )
    try:
        timeout = float(value)
    except ValueError as exc:
        raise LaunchPreflightError(
            "GC_COMPLETE_DELIVERY_CONFIG_TIMEOUT_SECONDS must be numeric"
        ) from exc
    if not 0 < timeout <= 60:
        raise LaunchPreflightError(
            "GC_COMPLETE_DELIVERY_CONFIG_TIMEOUT_SECONDS must be greater than zero and at most 60"
        )
    return timeout


def run_gc_config() -> dict[str, Any]:
    timeout_seconds = gc_config_timeout_seconds()
    try:
        result = subprocess.run(
            ["gc", "config", "show", "--json"],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise LaunchPreflightError(
            f"gc config show timed out after {timeout_seconds:g}s"
        ) from exc
    except OSError as exc:
        raise LaunchPreflightError(f"could not execute gc config show: {exc}") from exc
    if result.returncode:
        diagnostic = result.stderr.strip() or result.stdout.strip() or "no diagnostic"
        raise LaunchPreflightError(
            f"gc config show failed with status {result.returncode}: {diagnostic}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise LaunchPreflightError("gc config show returned malformed JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("config"), dict):
        raise LaunchPreflightError("gc config show returned no exact resolved config object")
    return payload["config"]


def target_rig(config: dict[str, Any], name: str) -> dict[str, Any]:
    rigs = config.get("Rigs")
    if not isinstance(rigs, list):
        raise LaunchPreflightError("resolved city configuration has no rig list")
    matches = [
        rig for rig in rigs if isinstance(rig, dict) and rig.get("Name") == name
    ]
    if len(matches) != 1:
        raise LaunchPreflightError(
            f"rig {name!r} must resolve to exactly one registered rig (got {len(matches)})"
        )
    return matches[0]


def registered_rig_root(rig: dict[str, Any], name: str) -> Path:
    value = rig.get("Path")
    if not isinstance(value, str) or not value.strip() or has_control_characters(value):
        raise LaunchPreflightError(f"rig {name!r} has no usable path")
    try:
        root = Path(value).resolve(strict=True)
    except OSError as exc:
        raise LaunchPreflightError(f"rig {name!r} path is unavailable: {exc}") from exc
    if not root.is_dir():
        raise LaunchPreflightError(f"rig {name!r} path is not a directory: {root}")
    return root


def inherited_gascity_root(pack_root: Path) -> Path:
    # complete-delivery -> gstack -> gascity are relative Pack V2 imports.
    candidate = pack_root.parent / "gascity"
    required = candidate / "assets" / "scripts" / "checks" / "build-artifact-valid.sh"
    if not required.is_file():
        raise LaunchPreflightError(
            "the inherited gascity validation assets are unavailable beside the "
            f"Complete Delivery pack ({candidate}); reinstall or repair the pack imports"
        )
    return candidate


def require_contained_destination(rig_root: Path, destination: Path, label: str) -> None:
    """Reject lexical, resolved, and existing-ancestor escapes from ``rig_root``."""

    try:
        relative = destination.relative_to(rig_root)
    except ValueError as exc:
        raise LaunchPreflightError(f"{label} must be beneath the registered rig root") from exc
    if not relative.parts or ".." in relative.parts:
        raise LaunchPreflightError(f"{label} is not a contained managed path")

    current = rig_root
    for component in relative.parts:
        current /= component
        if not (current.exists() or current.is_symlink()):
            continue
        if current.is_symlink():
            raise LaunchPreflightError(f"{label} contains an existing symlink component")
        try:
            resolved = current.resolve(strict=True)
            resolved.relative_to(rig_root)
        except (OSError, ValueError) as exc:
            raise LaunchPreflightError(
                f"{label} has an existing symlink or ancestor outside the registered rig root"
            ) from exc

    try:
        destination.resolve(strict=False).relative_to(rig_root)
    except ValueError as exc:
        raise LaunchPreflightError(f"{label} resolves outside the registered rig root") from exc


def validate_artifact_root(rig_root: Path, raw_value: str) -> Path:
    if not is_nonblank(raw_value) or raw_value != raw_value.strip():
        raise LaunchPreflightError("artifact_root must be a nonblank path without edge whitespace")
    if has_control_characters(raw_value):
        raise LaunchPreflightError("artifact_root must not contain control characters")
    raw = Path(raw_value)
    if ".." in raw.parts or raw.parts in ((), (".",)):
        raise LaunchPreflightError("artifact_root must not be empty, '.', or contain parent traversal")
    destination = raw if raw.is_absolute() else rig_root / raw
    require_contained_destination(rig_root, destination, "artifact_root")
    if destination.exists():
        if not destination.is_dir():
            raise LaunchPreflightError("artifact_root already exists and is not a directory")
        try:
            populated = next(destination.iterdir(), None) is not None
        except OSError as exc:
            raise LaunchPreflightError(f"artifact_root cannot be inspected: {exc}") from exc
        if populated:
            raise LaunchPreflightError(
                "artifact_root is already populated by an earlier launch; choose a new --artifact-root"
            )
    return destination


def string_var(profile: dict[str, Any], name: str, default: str) -> str:
    value = profile.get(name, default)
    return value if isinstance(value, str) else str(value)


def duration_is_bounded(value: str) -> bool:
    match = DURATION.fullmatch(value)
    if not match:
        return False
    try:
        amount = Decimal(match.group(1))
    except InvalidOperation:
        return False
    seconds = amount * {
        "": Decimal(1),
        "s": Decimal(1),
        "m": Decimal(60),
        "h": Decimal(3600),
        "d": Decimal(86400),
    }[match.group(2)]
    return Decimal(0) < seconds <= MAX_DURATION_SECONDS


def https_url_is_valid(value: str) -> bool:
    if (
        value != value.strip()
        or has_control_characters(value)
        or any(character.isspace() for character in value)
    ):
        return False
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return False
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        pass
    if len(hostname) > 253 or hostname.endswith("."):
        return False
    return all(HOST_LABEL.fullmatch(label) for label in hostname.split("."))


def ci_profile_is_valid(workflow: str, environment: str) -> bool:
    parts = Path(workflow).parts
    return bool(
        CI_WORKFLOW.fullmatch(workflow)
        and ".." not in parts
        and "." not in parts
        and all(parts)
        and environment == environment.strip()
        and is_nonblank(environment)
        and len(environment) <= 255
        and not has_control_characters(environment)
    )


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
            choices = ", ".join(sorted(allowed))
            errors.append(f"{name} must be one of: {choices} (got {value or 'empty'})")
        return value

    push = require_bool("push", "true")
    open_pr = require_bool("open_pr", "true")
    allow_no_local = require_bool("allow_no_local_gates", "false")
    allow_no_smoke = require_bool("allow_no_smoke", "false")
    require_bool("allow_no_ci", "false")
    require_enum("coderabbit", "off", {"required", "optional", "off"})
    require_enum("merge_method", "squash", {"squash", "merge", "rebase"})
    deploy_mode = require_enum(
        "deploy_mode", "command", {"command", "ci", "not-applicable"}
    )

    if push != "true":
        errors.append("push must be true for Complete Delivery")
    if open_pr != "true":
        errors.append("open_pr must be true for Complete Delivery")

    required_checks = string_var(profile, "required_checks", "auto")
    if not is_nonblank(required_checks):
        errors.append("required_checks must be auto or an exact comma-separated list")
    elif required_checks != "auto":
        checks = [part.strip() for part in required_checks.split(",")]
        if not all(checks) or len(checks) != len(set(checks)):
            errors.append("required_checks must contain unique, nonempty exact check names")

    local_gates = (
        "setup_command",
        "lint_command",
        "typecheck_command",
        "test_command",
        "build_command",
        "browser_test_command",
        "security_command",
        "extra_gate_command",
    )
    if not any(is_nonblank(string_var(profile, name, "")) for name in local_gates):
        if allow_no_local != "true":
            errors.append(
                "configure at least one repository gate or explicitly set allow_no_local_gates=true"
            )

    deploy_command = string_var(profile, "deploy_command", "")
    workflow = string_var(profile, "deploy_ci_workflow", "")
    environment = string_var(profile, "deploy_environment", "")
    verify_command = string_var(profile, "deploy_verify_command", "")
    smoke_command = string_var(profile, "smoke_command", "")
    no_smoke_reason = string_var(profile, "no_smoke_reason", "")
    na_reason = string_var(profile, "deploy_not_applicable_reason", "")

    if allow_no_smoke == "true" and not is_nonblank(no_smoke_reason):
        errors.append(
            "no_smoke_reason is required and must be nonblank when allow_no_smoke=true"
        )

    if deploy_mode == "command":
        if not is_nonblank(deploy_command):
            errors.append("deploy_command is required for deploy_mode=command")
        if not is_nonblank(verify_command):
            errors.append("deploy_verify_command is required for deploy_mode=command")
    elif deploy_mode == "ci":
        if not ci_profile_is_valid(workflow, environment):
            errors.append(
                "deploy_mode=ci requires a safe .github/workflows/*.yml path and nonblank deploy_environment"
            )
        if not is_nonblank(verify_command):
            errors.append("deploy_verify_command is required for deploy_mode=ci")
    elif deploy_mode == "not-applicable" and not is_nonblank(na_reason):
        errors.append(
            "deploy_not_applicable_reason is required and must be nonblank for deploy_mode=not-applicable"
        )

    if deploy_mode != "not-applicable":
        if deploy_mode == "command" and not duration_is_bounded(
            string_var(profile, "deploy_timeout", "5m")
        ):
            errors.append("deploy_timeout must be a positive finite duration no greater than 1h")
        if not duration_is_bounded(string_var(profile, "deploy_verify_timeout", "5m")):
            errors.append(
                "deploy_verify_timeout must be a positive finite duration no greater than 1h"
            )
        if is_nonblank(smoke_command) and not duration_is_bounded(
            string_var(profile, "smoke_timeout", "5m")
        ):
            errors.append("smoke_timeout must be a positive finite duration no greater than 1h")
        if not is_nonblank(smoke_command) and allow_no_smoke != "true":
            errors.append("smoke_command is required unless allow_no_smoke=true")

    production_url = string_var(profile, "production_url", "")
    if production_url and not https_url_is_valid(production_url):
        errors.append("production_url must be an https URL with a valid host")
    return errors


def run_github_command(arguments: list[str], *, cwd: Path, purpose: str) -> str:
    """Run a launcher-only GitHub prerequisite without exposing credentials."""

    timeout_seconds = gc_config_timeout_seconds()
    try:
        result = subprocess.run(
            ["gh", *arguments],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError as exc:
        raise LaunchPreflightError("gh is required on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise LaunchPreflightError(
            f"{purpose} timed out after {timeout_seconds:g}s"
        ) from exc
    except OSError as exc:
        raise LaunchPreflightError(f"could not execute {purpose}: {exc}") from exc
    if result.returncode:
        diagnostic = result.stderr.strip() or result.stdout.strip() or "no diagnostic"
        raise LaunchPreflightError(
            f"{purpose} failed with status {result.returncode}: {diagnostic}"
        )
    return result.stdout


def verify_github_delivery_prerequisites(rig_root: Path, profile: dict[str, Any]) -> None:
    """Verify credentialed GitHub facts before Formula ConditionEnv is sanitized.

    Formula v2 executes its mechanical checks with a deliberately restricted
    HOME, so the launcher is the only valid boundary for checking the user's
    gh credential store.  The Formula receives a versioned durable attestation
    only after all three live GitHub predicates below have passed.
    """

    timeout_seconds = gc_config_timeout_seconds()
    try:
        repository = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=rig_root,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LaunchPreflightError("registered rig is not a readable git worktree") from exc
    if repository.returncode or repository.stdout.strip() != "true":
        raise LaunchPreflightError("registered rig is not a readable git worktree")

    run_github_command(["auth", "status"], cwd=rig_root, purpose="gh authentication check")
    repo_json = run_github_command(
        ["repo", "view", "--json", "nameWithOwner"],
        cwd=rig_root,
        purpose="GitHub repository resolution",
    )
    try:
        payload = json.loads(repo_json)
    except json.JSONDecodeError as exc:
        raise LaunchPreflightError("GitHub repository resolution returned malformed JSON") from exc
    name_with_owner = payload.get("nameWithOwner") if isinstance(payload, dict) else None
    if not isinstance(name_with_owner, str) or not name_with_owner.strip():
        raise LaunchPreflightError("GitHub repository resolution returned no nameWithOwner")

    base_branch = string_var(profile, "base_branch", "main")
    if not is_nonblank(base_branch):
        raise LaunchPreflightError("base_branch must be nonblank")
    run_github_command(
        [
            "api",
            f"repos/{{owner}}/{{repo}}/branches/{quote(base_branch, safe='')}/protection",
            "--silent",
        ],
        cwd=rig_root,
        purpose=f"protected base branch check for {base_branch}",
    )


def materialization_plan(pack_root: Path, rig_root: Path) -> list[tuple[Path, Path, str]]:
    gascity_root = inherited_gascity_root(pack_root)
    plan: list[tuple[Path, Path, str]] = []

    def add(source: Path, relative: str) -> None:
        destination = rig_root / relative
        if not source.is_file():
            raise LaunchPreflightError(f"required managed asset is missing from the pack: {source}")
        require_contained_destination(rig_root, destination, relative)
        plan.append((source, destination, relative))

    for name in DELIVERY_CHECKS:
        add(pack_root / "assets" / "scripts" / "checks" / name, f".gc/scripts/checks/{name}")
    for name in DELIVERY_SCRIPTS:
        add(pack_root / "assets" / "scripts" / name, f".gc/scripts/{name}")
    for name in GASCITY_CHECKS:
        add(gascity_root / "assets" / "scripts" / "checks" / name, f".gc/scripts/checks/{name}")
    for name in GASCITY_SCRIPTS:
        add(gascity_root / "assets" / "scripts" / name, f".gc/scripts/{name}")
    for name in GASCITY_SCHEMAS:
        add(gascity_root / "schemas" / "build" / name, f"schemas/build/{name}")
    return plan


def atomic_copy(source: Path, destination: Path, rig_root: Path, label: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    require_contained_destination(rig_root, destination, label)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_handle:
            shutil.copyfileobj(input_handle, output)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(stat.S_IMODE(source.stat().st_mode))
        require_contained_destination(rig_root, destination, label)
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def atomic_write_text(destination: Path, content: str, rig_root: Path, label: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    require_contained_destination(rig_root, destination, label)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        require_contained_destination(rig_root, destination, label)
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def materialize_assets(pack_root: Path, rig_root: Path) -> list[str]:
    plan = materialization_plan(pack_root, rig_root)
    manifest_path = rig_root / ".gc" / MANIFEST_NAME
    require_contained_destination(rig_root, manifest_path, MANIFEST_NAME)
    installed = [relative for _, _, relative in plan]
    for source, destination, relative in plan:
        atomic_copy(source, destination, rig_root, relative)
    manifest = {
        "assets": installed,
        "inherited_from": "gascity",
        "owner": "complete-delivery",
        "version": 1,
    }
    atomic_write_text(
        manifest_path,
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        rig_root,
        MANIFEST_NAME,
    )
    return installed


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        try:
            pack_root = args.pack_root.resolve(strict=True)
        except OSError as exc:
            raise LaunchPreflightError(f"GC_PACK_DIR is unavailable: {exc}") from exc
        if not pack_root.is_dir():
            raise LaunchPreflightError("GC_PACK_DIR must point to the Complete Delivery pack")

        config = run_gc_config()
        rig = target_rig(config, args.rig)
        rig_root = registered_rig_root(rig, args.rig)
        profile = rig.get("FormulaVars")
        if not isinstance(profile, dict):
            profile = {}
        errors = validate_profile(profile)
        if errors:
            joined = "\n  - ".join(errors)
            raise LaunchPreflightError(f"Complete Delivery profile is invalid:\n  - {joined}")

        # This must run before Formula v2 creates a root whose future
        # ConditionEnv deliberately cannot read the launcher's gh config.
        verify_github_delivery_prerequisites(rig_root, profile)

        # Every validation above, including the collision check and the complete
        # source/destination plan, runs before the first target-rig mutation.
        validate_artifact_root(rig_root, args.artifact_root)
        materialization_plan(pack_root, rig_root)
        installed = materialize_assets(pack_root, rig_root)
    except LaunchPreflightError as exc:
        print(f"gc complete-delivery delivery start: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"gc complete-delivery delivery start: managed asset write failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"complete-delivery launch preflight passed: materialized {len(installed)} managed asset(s)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
