#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=delivery-common.sh
source "$SCRIPT_DIR/delivery-common.sh"
delivery_initialize_context

cd "$DELIVERY_WORK_DIR"

ALLOW_NONE="$(delivery_var allow_no_local_gates false)"
RAN=0

reject_terminal_approval_command() {
  local command="$1"
  local normalized="${command,,}"
  normalized="${normalized//\\/}"
  local gh_command_pattern='(^|[[:space:];|&()])([^[:space:];|&()]*/)?gh([[:space:];|&()]|$)'

  # Local gates run before publication and must never decide the terminal PR
  # state. Inspect the complete configured command before handing it to
  # `bash -lc`, so a compound command cannot run a side effect first. The
  # resolver also inspects repository-specific wrappers that cannot be named
  # centrally; this policy enforces the canonical provider/approval boundary.
  # Normalize Bash's simple backslash escapes so a forbidden executable name
  # cannot cross that boundary under a shell-escaped spelling.
  # Repository gate configuration is trusted policy, not a shell sandbox.
  case "$normalized" in
    *delivery_gate.py*|*delivery-pr-approved.sh*|*coderabbit*|*api.github.com*|*remote-approval*|*remote_approval*|*approval-gate*|*approval_gate*)
      delivery_fail "local gate command invokes a terminal remote approval gate or provider command: $command"
      ;;
  esac

  if [[ "$normalized" =~ $gh_command_pattern ]]; then
    delivery_fail "local gate command invokes a terminal remote approval gate or provider command: $command"
  fi
}

run_gate() {
  local label="$1"
  local key="$2"
  local command
  command="$(delivery_var "$key" "")"
  [ -n "$command" ] || return 0
  reject_terminal_approval_command "$command"
  RAN=$((RAN + 1))
  echo "complete-delivery local gate [$label]: $command"
  if ! bash -lc "$command"; then
    delivery_fail "$label command failed: $command"
  fi
}

run_gate setup setup_command
run_gate lint lint_command
run_gate typecheck typecheck_command
run_gate test test_command
run_gate build build_command
run_gate browser browser_test_command
run_gate security security_command
run_gate extra extra_gate_command

if [ "$RAN" -eq 0 ] && [ "$ALLOW_NONE" != "true" ]; then
  delivery_fail "no local quality commands are configured; set rig formula_vars or explicitly set allow_no_local_gates=true"
fi

echo "complete-delivery local gates passed ($RAN command(s))"
