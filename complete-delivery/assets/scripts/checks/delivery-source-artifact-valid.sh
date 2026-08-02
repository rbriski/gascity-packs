#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=delivery-common.sh
source "$SCRIPT_DIR/delivery-common.sh"
delivery_initialize_context

# A deployed pack supplies the common checker beside this materialized script.
# Source-tree and other non-materialized callers must configure it explicitly;
# never walk repository-relative paths that can silently select a different
# pack revision.
GENERIC_CHECK="$(delivery_var build_artifact_valid_path '')"
if [ -z "$GENERIC_CHECK" ]; then
  GENERIC_CHECK="$SCRIPT_DIR/build-artifact-valid.sh"
else
  GENERIC_CHECK="$(delivery_resolve_path "$GENERIC_CHECK")"
fi
[ -f "$GENERIC_CHECK" ] || delivery_fail "build-artifact-valid.sh is unavailable"

# Preserve the inherited schema and trace gate before applying the
# Complete Delivery source-binding refinement.
bash "$GENERIC_CHECK"

SCHEMA="$(delivery_metadata_value "$DELIVERY_STEP_JSON" "gc.build.artifact_schema")"
case "$SCHEMA" in
  gc.build.requirements.v1|gc.build.plan.v1|gc.build.decomposition.v1|gc.build.final-report.v1) ;;
  *) delivery_fail "source-artifact check does not support schema $SCHEMA" ;;
esac

PATH_KEYS="$(delivery_metadata_value "$DELIVERY_STEP_JSON" "gc.build.artifact_path_keys")"
[ -n "$PATH_KEYS" ] || delivery_fail "gc.build.artifact_path_keys is missing"
ARTIFACT_PATH=""
IFS=',' read -r -a KEYS <<<"$PATH_KEYS"
for key in "${KEYS[@]}"; do
  key="$(printf '%s' "$key" | tr -d '[:space:]')"
  [ -n "$key" ] || continue
  value="$(delivery_root_metadata "$key")"
  if [ -n "$value" ]; then
    ARTIFACT_PATH="$(delivery_resolve_path "$value")"
    break
  fi
done
[ -n "$ARTIFACT_PATH" ] || delivery_fail "source-bound artifact path is missing"
[ -f "$ARTIFACT_PATH" ] || delivery_fail "source-bound artifact does not exist: $ARTIFACT_PATH"

SOURCE_ID="$(delivery_var source_bead_id "")"
SOURCE_TITLE="$(delivery_var source_title "")"
[ -n "$SOURCE_ID" ] || delivery_fail "gc.var.source_bead_id is missing"
[ -n "$SOURCE_TITLE" ] || delivery_fail "gc.var.source_title is missing"
python3 -c 'import yaml' >/dev/null 2>&1 || \
  delivery_fail "PyYAML is required for Complete Delivery source-artifact validation"

SOURCE_JSON="$(delivery_read_bead_json "$SOURCE_ID")" || \
  delivery_fail "source $SOURCE_ID is unreadable"
delivery_json_is_valid "$SOURCE_JSON" || delivery_fail "source $SOURCE_ID returned invalid JSON"
SOURCE_FIELDS="$(printf '%s' "$SOURCE_JSON" | python3 -c '
import json
import sys

expected_id = sys.argv[1]
expected_title = sys.argv[2]
schema = sys.argv[3]
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
if schema == "gc.build.final-report.v1" and (
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
' "$SOURCE_ID" "$SOURCE_TITLE" "$SCHEMA")" || \
  delivery_fail "source $SOURCE_ID is ambiguous, incomplete, or does not exactly match configured source identity"

python3 - "$ARTIFACT_PATH" "$SCHEMA" "$SOURCE_FIELDS" <<'PY'
import hashlib
import json
import re
import sys
from pathlib import Path

import yaml

path = Path(sys.argv[1])
schema = sys.argv[2]
source_record = json.loads(sys.argv[3])
expected_id = source_record["id"]
expected_title = source_record["title"]
acceptance_hash = ""
if schema == "gc.build.final-report.v1":
    acceptance_hash = "sha256:" + hashlib.sha256(
        source_record["acceptance_criteria"].encode("utf-8")
    ).hexdigest()
text = path.read_text(encoding="utf-8")
match = re.match(r"\A---\n(?P<front>.*?)\n---(?:\n|\Z)(?P<body>.*)\Z", text, re.DOTALL)
if not match:
    raise SystemExit("complete-delivery-check: source-bound artifact has no YAML front matter")
front = yaml.safe_load(match.group("front")) or {}
source = front.get("source") if isinstance(front, dict) else None
if not isinstance(source, dict):
    raise SystemExit("complete-delivery-check: source-bound artifact requires a source mapping")
expected = {"id": expected_id, "title": expected_title, "anchor": f"gc:{expected_id}"}
for key, value in expected.items():
    if source.get(key) != value:
        raise SystemExit(
            f"complete-delivery-check: source.{key} must equal {value!r}"
        )
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


visible = markdown_outside_fences(match.group("body"))
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
    if source.get("acceptance_criteria_sha256") != acceptance_hash:
        raise SystemExit(
            "complete-delivery-check: source.acceptance_criteria_sha256 must equal "
            f"{acceptance_hash!r}"
        )
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
    for value in (
        f"Source ID: `{expected_id}`",
        f"Source title: {expected_title}",
        f"Acceptance criteria SHA-256: {acceptance_hash}",
    ):
        if value not in trace:
            raise SystemExit(
                "complete-delivery-check: Source trace must bind exact durable source "
                f"value {value!r}"
            )
else:
    raise SystemExit(f"complete-delivery-check: unsupported schema {schema}")
PY

echo "complete-delivery source artifact valid: schema=$SCHEMA source=$SOURCE_ID path=$ARTIFACT_PATH"
