#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=delivery-common.sh
source "$SCRIPT_DIR/delivery-common.sh"

GENERIC_CHECK="$SCRIPT_DIR/build-artifact-valid.sh"
if [ ! -f "$GENERIC_CHECK" ]; then
  GENERIC_CHECK="$SCRIPT_DIR/../../../../gascity/assets/scripts/checks/build-artifact-valid.sh"
fi
[ -f "$GENERIC_CHECK" ] || delivery_fail "build-artifact-valid.sh is unavailable"

# Preserve the inherited schema and trace gate before applying the
# Complete Delivery source-binding refinement.
bash "$GENERIC_CHECK"

delivery_initialize_context

SCHEMA="$(delivery_metadata_value "$DELIVERY_STEP_JSON" "gc.build.artifact_schema")"
case "$SCHEMA" in
  gc.build.requirements.v1|gc.build.plan.v1|gc.build.decomposition.v1) ;;
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

SOURCE_JSON="$(delivery_read_bead_json "$SOURCE_ID")" || \
  delivery_fail "source $SOURCE_ID is unreadable"
delivery_json_is_valid "$SOURCE_JSON" || delivery_fail "source $SOURCE_ID returned invalid JSON"
RESOLVED_SOURCE="$(printf '%s' "$SOURCE_JSON" | python3 -c '
import json
import sys

data = json.load(sys.stdin)
if isinstance(data, list):
    if len(data) != 1:
        raise SystemExit(1)
    data = data[0]
if not isinstance(data, dict):
    raise SystemExit(1)
source_id = data.get("id")
title = data.get("title")
if not isinstance(source_id, str) or not source_id.strip():
    raise SystemExit(1)
if not isinstance(title, str) or not title.strip():
    raise SystemExit(1)
print(source_id.strip())
print(title.strip())
')" || delivery_fail "source $SOURCE_ID is ambiguous or incomplete"
RESOLVED_ID="$(printf '%s\n' "$RESOLVED_SOURCE" | sed -n '1p')"
RESOLVED_TITLE="$(printf '%s\n' "$RESOLVED_SOURCE" | sed -n '2p')"
[ "$RESOLVED_ID" = "$SOURCE_ID" ] || delivery_fail "resolved source id does not match $SOURCE_ID"
[ "$RESOLVED_TITLE" = "$SOURCE_TITLE" ] || delivery_fail "resolved source title does not match gc.var.source_title"

python3 - "$ARTIFACT_PATH" "$SOURCE_ID" "$SOURCE_TITLE" <<'PY'
import re
import sys
from pathlib import Path

import yaml

path = Path(sys.argv[1])
expected_id = sys.argv[2]
expected_title = sys.argv[3]
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


if not re.search(
    r"^##[ \t]+Source Intent[ \t]*$",
    markdown_outside_fences(match.group("body")),
    re.MULTILINE,
):
    raise SystemExit("complete-delivery-check: source-bound artifact requires a Source Intent section")
PY

echo "complete-delivery source artifact valid: schema=$SCHEMA source=$SOURCE_ID path=$ARTIFACT_PATH"
