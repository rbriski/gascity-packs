#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=delivery-common.sh
source "$SCRIPT_DIR/delivery-common.sh"
delivery_initialize_context

delivery_resolve_contained_path() {
  local value="$1"
  local label="$2"
  local resolved

  if ! resolved="$(python3 - "$DELIVERY_WORK_DIR" "$value" <<'PY'
import os
import sys
from pathlib import Path

root_value, candidate_value = sys.argv[1:]
if not candidate_value:
    raise SystemExit(1)
root = Path(root_value).resolve(strict=True)
if not root.is_dir():
    raise SystemExit(1)
raw = Path(candidate_value)
if not raw.is_absolute() and ".." in raw.parts:
    raise SystemExit(1)
candidate = raw.resolve(strict=False) if raw.is_absolute() else (root / raw).resolve(strict=False)
try:
    candidate.relative_to(root)
except ValueError:
    raise SystemExit(1)
print(os.fspath(candidate))
PY
  )"; then
    delivery_fail "$label must resolve within the canonical delivery work directory"
  fi
  printf '%s' "$resolved"
}

delivery_resolve_delivery_artifact_path() {
  local value="$1"
  local label="$2"
  local artifact_root="$3"
  local resolved

  if ! resolved="$(python3 - "$DELIVERY_WORK_DIR" "$artifact_root" "$value" <<'PY'
import os
import sys
from pathlib import Path

work_value, artifact_value, candidate_value = sys.argv[1:]

def fail():
    raise SystemExit(1)

def lexical_relative(raw: Path, root: Path):
    if raw.is_absolute():
        try:
            return raw.relative_to(root)
        except ValueError:
            fail()
    if ".." in raw.parts:
        fail()
    return raw

work = Path(work_value).resolve(strict=True)
if not work.is_dir():
    fail()
artifact_raw = Path(artifact_value)
candidate_raw = Path(candidate_value)
if not artifact_value or not candidate_value:
    fail()
artifact_relative = lexical_relative(artifact_raw, work)
candidate_relative = lexical_relative(candidate_raw, work)
delivery_relative = artifact_relative / "delivery"
if candidate_relative == delivery_relative or delivery_relative not in candidate_relative.parents:
    fail()

# Reject any symlink in the authority chain, including the artifact root,
# delivery directory, and final document.  The later reader therefore cannot
# be redirected outside the approved delivery authority directory.
current = work
for component in candidate_relative.parts:
    current /= component
    if current.is_symlink():
        fail()
try:
    resolved = current.resolve(strict=True)
    delivery = (work / delivery_relative).resolve(strict=True)
    resolved.relative_to(delivery)
except (OSError, ValueError):
    fail()
print(os.fspath(resolved))
PY
  )"; then
    delivery_fail "$label must resolve within the canonical artifact delivery directory without symlinks"
  fi
  printf '%s' "$resolved"
}

# Render a deterministic pre-task authority manifest.  Build-base normally
# derives blank paths in its prepare task, but task descriptions are compiled
# before that task runs; Complete Delivery therefore binds these values in its
# launcher and workers must reject any drift before doing work.
if [ "${1:-}" = "--context" ]; then
  STAGE="${2:-}"
  case "$STAGE" in
    requirements|plan|decompose|finalize) ;;
    *) delivery_fail "--context requires requirements, plan, decompose, or finalize" ;;
  esac

  ARTIFACT_ROOT="$(delivery_var artifact_root '')"
  [ -n "$ARTIFACT_ROOT" ] || delivery_fail "gc.var.artifact_root is missing"
  SOURCE_ID="$(delivery_root_metadata gc.var.source_bead_id)"
  [ -n "$SOURCE_ID" ] || delivery_fail "gc.var.source_bead_id is missing"

  REQUIREMENTS_PATH="$(delivery_root_metadata gc.var.requirements_path)"
  PLAN_PATH="$(delivery_root_metadata gc.var.plan_path)"
  DECOMPOSITION_PATH="$(delivery_root_metadata gc.var.decomposition_path)"
  FINAL_REPORT_PATH="$(delivery_root_metadata gc.var.final_report_path)"
  for binding in \
    "requirements_path:$REQUIREMENTS_PATH" \
    "plan_path:$PLAN_PATH" \
    "decomposition_path:$DECOMPOSITION_PATH" \
    "final_report_path:$FINAL_REPORT_PATH"; do
    case "$binding" in *:) delivery_fail "gc.var.${binding%%:*} is missing" ;; esac
  done

  ATTEMPT="$(delivery_metadata_value "$DELIVERY_STEP_JSON" gc.attempt)"
  [ -n "$ATTEMPT" ] || ATTEMPT="1"
  CONTROL_ID=""
  ATTEMPT_LOG=""
  if ! [[ "$ATTEMPT" =~ ^[1-9][0-9]*$ ]]; then
    delivery_fail "gc.attempt must be a positive integer"
  fi
  if [ "$ATTEMPT" -gt 1 ]; then
    CONTROL_ID="$(delivery_metadata_value "$DELIVERY_STEP_JSON" gc.control_for)"
    [[ "$CONTROL_ID" =~ ^[A-Za-z0-9._-]+$ ]] || \
      delivery_fail "retry gc.control_for must be one durable bead ID"
    CONTROL_JSON="$(delivery_read_bead_json "$CONTROL_ID")" || \
      delivery_fail "gc bd show $CONTROL_ID failed while resolving retry context"
    delivery_json_is_valid "$CONTROL_JSON" || \
      delivery_fail "gc bd show $CONTROL_ID returned invalid JSON"
    delivery_retry_lineage_is_valid "$CONTROL_JSON" || \
      delivery_fail "retry gc.control_for has ambiguous or invalid logical lineage"
    ATTEMPT_LOG="$(delivery_metadata_value "$CONTROL_JSON" gc.attempt_log)"
    [ -n "$ATTEMPT_LOG" ] || \
      delivery_fail "logical control $CONTROL_ID has no gc.attempt_log for retry $ATTEMPT"
  fi

  DELIVERY_CONTEXT_STAGE="$STAGE" \
  DELIVERY_CONTEXT_WORK_DIR="$DELIVERY_WORK_DIR" \
  DELIVERY_CONTEXT_ARTIFACT_ROOT="$ARTIFACT_ROOT" \
  DELIVERY_CONTEXT_SOURCE_ID="$SOURCE_ID" \
  DELIVERY_CONTEXT_ATTEMPT="$ATTEMPT" \
  DELIVERY_CONTEXT_CONTROL_ID="$CONTROL_ID" \
  DELIVERY_CONTEXT_ATTEMPT_LOG="$ATTEMPT_LOG" \
  DELIVERY_CONTEXT_REQUIREMENTS_PATH="$REQUIREMENTS_PATH" \
  DELIVERY_CONTEXT_PLAN_PATH="$PLAN_PATH" \
  DELIVERY_CONTEXT_DECOMPOSITION_PATH="$DECOMPOSITION_PATH" \
  DELIVERY_CONTEXT_FINAL_REPORT_PATH="$FINAL_REPORT_PATH" \
  python3 - <<'PY'
import json
import os
from pathlib import Path

stage = os.environ["DELIVERY_CONTEXT_STAGE"]
work = Path(os.environ["DELIVERY_CONTEXT_WORK_DIR"]).resolve(strict=True)
artifact_raw = Path(os.environ["DELIVERY_CONTEXT_ARTIFACT_ROOT"])
if not os.environ["DELIVERY_CONTEXT_ARTIFACT_ROOT"] or ".." in artifact_raw.parts:
    raise SystemExit("complete-delivery-check: artifact_root is not a safe canonical path")
artifact = artifact_raw if artifact_raw.is_absolute() else work / artifact_raw
try:
    artifact_relative = artifact.relative_to(work)
except ValueError:
    raise SystemExit("complete-delivery-check: artifact_root must resolve beneath the delivery work directory")
if not artifact_relative.parts:
    raise SystemExit("complete-delivery-check: artifact_root must not be the delivery work directory")

names = {
    "requirements_path": "requirements.md",
    "plan_path": "implementation-plan.md",
    "decomposition_path": "decomposition.md",
    "final_report_path": "final-report.md",
}
paths = {}
for key, filename in names.items():
    value = os.environ[f"DELIVERY_CONTEXT_{key.upper()}"]
    raw = Path(value)
    if not value or ".." in raw.parts:
        raise SystemExit(f"complete-delivery-check: gc.var.{key} is not a safe path")
    candidate = raw if raw.is_absolute() else work / raw
    try:
        relative = candidate.relative_to(work)
    except ValueError:
        raise SystemExit(f"complete-delivery-check: gc.var.{key} must resolve beneath the delivery work directory")
    expected = artifact_relative / "delivery" / filename
    if relative != expected:
        raise SystemExit(f"complete-delivery-check: gc.var.{key} must equal {expected}")
    current = work
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise SystemExit(f"complete-delivery-check: gc.var.{key} has a symlink authority escape")
    paths[key] = str((work / relative).resolve(strict=False))

inputs = {
    "requirements": [],
    "plan": [paths["requirements_path"]],
    "decompose": [paths["requirements_path"], paths["plan_path"]],
    "finalize": [paths["requirements_path"], paths["plan_path"], paths["decomposition_path"]],
}[stage]
output_key = {"requirements": "requirements_path", "plan": "plan_path", "decompose": "decomposition_path", "finalize": "final_report_path"}[stage]
print(json.dumps({
    "stage": stage,
    "source_bead_id": os.environ["DELIVERY_CONTEXT_SOURCE_ID"],
    "attempt": int(os.environ["DELIVERY_CONTEXT_ATTEMPT"]),
    "logical_control_id": os.environ["DELIVERY_CONTEXT_CONTROL_ID"],
    "attempt_log": os.environ["DELIVERY_CONTEXT_ATTEMPT_LOG"],
    "canonical_paths": paths,
    "permitted_input_paths": inputs,
    "permitted_output_paths": [paths[output_key]],
}, sort_keys=True))
PY
  exit 0
fi

# A deployed pack supplies the common checker beside this materialized script.
# A source-tree caller may select one only from workflow-root policy, never a
# step-controlled value. Canonicalize configured paths inside the worktree so
# an absolute path, parent traversal, or symlink cannot execute another pack.
ROOT_GENERIC_CHECK="$(delivery_root_metadata gc.var.build_artifact_valid_path)"
STEP_GENERIC_CHECK="$(delivery_metadata_value "$DELIVERY_STEP_JSON" gc.var.build_artifact_valid_path)"
if [ "$DELIVERY_BEAD_ID" != "$DELIVERY_ROOT_ID" ]; then
  [ -z "$STEP_GENERIC_CHECK" ] || \
    delivery_fail "build-artifact-valid.sh path must be configured on the workflow root"
fi
if [ -z "$ROOT_GENERIC_CHECK" ]; then
  GENERIC_CHECK="$SCRIPT_DIR/build-artifact-valid.sh"
else
  GENERIC_CHECK="$(delivery_resolve_contained_path "$ROOT_GENERIC_CHECK" "build-artifact-valid.sh path")"
fi
[ -f "$GENERIC_CHECK" ] || delivery_fail "build-artifact-valid.sh is unavailable"

SCHEMA="$(delivery_metadata_value "$DELIVERY_STEP_JSON" "gc.build.artifact_schema")"
case "$SCHEMA" in
  gc.build.requirements.v1|gc.build.plan.v1|gc.build.decomposition.v1|gc.build.final-report.v1) ;;
  *) delivery_fail "source-artifact check does not support schema $SCHEMA" ;;
esac

PATH_KEYS="$(delivery_metadata_value "$DELIVERY_STEP_JSON" "gc.build.artifact_path_keys")"
[ -n "$PATH_KEYS" ] || delivery_fail "gc.build.artifact_path_keys is missing"
ARTIFACT_ROOT="$(delivery_var artifact_root '')"
[ -n "$ARTIFACT_ROOT" ] || delivery_fail "gc.var.artifact_root is missing"
ARTIFACT_PATH=""
IFS=',' read -r -a KEYS <<<"$PATH_KEYS"
for key in "${KEYS[@]}"; do
  key="$(printf '%s' "$key" | tr -d '[:space:]')"
  [ -n "$key" ] || continue
  value="$(delivery_root_metadata "$key")"
  if [ -n "$value" ]; then
    ARTIFACT_PATH="$(delivery_resolve_delivery_artifact_path "$value" "source-bound artifact path" "$ARTIFACT_ROOT")"
    break
  fi
done
[ -n "$ARTIFACT_PATH" ] || delivery_fail "source-bound artifact path is missing"
[ -f "$ARTIFACT_PATH" ] || delivery_fail "source-bound artifact does not exist: $ARTIFACT_PATH"

# Reject foreign paths before the inherited checker can open them, then preserve
# its schema and trace gate before applying the Complete Delivery refinement.
bash "$GENERIC_CHECK"

STEP_SOURCE_ID="$(delivery_metadata_value "$DELIVERY_STEP_JSON" gc.var.source_bead_id)"
STEP_SOURCE_TITLE="$(delivery_metadata_value "$DELIVERY_STEP_JSON" gc.var.source_title)"
if [ "$DELIVERY_BEAD_ID" != "$DELIVERY_ROOT_ID" ]; then
  [ -z "$STEP_SOURCE_ID" ] || \
    delivery_fail "gc.var.source_bead_id must be configured on the workflow root"
  [ -z "$STEP_SOURCE_TITLE" ] || \
    delivery_fail "gc.var.source_title must be configured on the workflow root"
fi
SOURCE_ID="$(delivery_root_metadata gc.var.source_bead_id)"
SOURCE_TITLE="$(delivery_root_metadata gc.var.source_title)"
[ -n "$SOURCE_ID" ] || delivery_fail "gc.var.source_bead_id is missing"
[ -n "$SOURCE_TITLE" ] || delivery_fail "gc.var.source_title is missing"
python3 -c 'import yaml' >/dev/null 2>&1 || \
  delivery_fail "PyYAML is required for Complete Delivery source-artifact validation"

SOURCE_JSON="$(delivery_read_bead_json "$SOURCE_ID")" || \
  delivery_fail "source $SOURCE_ID is unreadable"
delivery_json_is_valid "$SOURCE_JSON" || delivery_fail "source $SOURCE_ID returned invalid JSON"
SOURCE_FIELDS="$(printf '%s' "$SOURCE_JSON" | \
  DELIVERY_SOURCE_ID="$SOURCE_ID" \
  DELIVERY_SOURCE_TITLE="$SOURCE_TITLE" \
  DELIVERY_SOURCE_SCHEMA="$SCHEMA" \
  python3 -c '
import json
import os
import sys

expected_id = os.environ["DELIVERY_SOURCE_ID"]
expected_title = os.environ["DELIVERY_SOURCE_TITLE"]
schema = os.environ["DELIVERY_SOURCE_SCHEMA"]
data = json.load(sys.stdin)
if isinstance(data, list):
    if len(data) != 1:
        raise SystemExit(1)
    data = data[0]
if not isinstance(data, dict):
    raise SystemExit(1)
source_id = data.get("id")
title = data.get("title")
acceptance_criteria = data.get("acceptance_criteria")
if not isinstance(source_id, str) or not source_id.strip():
    raise SystemExit(1)
if not isinstance(title, str) or not title.strip():
    raise SystemExit(1)
title = title.strip()
if (
    not isinstance(acceptance_criteria, str) or not acceptance_criteria.strip()
):
    raise SystemExit(1)
if source_id != expected_id or title != expected_title:
    raise SystemExit(1)
print(json.dumps({
    "id": source_id,
    "title": title,
    "acceptance_criteria": acceptance_criteria,
}))
')" || \
  delivery_fail "source $SOURCE_ID is ambiguous, incomplete, or does not exactly match configured source identity"

REQUIREMENTS_PATH=""
PLAN_PATH=""
DECOMPOSITION_PATH=""
if [ "$SCHEMA" = "gc.build.final-report.v1" ]; then
  REQUIREMENTS_PATH="$(delivery_root_metadata gc.build.requirements_path)"
  PLAN_PATH="$(delivery_root_metadata gc.build.plan_path)"
  DECOMPOSITION_PATH="$(delivery_root_metadata gc.build.decomposition_path)"
  [ -n "$REQUIREMENTS_PATH" ] || \
    delivery_fail "gc.build.requirements_path is required to finalize source traceability"
  [ -n "$PLAN_PATH" ] || \
    delivery_fail "gc.build.plan_path is required to finalize source traceability"
  [ -n "$DECOMPOSITION_PATH" ] || \
    delivery_fail "gc.build.decomposition_path is required to finalize source traceability"
  REQUIREMENTS_PATH="$(delivery_resolve_delivery_artifact_path "$REQUIREMENTS_PATH" "approved requirements artifact path" "$ARTIFACT_ROOT")"
  PLAN_PATH="$(delivery_resolve_delivery_artifact_path "$PLAN_PATH" "approved plan artifact path" "$ARTIFACT_ROOT")"
  DECOMPOSITION_PATH="$(delivery_resolve_delivery_artifact_path "$DECOMPOSITION_PATH" "approved decomposition artifact path" "$ARTIFACT_ROOT")"
fi

DELIVERY_SOURCE_FIELDS="$SOURCE_FIELDS" \
DELIVERY_REQUIREMENTS_PATH="$REQUIREMENTS_PATH" \
DELIVERY_PLAN_PATH="$PLAN_PATH" \
DELIVERY_DECOMPOSITION_PATH="$DECOMPOSITION_PATH" \
python3 - "$ARTIFACT_PATH" "$SCHEMA" <<'PY'
import hashlib
import json
import os
import re
import sys
from pathlib import Path

import yaml

path = Path(sys.argv[1])
schema = sys.argv[2]
source_record = json.loads(os.environ["DELIVERY_SOURCE_FIELDS"])
expected_id = source_record["id"]
expected_title = source_record["title"]
acceptance_hash = "sha256:" + hashlib.sha256(
    source_record["acceptance_criteria"].encode("utf-8")
).hexdigest()


def artifact_parts(artifact_path: Path, context: str):
    try:
        text = artifact_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(
            f"complete-delivery-check: {context} is unreadable: {exc}"
        ) from exc
    match = re.match(
        r"\A---\n(?P<front>.*?)\n---(?:\n|\Z)(?P<body>.*)\Z",
        text,
        re.DOTALL,
    )
    if not match:
        raise SystemExit(
            f"complete-delivery-check: {context} has no YAML front matter"
        )
    front = yaml.safe_load(match.group("front")) or {}
    if not isinstance(front, dict):
        raise SystemExit(
            f"complete-delivery-check: {context} front matter must be a mapping"
        )
    return front, match.group("body")


def validate_source_binding(front, context: str):
    source = front.get("source")
    if not isinstance(source, dict):
        raise SystemExit(
            f"complete-delivery-check: {context} requires a source mapping"
        )
    expected = {
        "id": expected_id,
        "title": expected_title,
        "anchor": f"gc:{expected_id}",
        "acceptance_criteria_sha256": acceptance_hash,
    }
    for key, value in expected.items():
        if source.get(key) == value:
            continue
        raise SystemExit(
            f"complete-delivery-check: {context} source.{key} must equal {value!r}"
        )
    return source


front, body = artifact_parts(path, "source-bound artifact")
source = validate_source_binding(front, "source-bound artifact")


def markdown_outside_fences(markdown: str) -> str:
    visible = []
    fence = None
    for line in markdown.splitlines():
        if fence is not None:
            character, minimum = fence
            if re.fullmatch(
                rf" {{0,3}}{re.escape(character)}{{{minimum},}}[ \t]*", line
            ):
                fence = None
            continue
        opening = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if opening:
            marker = opening.group(1)
            fence = (marker[0], len(marker))
            continue
        visible.append(line)
    return "\n".join(visible)


visible = markdown_outside_fences(body)
if schema in {
    "gc.build.requirements.v1",
    "gc.build.plan.v1",
    "gc.build.decomposition.v1",
}:
    if not re.search(r"^##[ \t]+Source Intent[ \t]*$", visible, re.MULTILINE):
        raise SystemExit(
            "complete-delivery-check: source-bound artifact requires a Source Intent section"
        )
elif schema == "gc.build.final-report.v1":
    upstream_artifacts = (
        ("gc.build.requirements.v1", os.environ["DELIVERY_REQUIREMENTS_PATH"]),
        ("gc.build.plan.v1", os.environ["DELIVERY_PLAN_PATH"]),
        ("gc.build.decomposition.v1", os.environ["DELIVERY_DECOMPOSITION_PATH"]),
    )
    for upstream_schema, upstream_path in upstream_artifacts:
        context = f"approved {upstream_schema} artifact"
        upstream_front, _ = artifact_parts(Path(upstream_path), context)
        if upstream_front.get("schema") != upstream_schema:
            raise SystemExit(
                f"complete-delivery-check: {context} schema must equal {upstream_schema!r}"
            )
        if upstream_front.get("status") != "approved":
            raise SystemExit(
                f"complete-delivery-check: {context} status must equal 'approved'"
            )
        validate_source_binding(upstream_front, context)
    trace_match = re.search(
        r"^##[ \t]+Source trace[ \t]*$(?P<content>.*?)(?=^##[ \t]+|\Z)",
        visible,
        re.MULTILINE | re.DOTALL,
    )
    if not trace_match:
        raise SystemExit(
            "complete-delivery-check: final report requires an unfenced Source trace section"
        )
    trace = trace_match.group("content")
    for label, value in (
        ("Source ID", f"Source ID: {expected_id}"),
        ("Source title", f"Source title: {expected_title}"),
        (
            "Acceptance criteria SHA-256",
            f"Acceptance criteria SHA-256: {acceptance_hash}",
        ),
    ):
        occurrences = [
            line
            for line in trace.splitlines()
            if re.match(rf"^[ \t]*{re.escape(label)}[ \t]*:", line)
        ]
        if occurrences != [value]:
            raise SystemExit(
                "complete-delivery-check: Source trace must contain exactly one "
                f"exact durable source value {value!r}"
            )
else:
    raise SystemExit(f"complete-delivery-check: unsupported schema {schema}")
PY

echo "complete-delivery source artifact valid: schema=$SCHEMA source=$SOURCE_ID path=$ARTIFACT_PATH"
