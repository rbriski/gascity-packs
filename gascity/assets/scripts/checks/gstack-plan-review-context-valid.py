#!/usr/bin/env python3
"""Fail-closed bindings and output checks for gstack plan-review Ralph loops.

Every invocation resolves inputs from the durable graph root.  In particular,
it deliberately ignores copied `gc.var.*` values on retry/lane beads: those
values were the source of a real cross-plan review incident.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


LANES = {
    "founder": ("plan-review.founder-scope-review", "founder"),
    "design": ("plan-review.design-plan-review", "design"),
    "engineering": ("plan-review.engineering-plan-review", "engineering"),
    "devex": ("plan-review.devex-plan-review", "devex"),
}
SYNTHESIS_STEP = "plan-review.synthesize-plan-review"
APPLY_STEP = "plan-review.apply-plan-review-findings"


class ContractError(RuntimeError):
    pass


def fail(message: str) -> None:
    raise ContractError(f"gstack-plan-review-contract: {message}")


def gc_json(*args: str) -> Any:
    result = subprocess.run(["gc", *args], text=True, capture_output=True)
    if result.returncode:
        fail(f"gc {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        fail(f"gc {' '.join(args)} returned invalid JSON: {exc}")


def bead(bead_id: str) -> dict[str, Any]:
    value = gc_json("bd", "show", bead_id, "--json")
    if isinstance(value, list):
        value = value[0] if value else None
    if not isinstance(value, dict):
        fail(f"bead {bead_id} is missing")
    return value


def metadata(value: dict[str, Any]) -> dict[str, str]:
    raw = value.get("metadata") or {}
    return {str(key): str(item) for key, item in raw.items() if item is not None}


def current_bead_id() -> str:
    bead_id = os.environ.get("GC_BEAD_ID", "")
    if not bead_id:
        fail("GC_BEAD_ID is required")
    return bead_id


def contained_file(value: str, artifact_root: Path, label: str) -> Path:
    if not value:
        fail(f"durable root metadata {label} is missing")
    path = Path(value)
    if not path.is_absolute():
        fail(f"durable root metadata {label} must be an absolute path")
    try:
        resolved_root = artifact_root.resolve(strict=True)
        resolved = path.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError):
        fail(f"{label} must exist beneath gc.var.artifact_root without escaping it")
    if not resolved.is_file():
        fail(f"{label} must be a file")
    return resolved


def contained_path(path: Path, artifact_root: Path, label: str, *, exists: bool = True) -> Path:
    try:
        root = artifact_root.resolve(strict=True)
        lexical = path.absolute()
        lexical.relative_to(root)
        resolved = path.resolve(strict=exists)
        resolved.relative_to(root)
    except (OSError, ValueError):
        fail(f"{label} must resolve beneath gc.var.artifact_root without symlinks")
    return resolved


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def base_binding(bead_id: str) -> dict[str, Any]:
    lane = bead(bead_id)
    lane_meta = metadata(lane)
    root_id = lane_meta.get("gc.root_bead_id", "")
    if not root_id:
        fail(f"gc.root_bead_id is missing on {bead_id}")
    root_meta = metadata(bead(root_id))
    artifact_value = root_meta.get("gc.var.artifact_root", "")
    if not artifact_value:
        fail(f"gc.var.artifact_root is missing on durable root {root_id}")
    artifact_root = Path(artifact_value)
    if not artifact_root.is_absolute():
        work_dir = root_meta.get("gc.work_dir", "")
        if not work_dir:
            fail("relative gc.var.artifact_root requires gc.work_dir on the durable root")
        artifact_root = Path(work_dir) / artifact_root
    try:
        artifact_root = artifact_root.resolve(strict=True)
    except OSError:
        fail(f"gc.var.artifact_root on durable root {root_id} does not exist")
    if not artifact_root.is_dir():
        fail("gc.var.artifact_root must be a directory")
    source = root_meta.get("gc.var.source_bead_id", "")
    if not source:
        fail(f"gc.var.source_bead_id is missing on durable root {root_id}")
    plan = contained_file(
        root_meta.get("gc.build.plan_path") or root_meta.get("gc.var.plan_path", ""),
        artifact_root,
        "gc.build.plan_path",
    )
    context = contained_file(
        root_meta.get("gc.build.plan_review_context_path", ""),
        artifact_root,
        "gc.build.plan_review_context_path",
    )
    return {
        "bead_id": bead_id,
        "bead": lane,
        "meta": lane_meta,
        "root_id": root_id,
        "root_meta": root_meta,
        "artifact_root": artifact_root,
        "source": source,
        "plan": plan,
        "context": context,
    }


def binding(bead_id: str) -> dict[str, Any]:
    result = base_binding(bead_id)
    lane_meta = result["meta"]
    attempt = lane_meta.get("gc.attempt", "")
    scope = lane_meta.get("gc.scope_ref", "")
    if not attempt.isdecimal() or int(attempt) < 1:
        fail(f"gc.attempt on {bead_id} must be a positive decimal")
    if not scope or "plan-review.gstack-plan-review-loop.iteration." not in scope:
        fail(f"gc.scope_ref on {bead_id} is not a gstack plan-review loop scope")
    return {**result, "attempt": attempt, "scope": scope}


def attempt_dir(contract: dict[str, Any]) -> Path:
    return contract["artifact_root"] / "plan-review" / contract["root_id"] / f"attempt-{contract['attempt']}"


def context_path(contract: dict[str, Any]) -> Path:
    return attempt_dir(contract) / "context.json"


def root_binding_path(contract: dict[str, Any]) -> Path:
    return contract["artifact_root"] / "plan-review" / contract["root_id"] / "binding.json"


def expected_output(contract: dict[str, Any], name: str) -> Path:
    return attempt_dir(contract) / f"{name}.md"


def root_payload(contract: dict[str, Any]) -> dict[str, str]:
    return {
        "root_bead_id": contract["root_id"],
        "source_bead_id": contract["source"],
        "artifact_root": str(contract["artifact_root"]),
        "plan_path": str(contract["plan"]),
        "plan_sha256": digest(contract["plan"]),
        "review_context_path": str(contract["context"]),
        "review_context_sha256": digest(contract["context"]),
    }


def context_payload(contract: dict[str, Any]) -> dict[str, str]:
    return {
        **root_payload(contract),
        "attempt": contract["attempt"],
        "scope_ref": contract["scope"],
    }


def validate_root_binding(contract: dict[str, Any]) -> None:
    candidate = root_binding_path(contract)
    if not candidate.is_file():
        fail("durable plan-review binding is missing; setup must run before the Ralph loop")
    path = contained_path(candidate, contract["artifact_root"], "durable plan-review binding")
    try:
        actual = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"durable plan-review binding is invalid: {exc}")
    if actual != root_payload(contract):
        fail("durable plan-review binding does not match the durable root")


def validate_context(contract: dict[str, Any]) -> Path:
    validate_root_binding(contract)
    candidate = context_path(contract)
    if not candidate.is_file():
        fail("attempt-local plan-review context is missing; setup must run before lanes")
    path = contained_path(candidate, contract["artifact_root"], "plan-review context")
    try:
        actual = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"attempt-local plan-review context is invalid: {exc}")
    if actual != context_payload(contract):
        fail("attempt-local plan-review context does not match the durable root binding")
    return path


def header(contract: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"root_bead_id: {contract['root_id']}",
            f"source_bead_id: {contract['source']}",
            f"attempt: {contract['attempt']}",
            f"scope_ref: {contract['scope']}",
            f"context_path: {context_path(contract)}",
        ]
    )


def validate_output(contract: dict[str, Any], item: dict[str, Any], name: str, path_key: str) -> Path:
    item_meta = metadata(item)
    declared = item_meta.get(path_key, "")
    expected = expected_output(contract, name)
    if declared != str(expected):
        fail(f"{name} output must be exactly {expected}; got {declared or 'nothing'}")
    actual = contained_path(expected, contract["artifact_root"], f"{name} output")
    if not actual.is_file():
        fail(f"{name} output is missing")
    content = actual.read_text(encoding="utf-8")
    for line in header(contract).splitlines():
        if line not in content:
            fail(f"{name} output is not bound to this root/source/attempt/context")
    return actual


def matching_items(contract: dict[str, Any]) -> list[dict[str, Any]]:
    values = gc_json("bd", "list", "--all", "--metadata-field", f"gc.root_bead_id={contract['root_id']}", "--json", "--limit=0")
    if not isinstance(values, list):
        fail("gc bd list returned non-list JSON")
    return [
        value
        for value in values
        if isinstance(value, dict)
        and metadata(value).get("gc.root_bead_id") == contract["root_id"]
        and metadata(value).get("gc.attempt") == contract["attempt"]
        and metadata(value).get("gc.scope_ref") == contract["scope"]
    ]


def item_for(items: list[dict[str, Any]], step_id: str) -> dict[str, Any]:
    matches = [value for value in items if metadata(value).get("gc.step_id") == step_id]
    if len(matches) != 1:
        fail(f"expected exactly one current-attempt {step_id} bead, found {len(matches)}")
    return matches[0]


def validate_lanes(contract: dict[str, Any]) -> None:
    validate_context(contract)
    items = matching_items(contract)
    for lane, (step_id, output_name) in LANES.items():
        item = item_for(items, step_id)
        verdict = metadata(item).get(f"gstack.plan_review.{lane}_verdict", "")
        if verdict not in {"approve", "iterate"}:
            fail(f"{lane} lane has invalid or missing verdict")
        validate_output(contract, item, output_name, "gstack.plan_review.output_path")


def prepare() -> None:
    contract = base_binding(current_bead_id())
    directory = contained_path(root_binding_path(contract).parent, contract["artifact_root"], "plan-review binding directory", exists=False)
    directory.mkdir(parents=True, exist_ok=True)
    path = root_binding_path(contract)
    path.write_text(json.dumps(root_payload(contract), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for bead_id in (contract["root_id"], contract["bead_id"]):
        result = subprocess.run(
            ["gc", "bd", "update", bead_id, "--set-metadata", f"gstack.plan_review.context_path={path}"],
            text=True,
            capture_output=True,
        )
        if result.returncode:
            fail(f"could not record durable plan-review context on {bead_id}: {result.stderr.strip()}")
    print(path)


def lane_inputs(name: str) -> None:
    if name not in LANES:
        fail(f"unknown lane {name}")
    contract = binding(current_bead_id())
    step_id, _ = LANES[name]
    if contract["meta"].get("gc.step_id") != step_id:
        fail(f"--lane-inputs {name} was invoked outside {step_id}")
    validate_root_binding(contract)
    directory = contained_path(attempt_dir(contract), contract["artifact_root"], "attempt directory", exists=False)
    directory.mkdir(parents=True, exist_ok=True)
    path = context_path(contract)
    path.write_text(json.dumps(context_payload(contract), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(path)


def validate_lane(name: str) -> None:
    if name not in LANES:
        fail(f"unknown lane {name}")
    contract = binding(current_bead_id())
    step_id, output_name = LANES[name]
    if contract["meta"].get("gc.step_id") != step_id:
        fail(f"--lane {name} was invoked outside {step_id}")
    verdict = contract["meta"].get(f"gstack.plan_review.{name}_verdict", "")
    if verdict not in {"approve", "iterate"}:
        fail(f"{name} verdict must be approve or iterate")
    validate_context(contract)
    validate_output(contract, contract["bead"], output_name, "gstack.plan_review.output_path")
    print(f"validated {name} lane output")


def validate_synthesis_inputs() -> dict[str, Any]:
    contract = binding(current_bead_id())
    if contract["meta"].get("gc.step_id") != SYNTHESIS_STEP:
        fail("--synthesis-inputs was invoked outside the synthesis lane")
    validate_lanes(contract)
    return contract


def validate_synthesis() -> None:
    contract = validate_synthesis_inputs()
    validate_output(contract, contract["bead"], "synthesis", "gstack.plan_review.synthesis_path")
    print("validated plan-review synthesis")


def validate_apply_inputs() -> dict[str, Any]:
    contract = binding(current_bead_id())
    if contract["meta"].get("gc.step_id") != APPLY_STEP:
        fail("--apply-inputs was invoked outside the apply lane")
    validate_lanes(contract)
    items = matching_items(contract)
    synthesis = item_for(items, SYNTHESIS_STEP)
    validate_output(contract, synthesis, "synthesis", "gstack.plan_review.synthesis_path")
    return contract


def validate_apply() -> None:
    contract = validate_apply_inputs()
    verdict = contract["meta"].get("design_review.verdict", "")
    if verdict not in {"done", "iterate"}:
        fail("design_review.verdict must be done or iterate")
    validate_output(contract, contract["bead"], "remediation", "design_review.report_path")
    print("validated plan-review apply output")


def validate_loop() -> None:
    contract = binding(current_bead_id())
    validate_lanes(contract)
    items = matching_items(contract)
    synthesis = item_for(items, SYNTHESIS_STEP)
    validate_output(contract, synthesis, "synthesis", "gstack.plan_review.synthesis_path")
    apply = item_for(items, APPLY_STEP)
    validate_output(contract, apply, "remediation", "design_review.report_path")
    verdict = metadata(apply).get("design_review.verdict", "")
    if verdict != "done":
        fail("plan review needs another pass")
    print("gstack plan review approved with root-bound current-attempt outputs")


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--prepare", action="store_true")
    group.add_argument("--lane-inputs", choices=sorted(LANES))
    group.add_argument("--lane", choices=sorted(LANES))
    group.add_argument("--synthesis-inputs", action="store_true")
    group.add_argument("--synthesis", action="store_true")
    group.add_argument("--apply-inputs", action="store_true")
    group.add_argument("--apply", action="store_true")
    group.add_argument("--loop", action="store_true")
    args = parser.parse_args()
    try:
        if args.prepare:
            prepare()
        elif args.lane_inputs:
            lane_inputs(args.lane_inputs)
        elif args.lane:
            validate_lane(args.lane)
        elif args.synthesis_inputs:
            validate_synthesis_inputs()
        elif args.synthesis:
            validate_synthesis()
        elif args.apply_inputs:
            validate_apply_inputs()
        elif args.apply:
            validate_apply()
        else:
            validate_loop()
    except ContractError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
