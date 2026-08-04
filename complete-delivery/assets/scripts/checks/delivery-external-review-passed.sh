#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=delivery-common.sh
source "$SCRIPT_DIR/delivery-common.sh"
delivery_initialize_context

# Expansions replace their source step's metadata in graph.v2. Locate the
# expansion target by its durable workflow lineage, rather than treating the
# source step's closed status as a successful external review.
WORKFLOW_JSON="$(gc bd list --all --include-gates --metadata-field "gc.root_bead_id=$DELIVERY_ROOT_ID" --json --limit 0)" || \
  delivery_fail "could not list workflow beads for external-review outcome"
delivery_json_is_valid "$WORKFLOW_JSON" || \
  delivery_fail "workflow bead list is not valid JSON"

python3 - "$WORKFLOW_JSON" <<'PY'
import json
import sys

try:
    beads = json.loads(sys.argv[1])
except json.JSONDecodeError as exc:
    raise SystemExit(f"workflow bead list is not valid JSON: {exc}") from exc

if not isinstance(beads, list):
    raise SystemExit("workflow bead list must be an array")

candidates = []
for bead in beads:
    if not isinstance(bead, dict):
        continue
    metadata = bead.get("metadata")
    if not isinstance(metadata, dict):
        continue
    if metadata.get("gc.step_id") != "external-review":
        continue
    if metadata.get("gc.kind") != "scope":
        continue
    candidates.append(bead)

if len(candidates) != 1:
    raise SystemExit(
        "expected exactly one external-review scope for this workflow; "
        f"found {len(candidates)}"
    )

scope = candidates[0]
metadata = scope["metadata"]
if scope.get("status") != "closed":
    raise SystemExit("external-review scope is not terminal")
if metadata.get("gc.outcome") != "pass":
    raise SystemExit("external-review scope did not pass")
PY
