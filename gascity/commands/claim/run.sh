#!/bin/sh
# gc <binding> claim — atomically claim and verify one routed work bead.

set -eu

acknowledge_drain() {
    if ! command -v gc >/dev/null 2>&1; then
        return 1
    fi
    gc runtime drain-ack >/dev/null 2>&1
}

acknowledge_drain_or_report() {
    if ! acknowledge_drain; then
        echo "DRAIN_ACK_FAILED gc runtime drain-ack did not complete" >&2
        return 1
    fi
}

if [ -z "${GC_PACK_DIR:-}" ]; then
    echo "CONFIG_REJECTED gc gascity claim: missing Gas City pack context" >&2
    acknowledge_drain_or_report || true
    exit 1
fi

while [ "$#" -gt 0 ]; do
    case "$1" in
        gc|gascity|claim|--city=*|--rig=*)
            shift
            ;;
        --city|--rig)
            if [ "$#" -lt 2 ]; then
                echo "gc ${GC_PACK_NAME:-gascity} claim: missing value for $1" >&2
                exit 2
            fi
            shift 2
            ;;
        *)
            break
            ;;
    esac
done

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
    cat "$GC_PACK_DIR/commands/claim/help.md"
    exit 0
fi

if [ "$#" -ne 0 ]; then
    echo "gc ${GC_PACK_NAME:-gascity} claim: no arguments accepted" >&2
    exit 2
fi

if ! command -v gc >/dev/null 2>&1; then
    echo "CONFIG_REJECTED gc ${GC_PACK_NAME:-gascity} claim: gc binary not in PATH" >&2
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "CONFIG_REJECTED gc ${GC_PACK_NAME:-gascity} claim: python3 not in PATH" >&2
    acknowledge_drain_or_report || true
    exit 1
fi

json_pick() {
    python3 -c '
import json
import sys

path = sys.argv[1]
try:
    data = json.load(sys.stdin)
except Exception:
    print("")
    raise SystemExit(0)

if isinstance(data, list):
    data = data[0] if data else {}
if not isinstance(data, dict):
    print("")
    raise SystemExit(0)

if path.startswith("metadata:"):
    metadata = data.get("metadata") or {}
    value = metadata.get(path.split(":", 1)[1], "") if isinstance(metadata, dict) else ""
else:
    value = data.get(path, "")

if value is None:
    value = ""
print(value if isinstance(value, str) else str(value))
' "$1"
}

EXPECTED_ASSIGNEE="${BEADS_ACTOR:-${GC_SESSION_NAME:-${GC_SESSION_ID:-${GC_AGENT:-}}}}"
EXPECTED_ROUTE="${GC_TEMPLATE:-${GC_AGENT:-}}"

if [ -z "$EXPECTED_ASSIGNEE" ]; then
    echo "CONFIG_REJECTED gc ${GC_PACK_NAME:-gascity} claim: missing expected assignee" >&2
    acknowledge_drain_or_report || true
    exit 1
fi

claim_file="$(mktemp)"
show_file="$(mktemp)"
control_file="$(mktemp)"
err_file="$(mktemp)"
cleanup() {
    rm -f "$claim_file" "$show_file" "$control_file" "$err_file"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

max_attempts=3
claim_try=0
work_id=""
while [ "$claim_try" -lt "$max_attempts" ]; do
    claim_try=$((claim_try + 1))
    if gc hook --claim --drain-ack --json >"$claim_file" 2>"$err_file"; then
        claim_code=0
    else
        claim_code=$?
    fi

    claim_action="$(json_pick action <"$claim_file")"
    work_id="$(json_pick bead_id <"$claim_file")"
    claim_assignee="$(json_pick assignee <"$claim_file")"
    claim_route="$(json_pick route <"$claim_file")"

    if [ "$claim_code" -eq 0 ] && [ "$claim_action" = "drain" ]; then
        cat "$claim_file"
        exit 0
    fi

    if [ "$claim_code" -eq 0 ] && [ "$claim_action" = "work" ] && [ -n "$work_id" ]; then
        break
    fi

    work_id=""
    if [ -s "$err_file" ]; then
        printf 'CLAIM_RETRY %s/%s gc hook --claim failed: %s\n' \
            "$claim_try" "$max_attempts" "$(sed -n '1p' "$err_file")" >&2
    else
        printf 'CLAIM_RETRY %s/%s unexpected gc hook --claim result\n' \
            "$claim_try" "$max_attempts" >&2
    fi
    if [ "$claim_try" -lt "$max_attempts" ]; then
        sleep 2
    fi
done

if [ -z "$work_id" ]; then
    printf 'CLAIM_REJECTED gc hook --claim returned no workable bead after %s attempts\n' \
        "$max_attempts" >&2
    exit 1
fi

hook_assignee="$claim_assignee"
hook_route="$claim_route"
verified=0
verify_try=0
while [ "$verify_try" -lt "$max_attempts" ]; do
    verify_try=$((verify_try + 1))
    if ! gc bd show "$work_id" --json >"$show_file" 2>"$err_file"; then
        if [ -s "$err_file" ]; then
            printf 'CLAIM_RETRY %s/%s bead read failed for %s: %s\n' \
                "$verify_try" "$max_attempts" "$work_id" "$(sed -n '1p' "$err_file")" >&2
        else
            printf 'CLAIM_RETRY %s/%s bead read failed for %s\n' \
                "$verify_try" "$max_attempts" "$work_id" >&2
        fi
    else
        claim_id="$(json_pick id <"$show_file")"
        claim_status="$(json_pick status <"$show_file")"
        show_assignee="$(json_pick assignee <"$show_file")"
        show_route="$(json_pick metadata:gc.routed_to <"$show_file")"
        claim_assignee="$hook_assignee"
        claim_route="$hook_route"
        [ -n "$show_assignee" ] && claim_assignee="$show_assignee"
        [ -n "$show_route" ] && claim_route="$show_route"

        if [ -z "$claim_id" ] || [ -z "$claim_status" ] || [ -z "$claim_assignee" ]; then
            printf 'CLAIM_RETRY %s/%s incomplete bead record for %s\n' \
                "$verify_try" "$max_attempts" "$work_id" >&2
        elif [ "$claim_id" != "$work_id" ]; then
            printf 'CLAIM_REJECTED verification failed for %s\n' "$work_id" >&2
            break
        elif [ "$claim_status" != "open" ] && [ "$claim_status" != "in_progress" ]; then
            printf 'CLAIM_REJECTED unexpected status for %s: %s\n' \
                "$work_id" "$claim_status" >&2
            break
        elif [ "$claim_assignee" != "$EXPECTED_ASSIGNEE" ]; then
            printf 'CLAIM_REJECTED assignee mismatch for %s\n' "$work_id" >&2
            break
        elif [ -n "$EXPECTED_ROUTE" ] && [ -n "$claim_route" ] && [ "$claim_route" != "$EXPECTED_ROUTE" ]; then
            printf 'CLAIM_REJECTED route mismatch for %s\n' "$work_id" >&2
            break
        else
            verified=1
            break
        fi
    fi

    if [ "$verify_try" -lt "$max_attempts" ]; then
        sleep 1
    fi
done

if [ "$verified" -ne 1 ]; then
    printf 'CLAIM_REJECTED verification failed for %s after %s attempts\n' \
        "$work_id" "$verify_try" >&2
    exit 1
fi

if python3 - "$show_file" <<'PY'
import json
import sys

try:
    bead = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    raise SystemExit(1)
if isinstance(bead, list):
    bead = bead[0] if len(bead) == 1 else {}
description = bead.get("description") if isinstance(bead, dict) else None
raise SystemExit(0 if isinstance(description, str) and not description.strip() else 1)
PY
then
    control_id="$(json_pick metadata:gc.control_for <"$show_file")"
    case "$control_id" in
        ''|*[!A-Za-z0-9._-]*)
            printf 'CLAIM_REJECTED blank retry description has no valid gc.control_for bead ID\n' >&2
            exit 1
            ;;
    esac

    control_read=0
    control_try=0
    while [ "$control_try" -lt "$max_attempts" ]; do
        control_try=$((control_try + 1))
        if gc bd show "$control_id" --json >"$control_file" 2>"$err_file"; then
            control_read=1
            break
        fi
        if [ "$control_try" -lt "$max_attempts" ]; then
            sleep 1
        fi
    done
    if [ "$control_read" -ne 1 ]; then
        printf 'CLAIM_REJECTED could not read gc.control_for bead %s for blank retry recovery\n' \
            "$control_id" >&2
        exit 1
    fi
fi

python3 - "$show_file" "$control_file" <<'PY'
import json
import re
import sys

def one_bead(path, label):
    try:
        value = json.load(open(path, encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"{label} is not valid JSON: {exc}") from exc
    if isinstance(value, list):
        if len(value) != 1 or not isinstance(value[0], dict):
            raise ValueError(f"{label} must contain exactly one bead")
        return value[0]
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain one bead object")
    return value

def text(value):
    return value if isinstance(value, str) else ""

def fail(message):
    print(f"CLAIM_REJECTED blank retry description has ambiguous or invalid logical lineage: {message}", file=sys.stderr)
    raise SystemExit(1)

try:
    bead = one_bead(sys.argv[1], "claimed bead")
except ValueError as exc:
    fail(str(exc))

metadata = bead.get("metadata")
if not isinstance(metadata, dict):
    metadata = {}
description = text(bead.get("description"))
is_blank_retry = isinstance(bead.get("description"), str) and not description.strip()
logical_bead_id = bead["id"]

if is_blank_retry:
    try:
        control = one_bead(sys.argv[2], "control bead")
    except ValueError as exc:
        fail(str(exc))
    control_metadata = control.get("metadata")
    if not isinstance(control_metadata, dict):
        fail("retry and control beads must have metadata objects")

    control_id = text(metadata.get("gc.control_for"))
    attempt_number = text(metadata.get("gc.attempt"))
    step_id = text(metadata.get("gc.step_id"))
    step_ref = text(metadata.get("gc.step_ref"))
    control_ref = text(control_metadata.get("gc.step_ref"))
    root_id = text(metadata.get("gc.root_bead_id"))
    if not re.fullmatch(r"[A-Za-z0-9._-]+", control_id):
        fail("retry gc.control_for must be one durable bead ID")
    if not re.fullmatch(r"[1-9][0-9]*", attempt_number):
        fail("retry gc.attempt must be a positive integer")
    if text(control.get("id")) != control_id:
        fail("retry gc.control_for does not resolve to its exact control bead")
    if not text(control.get("description")).strip():
        fail("control bead has no reusable logical contract")
    if not root_id or text(control_metadata.get("gc.root_bead_id")) != root_id:
        fail("control bead workflow root does not match the retry")
    if not step_id or text(control_metadata.get("gc.step_id")) != step_id:
        fail("control bead step does not match the retry")
    if not text(metadata.get("gc.run_target")) or text(metadata.get("gc.run_target")) != text(control_metadata.get("gc.run_target")):
        fail("control bead run target does not match the retry")
    if text(bead.get("title")) != text(control.get("title")):
        fail("control bead title does not match the retry")
    if not control_ref or step_ref != f"{control_ref}.iteration.{attempt_number}":
        fail("retry step reference is not the control bead iteration")
    if text(metadata.get("gc.idempotency_key")) != f"{control_id}:attempt:{attempt_number}":
        fail("retry idempotency key does not bind the control bead and attempt")

    # The retry remains the only bead that workers may update or close.  Its
    # task text is replaced only after the durable control-bead lineage above
    # proves that the two beads represent the same logical contract.
    bead["description"] = control["description"]
    logical_bead_id = control_id

print(json.dumps({
    "action": "work",
    "bead_id": bead["id"],
    "root_bead_id": metadata.get("gc.root_bead_id", ""),
    "continuation_group": metadata.get("gc.continuation_group", ""),
    "logical_bead_id": logical_bead_id,
    "bead": bead,
}, separators=(",", ":")))
PY
