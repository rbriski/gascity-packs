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

  # Bash removes a backslash-newline pair before parsing words. Reject it
  # before normalizing or invoking `bash -lc`: otherwise a forbidden command
  # name can be split across physical lines and still run its side effects.
  if [[ "$command" == *$'\\\n'* || "$command" == *$'\\\r\n'* ]]; then
    delivery_fail "local gate command contains a Bash line continuation; a terminal remote approval gate must not execute: $command"
  fi

  # Command substitution can synthesize a forbidden executable only after
  # this pre-execution scan. Reject both Bash forms rather than attempting to
  # partially expand them, so no provider or trailing side effect can run.
  if [[ "$command" == *'$('* || "$command" == *'`'* ]]; then
    delivery_fail "local gate command contains command substitution; a terminal remote approval gate must not execute: $command"
  fi

  normalized="${normalized//\\/}"
  # Bash removes unescaped quote delimiters while joining adjacent fragments
  # into one word. Mirror that join before matching so `g"h` and
  # `delivery_"gate.py"` cannot become terminal commands only after this
  # pre-execution guard has passed.
  normalized="${normalized//\"/}"
  normalized="${normalized//\'/}"
  # Quotes and backticks delimit shell words too. They must not become part
  # of a path/executable word in the pre-execution matcher: otherwise a
  # quoted terminal command or command substitution reaches `bash -lc`.
  local token_boundary=$'[[:space:];,|&()<>{}\'"`]'
  local token_word=$'[^[:space:];,|&()<>{}\'"`]'
  local forbidden_command_pattern="(^|$token_boundary)($token_word*/)?(delivery_gate\\.py|delivery-pr-approved\\.sh|coderabbit|remote-approval$token_word*|remote_approval$token_word*|approval-gate$token_word*|approval_gate$token_word*)($token_boundary|$)"
  local gh_command_pattern="(^|$token_boundary)($token_word*/)?gh($token_boundary|$)"

  # Local gates run before publication and must never decide the terminal PR
  # state. Inspect the complete configured command before handing it to
  # `bash -lc`, so a compound command cannot run a side effect first. The
  # resolver also inspects repository-specific wrappers that cannot be named
  # centrally; this policy enforces the canonical provider/approval boundary.
  # Normalize Bash's simple backslash escapes so a forbidden executable name
  # cannot cross that boundary under a shell-escaped spelling.
  # Repository gate configuration is trusted policy, not a shell sandbox.
  case "$normalized" in
    *api.github.com*)
      delivery_fail "local gate command invokes a terminal remote approval gate or provider command: $command"
      ;;
  esac

  if [[ "$normalized" =~ $forbidden_command_pattern || "$normalized" =~ $gh_command_pattern ]]; then
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
