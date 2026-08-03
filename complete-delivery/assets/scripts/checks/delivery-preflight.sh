#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=delivery-common.sh
source "$SCRIPT_DIR/delivery-common.sh"
delivery_initialize_context

errors=()
require_enum() {
  local key="$1"
  local value="$2"
  shift 2
  local allowed
  for allowed in "$@"; do
    [ "$value" = "$allowed" ] && return 0
  done
  errors+=("$key must be one of: $* (got ${value:-empty})")
}

require_bool() {
  local key="$1"
  local value="$2"
  if [ "$value" != "true" ] && [ "$value" != "false" ]; then
    errors+=("$key must be true or false (got ${value:-empty})")
  fi
}

delivery_command_is_nonblank() {
  [[ "$1" =~ [^[:space:]] ]]
}

delivery_timeout_is_bounded() {
  python3 - "$1" <<'PY'
import re
import sys
from decimal import Decimal, InvalidOperation

match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?|\.[0-9]+)([smhd]?)", sys.argv[1])
if not match:
    raise SystemExit(1)
try:
    value = Decimal(match.group(1))
except InvalidOperation:
    raise SystemExit(1)
seconds = value * {
    "": Decimal(1), "s": Decimal(1), "m": Decimal(60),
    "h": Decimal(3600), "d": Decimal(86400),
}[match.group(2)]
raise SystemExit(0 if Decimal(0) < seconds <= Decimal(3600) else 1)
PY
}

is_https_url() {
  python3 - "$1" <<'PY'
import sys
from urllib.parse import urlsplit

value = sys.argv[1]
try:
    parsed = urlsplit(value)
    hostname = parsed.hostname
    parsed.port
except ValueError:
    raise SystemExit(1)
raise SystemExit(0 if parsed.scheme == "https" and hostname and not any(char.isspace() for char in value) else 1)
PY
}

delivery_ci_profile_is_valid() {
  python3 - "$1" "$2" <<'PY'
import re
import sys

workflow, environment = sys.argv[1:]
valid = (
    re.fullmatch(r"\.github/workflows/[A-Za-z0-9_./-]+\.ya?ml", workflow)
    and ".." not in workflow.split("/")
    and environment.strip() == environment
    and environment
    and len(environment) <= 255
    and not any(character in environment for character in "\r\n\t")
)
raise SystemExit(0 if valid else 1)
PY
}

PUSH="$(delivery_var push true)"
OPEN_PR="$(delivery_var open_pr true)"
ALLOW_NO_CI="$(delivery_var allow_no_ci false)"
ALLOW_NO_LOCAL="$(delivery_var allow_no_local_gates false)"
ALLOW_NO_SMOKE="$(delivery_var allow_no_smoke false)"
NO_SMOKE_REASON="$(delivery_var no_smoke_reason '')"
CODERABBIT="$(delivery_var coderabbit off)"
REQUIRED_CHECKS="$(delivery_var required_checks auto)"
MERGE_METHOD="$(delivery_var merge_method squash)"
DEPLOY_MODE="$(delivery_var deploy_mode command)"
DEPLOY_COMMAND="$(delivery_var deploy_command '')"
DEPLOY_CI_WORKFLOW="$(delivery_var deploy_ci_workflow '')"
DEPLOY_ENVIRONMENT="$(delivery_var deploy_environment '')"
VERIFY_COMMAND="$(delivery_var deploy_verify_command '')"
SMOKE_COMMAND="$(delivery_var smoke_command '')"
DEPLOY_TIMEOUT="$(delivery_var deploy_timeout 5m)"
VERIFY_TIMEOUT="$(delivery_var deploy_verify_timeout 5m)"
SMOKE_TIMEOUT="$(delivery_var smoke_timeout 5m)"
NA_REASON="$(delivery_var deploy_not_applicable_reason '')"
PRODUCTION_URL="$(delivery_var production_url '')"
BASE_BRANCH="$(delivery_var base_branch main)"
SOURCE_BEAD_ID="$(delivery_var source_bead_id '')"
SOURCE_TITLE="$(delivery_var source_title '')"
LAUNCHER_GITHUB_PREFLIGHT="$(delivery_var launcher_github_preflight '')"

require_bool push "$PUSH"
require_bool open_pr "$OPEN_PR"
require_bool allow_no_ci "$ALLOW_NO_CI"
require_bool allow_no_local_gates "$ALLOW_NO_LOCAL"
require_bool allow_no_smoke "$ALLOW_NO_SMOKE"
require_enum coderabbit "$CODERABBIT" required optional off
require_enum merge_method "$MERGE_METHOD" squash merge rebase
require_enum deploy_mode "$DEPLOY_MODE" command ci not-applicable

if [ "$ALLOW_NO_SMOKE" = "true" ] && ! delivery_command_is_nonblank "$NO_SMOKE_REASON"; then
  errors+=("no_smoke_reason is required and must be nonblank when allow_no_smoke=true")
fi

[ -n "$SOURCE_BEAD_ID" ] || errors+=("source_bead_id is required; launch from a durable work bead or convoy")
delivery_command_is_nonblank "$SOURCE_TITLE" || \
  errors+=("source_title is required; resolve the durable source title before launch")
case "$SOURCE_BEAD_ID" in
  *[!A-Za-z0-9._-]*|"") errors+=("source_bead_id must be a valid durable bead or convoy ID") ;;
esac
if [ -n "$SOURCE_BEAD_ID" ] && [[ "$SOURCE_BEAD_ID" =~ ^[A-Za-z0-9._-]+$ ]]; then
  if ! SOURCE_JSON="$(delivery_read_bead_json "$SOURCE_BEAD_ID")" || ! delivery_json_is_valid "$SOURCE_JSON"; then
    errors+=("source_bead_id must resolve to a readable durable bead or convoy")
  else
    SOURCE_RESOLVED_TITLE="$(printf '%s' "$SOURCE_JSON" | python3 -c '
import json
import sys

data = json.load(sys.stdin)
if isinstance(data, list):
    if len(data) != 1:
        raise SystemExit(1)
    data = data[0]
if not isinstance(data, dict):
    raise SystemExit(1)
title = data.get("title")
if not isinstance(title, str) or not title.strip():
    raise SystemExit(1)
if any(ord(character) < 32 or ord(character) == 127 for character in title):
    raise SystemExit(1)
print(title.strip())
')" || SOURCE_RESOLVED_TITLE=""
    if [ -z "$SOURCE_RESOLVED_TITLE" ]; then
      errors+=("source_bead_id must resolve to one durable source with a title")
    elif [ -n "$SOURCE_TITLE" ] && [ "$SOURCE_TITLE" != "$SOURCE_RESOLVED_TITLE" ]; then
      errors+=("source_title must exactly match the resolved durable source title")
    fi
  fi
fi

[ "$PUSH" = "true" ] || errors+=("push must be true for Complete Delivery")
[ "$OPEN_PR" = "true" ] || errors+=("open_pr must be true for Complete Delivery")
[ -n "$REQUIRED_CHECKS" ] || errors+=("required_checks must be auto or an exact comma-separated list")
if [ -n "$REQUIRED_CHECKS" ] && [ "$REQUIRED_CHECKS" != "auto" ]; then
  if ! python3 - "$REQUIRED_CHECKS" <<'PY'
import sys

names = [part.strip() for part in sys.argv[1].split(",")]
raise SystemExit(0 if names and all(names) and len(names) == len(set(names)) else 1)
PY
  then
    errors+=("required_checks must contain unique, nonempty exact check names")
  fi
fi

LOCAL_GATE_COUNT=0
for key in setup_command lint_command typecheck_command test_command build_command \
  browser_test_command security_command extra_gate_command; do
  [ -n "$(delivery_var "$key" '')" ] && LOCAL_GATE_COUNT=$((LOCAL_GATE_COUNT + 1))
done
if [ "$LOCAL_GATE_COUNT" -eq 0 ] && [ "$ALLOW_NO_LOCAL" != "true" ]; then
  errors+=("configure at least one repository gate or explicitly set allow_no_local_gates=true")
fi

case "$DEPLOY_MODE" in
  command)
    delivery_command_is_nonblank "$DEPLOY_COMMAND" || errors+=("deploy_command is required for deploy_mode=command")
    delivery_command_is_nonblank "$VERIFY_COMMAND" || errors+=("deploy_verify_command is required for deploy_mode=command")
    ;;
  ci)
    delivery_ci_profile_is_valid "$DEPLOY_CI_WORKFLOW" "$DEPLOY_ENVIRONMENT" || \
      errors+=("deploy_mode=ci requires a .github/workflows/*.yml deploy_ci_workflow and nonblank deploy_environment")
    delivery_command_is_nonblank "$VERIFY_COMMAND" || errors+=("deploy_verify_command is required for deploy_mode=ci")
    ;;
  not-applicable)
    delivery_command_is_nonblank "$NA_REASON" || \
      errors+=("deploy_not_applicable_reason is required and must be nonblank for deploy_mode=not-applicable")
    ;;
esac
if [ "$DEPLOY_MODE" != "not-applicable" ]; then
  command -v timeout >/dev/null 2>&1 || errors+=("timeout is required on PATH for deployment verification")
  if [ "$DEPLOY_MODE" = "command" ]; then
    delivery_timeout_is_bounded "$DEPLOY_TIMEOUT" || \
      errors+=("deploy_timeout must be a positive finite duration no greater than 1h")
  fi
  delivery_timeout_is_bounded "$VERIFY_TIMEOUT" || \
    errors+=("deploy_verify_timeout must be a positive finite duration no greater than 1h")
  if delivery_command_is_nonblank "$SMOKE_COMMAND"; then
    delivery_timeout_is_bounded "$SMOKE_TIMEOUT" || \
      errors+=("smoke_timeout must be a positive finite duration no greater than 1h")
  fi
fi
if [ "$DEPLOY_MODE" != "not-applicable" ] && ! delivery_command_is_nonblank "$SMOKE_COMMAND" && \
  [ "$ALLOW_NO_SMOKE" != "true" ]; then
  errors+=("smoke_command is required unless allow_no_smoke=true")
fi
if [ -n "$PRODUCTION_URL" ] && ! is_https_url "$PRODUCTION_URL"; then
  errors+=("production_url must be an https URL")
fi

if [ "$LAUNCHER_GITHUB_PREFLIGHT" != "github-v1" ]; then
  errors+=("missing durable authenticated launcher GitHub preflight evidence")
fi
command -v git >/dev/null 2>&1 || errors+=("git is required on PATH")
if command -v git >/dev/null 2>&1; then
  git -C "$DELIVERY_WORK_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1 || \
    errors+=("launcher work directory is not a git worktree: $DELIVERY_WORK_DIR")
fi

if [ "${#errors[@]}" -gt 0 ]; then
  printf 'complete-delivery preflight failed (%d issue(s)):\n' "${#errors[@]}" >&2
  printf '  - %s\n' "${errors[@]}" >&2
  delivery_fail "repair the rig formula_vars or named external prerequisite, then retry"
fi

echo "complete-delivery preflight passed: source=$SOURCE_BEAD_ID, $LOCAL_GATE_COUNT local gate(s), CI=$REQUIRED_CHECKS, CodeRabbit=$CODERABBIT, deploy=$DEPLOY_MODE"
