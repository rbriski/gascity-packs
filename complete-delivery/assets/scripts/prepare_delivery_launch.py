#!/usr/bin/env python3
"""Validate and prepare a rig before pouring a Complete Delivery workflow.

Formula checks execute from the target repository rather than from an installed
pack.  The launcher therefore validates every launch input first, then
atomically installs the declared runtime assets inside the registered rig root.
"""

from __future__ import annotations

import argparse
import hashlib
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
MANIFEST_VERSION = 2
TRANSACTION_NAME = "complete-delivery-transaction.json"
TRANSACTION_VERSION = 1
# Version 1 did not record asset digests.  These are the SHA-256 digests of
# the complete-delivery 0.1.0 release inventory, rather than of whichever
# sources happen to be installed by the launcher that is performing an
# upgrade.  They make the one supported legacy migration fail closed.
LEGACY_V1_ASSET_HASHES = {
    ".gc/scripts/checks/build-artifact-valid.sh": "sha256:8178312263aab109472ddb6b5fe2e93d835cd6c9b470e7427f22a670f0e13e94",
    ".gc/scripts/checks/delivery-common.sh": "sha256:73e91e14c96eb75e37f9232db830a4849290298ba3b257ed1112ca9eaeda346a",
    ".gc/scripts/checks/delivery-external-review-deadline.sh": "sha256:0935b7c1ea7e1fee35539f050a13698180a9df129c76cbccd96e5e610fad91b3",
    ".gc/scripts/checks/delivery-local-gates.sh": "sha256:a76787b5ea0c1f1cc29a7234db63f70e068dfe5578df0841eef24c39d68232c3",
    ".gc/scripts/checks/delivery-merged.sh": "sha256:218740575c743ca7050d27c00446aa854dbb21b9daeded36423e5547bd707185",
    ".gc/scripts/checks/delivery-pr-approved.sh": "sha256:dbf9db95c2ce8cc79267e724557e264690b2ecfcdd0453bbdf68baf3bcb2c66d",
    ".gc/scripts/checks/delivery-pr-open.sh": "sha256:8a1bac06babd14cf84ebee1c537bb117a80a35fd502140ea9369f372d904e363",
    ".gc/scripts/checks/delivery-preflight.sh": "sha256:55c48cfd39610cb2e93627635d5c65179a5d24e660b6ff2b77b35c0182db3400",
    ".gc/scripts/checks/delivery-release-verified.sh": "sha256:6f0571b2e689f11c7fef21d474b3c46946544f7847fc0bd7cf37f155edb0076b",
    ".gc/scripts/checks/delivery-report-green.sh": "sha256:b7aefacf51f873437f6ec699a438b8aed940c59720f32b7026a9e46872717f64",
    ".gc/scripts/checks/delivery-report-valid.sh": "sha256:1e85ca850808a454f9d5bc0c8560d243fcb07d4f0099354dcc80325811387291",
    ".gc/scripts/checks/delivery-source-artifact-valid.sh": "sha256:3a6f17483424e7b7a592e61cbec7aa4382b95c4795d614e82c1e5d4a535d6299",
    ".gc/scripts/delivery_gate.py": "sha256:d9b335eb280d5a926c2b6182a8d46ab4fbd7e490d038c8e18937e7b118081b5a",
    ".gc/scripts/delivery_report.py": "sha256:ea4a2da346e9aae8ec53949f9f263da0109d555c40533c80b54b88fd9a8eb1a6",
    ".gc/scripts/validate_build_artifact.py": "sha256:083fa0706605ed9e4247bc9ff2755706dde9e4e500650a0775caab55690ad4c2",
    "schemas/build/decomposition.v1.yaml": "sha256:ef10d410d17e06c869afe3b08bcf49f9276cdee7ba3f250e063d742bf131415d",
    "schemas/build/final-report.v1.yaml": "sha256:6ebc3e474032a5e21450b911f550ff7aa6cee882b35b63ce177125a1636bf6bf",
    "schemas/build/implementation-summary.v1.yaml": "sha256:ab3c877a9555854a462116c2d3551b12fe3794eb8f29772de058a4cf5efa2197",
    "schemas/build/plan.v1.yaml": "sha256:91b4c39a92c73bd9ea2c853793087870aa19872bd6e0d75be5aa2ba65e807327",
    "schemas/build/requirements.v1.yaml": "sha256:09310e8466377a22c153a6cbf93287eec699a11092c2bc26f96bd6440aac01c5",
    "schemas/build/review.v1.yaml": "sha256:4e9773e44a271204b743d429d0751e3c71a32bb66597c47da0c9c34edc5ac454",
}
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
            f"repos/{name_with_owner}/branches/{quote(base_branch, safe='')}/protection",
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
        # Schemas are runtime assets, not source files owned by the target rig.
        # Keep them under the rig's ignored .gc directory beside the installed
        # validator so a delivery launch never writes to schemas/build.
        add(gascity_root / "schemas" / "build" / name, f".gc/schemas/build/{name}")
    return plan


def asset_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def manifest_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise LaunchPreflightError("managed asset manifest has an invalid asset path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or path.parts in ((), (".",)):
        raise LaunchPreflightError("managed asset manifest has an unsafe asset path")
    return value


def load_prior_manifest(rig_root: Path) -> dict[str, Any] | None:
    """Return the prior Complete Delivery inventory, or fail closed.

    The manifest is the ownership boundary: an existing file is mutable only
    when a previous launcher recorded it and its recorded bytes are intact.
    """

    manifest_path = rig_root / ".gc" / MANIFEST_NAME
    require_contained_destination(rig_root, manifest_path, MANIFEST_NAME)
    if not (manifest_path.exists() or manifest_path.is_symlink()):
        return None
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise LaunchPreflightError("managed asset manifest is not a regular file")
    if git_tracked(rig_root, manifest_path):
        raise LaunchPreflightError("managed asset manifest is tracked by the target rig")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LaunchPreflightError("managed asset manifest is invalid") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("owner") != "complete-delivery"
        or payload.get("version") not in (1, MANIFEST_VERSION)
        or not isinstance(payload.get("assets"), list)
    ):
        raise LaunchPreflightError("managed asset manifest is not a valid Complete Delivery inventory")
    assets = [manifest_relative_path(value) for value in payload["assets"]]
    if len(assets) != len(set(assets)):
        raise LaunchPreflightError("managed asset manifest contains duplicate asset paths")
    hashes = payload.get("asset_hashes", {})
    hashes_are_valid = (
        isinstance(hashes, dict)
        and set(hashes) == set(assets)
        and all(
            isinstance(value, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value)
            for value in hashes.values()
        )
    )
    if payload["version"] == MANIFEST_VERSION and not hashes_are_valid:
        raise LaunchPreflightError("managed asset manifest has invalid asset hashes")
    if payload["version"] == 1:
        try:
            legacy_hashes = {asset: LEGACY_V1_ASSET_HASHES[asset] for asset in assets}
        except KeyError as exc:
            raise LaunchPreflightError(
                "managed asset manifest contains an unknown Complete Delivery 0.1.0 asset"
            ) from exc
        # A v1 manifest normally has no hashes.  If an operator supplied them,
        # they must attest exactly to the known release bytes, not merely be
        # well-formed attacker-controlled digest strings.
        if hashes and (not hashes_are_valid or hashes != legacy_hashes):
            raise LaunchPreflightError("managed asset manifest has invalid legacy asset hashes")
        hashes = legacy_hashes
    return {"assets": set(assets), "asset_hashes": hashes}


def git_tracked(rig_root: Path, destination: Path) -> bool:
    relative = destination.relative_to(rig_root)
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", str(relative)],
        cwd=rig_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise LaunchPreflightError(f"could not determine whether {relative} is tracked by the target rig")


def ensure_regular_untracked_destination(rig_root: Path, destination: Path, label: str) -> None:
    require_contained_destination(rig_root, destination, label)
    relative = destination.relative_to(rig_root)
    parent = rig_root
    for component in relative.parts[:-1]:
        parent /= component
        if parent.exists() or parent.is_symlink():
            if parent.is_symlink() or not parent.is_dir():
                raise LaunchPreflightError(f"{label} has a non-directory ancestor")
    if not (destination.exists() or destination.is_symlink()):
        return
    if destination.is_symlink() or not destination.is_file():
        raise LaunchPreflightError(f"{label} is not a regular file")
    if git_tracked(rig_root, destination):
        raise LaunchPreflightError(f"{label} is tracked by the target rig")


def source_for_prior_asset(
    relative: str, planned_sources: dict[str, Path], gascity_root: Path
) -> Path | None:
    """Resolve legacy v1 root schemas only when their source is unambiguous."""

    if relative in planned_sources:
        return planned_sources[relative]
    prefix = "schemas/build/"
    if relative.startswith(prefix):
        candidate = gascity_root / "schemas" / "build" / relative.removeprefix(prefix)
        return candidate if candidate.is_file() else None
    return None


def preflight_materialization(
    pack_root: Path, rig_root: Path, plan: list[tuple[Path, Path, str]]
) -> tuple[dict[str, str], list[Path]]:
    """Validate every write and cleanup before changing the target rig."""

    prior = load_prior_manifest(rig_root)
    prior_assets = prior["assets"] if prior else set()
    prior_hashes = prior["asset_hashes"] if prior else {}
    planned_sources = {relative: source for source, _, relative in plan}
    gascity_root = inherited_gascity_root(pack_root)
    desired_hashes = {relative: asset_digest(source) for source, _, relative in plan}

    for source, destination, relative in plan:
        ensure_regular_untracked_destination(rig_root, destination, relative)
        if not destination.exists():
            continue
        if relative not in prior_assets:
            raise LaunchPreflightError(f"{relative} already exists but is not owned by Complete Delivery")
        expected = prior_hashes.get(relative, desired_hashes[relative])
        if asset_digest(destination) != expected:
            raise LaunchPreflightError(f"{relative} was modified after Complete Delivery installed it")

    stale_paths: list[Path] = []
    for relative in sorted(prior_assets - set(planned_sources)):
        destination = rig_root / relative
        ensure_regular_untracked_destination(rig_root, destination, relative)
        # Record the complete authenticated stale inventory, including assets
        # already absent when this attempt starts.  A retry must be able to
        # distinguish a previously removed managed asset from a journal that
        # was edited to omit a cleanup target.
        stale_paths.append(destination)
        if not destination.exists():
            continue
        expected = prior_hashes.get(relative)
        if expected is None:
            source = source_for_prior_asset(relative, planned_sources, gascity_root)
            if source is None:
                raise LaunchPreflightError(
                    f"cannot safely clean stale managed asset {relative} without a recorded digest"
                )
            expected = asset_digest(source)
        if asset_digest(destination) != expected:
            raise LaunchPreflightError(f"stale managed asset {relative} was modified after installation")
    return desired_hashes, stale_paths


def serialized_manifest(installed: list[str], desired_hashes: dict[str, str]) -> str:
    return json.dumps(
        {
            "asset_hashes": desired_hashes,
            "assets": installed,
            "inherited_from": "gascity",
            "owner": "complete-delivery",
            "version": MANIFEST_VERSION,
        },
        indent=2,
        sort_keys=True,
    ) + "\n"


def path_digest(path: Path) -> str | None:
    if not path.exists():
        return None
    return asset_digest(path)


def transaction_path(rig_root: Path) -> Path:
    path = rig_root / ".gc" / TRANSACTION_NAME
    require_contained_destination(rig_root, path, TRANSACTION_NAME)
    return path


def transaction_states(
    plan: list[tuple[Path, Path, str]], stale_paths: list[Path], manifest_path: Path
) -> dict[str, str | None]:
    states = {relative: path_digest(destination) for _, destination, relative in plan}
    states.update({str(path): path_digest(path) for path in stale_paths})
    states[MANIFEST_NAME] = path_digest(manifest_path)
    return states


def load_transaction(rig_root: Path) -> dict[str, Any] | None:
    path = transaction_path(rig_root)
    if not (path.exists() or path.is_symlink()):
        return None
    ensure_regular_untracked_destination(rig_root, path, TRANSACTION_NAME)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LaunchPreflightError("managed asset transaction journal is invalid") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("owner") != "complete-delivery"
        or payload.get("version") != TRANSACTION_VERSION
        or not isinstance(payload.get("desired_hashes"), dict)
        or not isinstance(payload.get("prior_states"), dict)
        or not isinstance(payload.get("stale_paths"), list)
    ):
        raise LaunchPreflightError("managed asset transaction journal is invalid")
    return payload


def apply_materialization(
    plan: list[tuple[Path, Path, str]],
    stale_paths: list[Path],
    manifest_path: Path,
    manifest_content: str,
    rig_root: Path,
) -> None:
    for source, destination, relative in plan:
        atomic_copy(source, destination, rig_root, relative)
    for stale_path in stale_paths:
        stale_path.unlink(missing_ok=True)
    atomic_write_text(manifest_path, manifest_content, rig_root, MANIFEST_NAME)


def verify_materialization_postcondition(
    plan: list[tuple[Path, Path, str]],
    stale_paths: list[Path],
    manifest_path: Path,
    desired_hashes: dict[str, str],
    expected_manifest: str,
) -> None:
    """Require the complete managed inventory before deleting its journal."""

    expected_states = {relative: digest for relative, digest in desired_hashes.items()}
    expected_states.update({str(path): None for path in stale_paths})
    expected_states[MANIFEST_NAME] = expected_manifest
    if transaction_states(plan, stale_paths, manifest_path) != expected_states:
        raise LaunchPreflightError("completed managed asset transaction has unexpected file contents")


def recover_interrupted_materialization(
    pack_root: Path, rig_root: Path, plan: list[tuple[Path, Path, str]]
) -> None:
    """Finish only a verified interrupted materialization transaction.

    The journal is written before the first target-rig mutation.  Recovery
    accepts each path only when it has the authorized prior digest or the
    current source digest; a third value is treated as a user modification.
    """

    transaction = load_transaction(rig_root)
    if transaction is None:
        return
    desired_hashes = {relative: asset_digest(source) for source, _, relative in plan}
    if transaction["desired_hashes"] != desired_hashes:
        raise LaunchPreflightError("managed asset transaction does not match this Complete Delivery release")
    manifest_path = rig_root / ".gc" / MANIFEST_NAME
    installed = [relative for _, _, relative in plan]
    manifest_content = serialized_manifest(installed, desired_hashes)
    expected_new_manifest = f"sha256:{hashlib.sha256(manifest_content.encode()).hexdigest()}"
    stale_relatives = transaction["stale_paths"]
    if any(
        not isinstance(relative, str)
        or not relative
        or Path(relative).is_absolute()
        or ".." in Path(relative).parts
        for relative in stale_relatives
    ):
        raise LaunchPreflightError("managed asset transaction journal has unsafe stale paths")
    stale_paths = [rig_root / relative for relative in stale_relatives]
    for _, destination, relative in plan:
        ensure_regular_untracked_destination(rig_root, destination, relative)
    for stale_path in stale_paths:
        ensure_regular_untracked_destination(rig_root, stale_path, str(stale_path.relative_to(rig_root)))
    ensure_regular_untracked_destination(rig_root, manifest_path, MANIFEST_NAME)
    current_manifest = path_digest(manifest_path)

    if current_manifest == expected_new_manifest:
        verify_materialization_postcondition(
            plan, stale_paths, manifest_path, desired_hashes, expected_new_manifest
        )
        transaction_path(rig_root).unlink()
        return

    prior = load_prior_manifest(rig_root)
    prior_hashes = prior["asset_hashes"] if prior else {}
    prior_assets = prior["assets"] if prior else set()
    expected_stale_relatives = sorted(prior_assets - set(desired_hashes))
    if stale_relatives != expected_stale_relatives:
        raise LaunchPreflightError(
            "managed asset transaction stale paths do not match the authenticated prior inventory"
        )
    authorized_prior_states = {
        relative: prior_hashes.get(relative) if relative in prior_assets else None
        for _, _, relative in plan
    }
    authorized_prior_states.update(
        {str(path): prior_hashes.get(str(path.relative_to(rig_root))) for path in stale_paths}
    )
    authorized_prior_states[MANIFEST_NAME] = current_manifest
    transaction_prior_states = transaction["prior_states"]
    if set(transaction_prior_states) != set(authorized_prior_states):
        raise LaunchPreflightError("managed asset transaction does not match the authenticated prior inventory")

    # A stale managed asset may already have been absent when the journal was
    # written, or may have been unlinked before an interrupted retry.  Both
    # states are safe, but a journal may never authorize any third digest.
    for stale_path in stale_paths:
        key = str(stale_path)
        if transaction_prior_states[key] not in (authorized_prior_states[key], None):
            raise LaunchPreflightError(
                "managed asset transaction does not match the authenticated prior inventory"
            )
    for _, _, relative in plan:
        if transaction_prior_states[relative] != authorized_prior_states[relative]:
            raise LaunchPreflightError("managed asset transaction does not match the authenticated prior inventory")
    if transaction_prior_states[MANIFEST_NAME] != authorized_prior_states[MANIFEST_NAME]:
        raise LaunchPreflightError("managed asset transaction does not match the authenticated prior inventory")

    current_states = transaction_states(plan, stale_paths, manifest_path)
    for _, destination, relative in plan:
        if current_states[relative] not in (authorized_prior_states[relative], desired_hashes[relative]):
            raise LaunchPreflightError(f"interrupted managed asset transaction found modified asset {relative}")
    for stale_path in stale_paths:
        key = str(stale_path)
        if current_states[key] not in (transaction_prior_states[key], None):
            raise LaunchPreflightError(f"interrupted managed asset transaction found modified stale asset {key}")
    if current_states[MANIFEST_NAME] != authorized_prior_states[MANIFEST_NAME]:
        raise LaunchPreflightError("interrupted managed asset transaction found a modified manifest")
    apply_materialization(plan, stale_paths, manifest_path, manifest_content, rig_root)
    verify_materialization_postcondition(
        plan, stale_paths, manifest_path, desired_hashes, expected_new_manifest
    )
    transaction_path(rig_root).unlink()


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
    recover_interrupted_materialization(pack_root, rig_root, plan)
    desired_hashes, stale_paths = preflight_materialization(pack_root, rig_root, plan)
    installed = [relative for _, _, relative in plan]
    manifest_content = serialized_manifest(installed, desired_hashes)
    journal = {
        "desired_hashes": desired_hashes,
        "owner": "complete-delivery",
        "prior_states": transaction_states(plan, stale_paths, manifest_path),
        "stale_paths": [str(path.relative_to(rig_root)) for path in stale_paths],
        "version": TRANSACTION_VERSION,
    }
    atomic_write_text(
        transaction_path(rig_root),
        json.dumps(journal, indent=2, sort_keys=True) + "\n",
        rig_root,
        TRANSACTION_NAME,
    )
    apply_materialization(plan, stale_paths, manifest_path, manifest_content, rig_root)
    expected_manifest = f"sha256:{hashlib.sha256(manifest_content.encode()).hexdigest()}"
    verify_materialization_postcondition(
        plan, stale_paths, manifest_path, desired_hashes, expected_manifest
    )
    transaction_path(rig_root).unlink()
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
