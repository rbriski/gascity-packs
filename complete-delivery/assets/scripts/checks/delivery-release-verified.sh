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

delivery_command_is_nonblank() {
  [[ "$1" =~ [^[:space:]] ]]
}

delivery_resolve_artifact_delivery_evidence() {
  local value="$1"
  local label="$2"
  local artifact_root delivery_dir resolved

  artifact_root="$(delivery_var artifact_root '')"
  [ -n "$artifact_root" ] || delivery_fail "gc.var.artifact_root is required for $label"
  artifact_root="$(delivery_resolve_contained_path "$artifact_root" "artifact_root")"
  delivery_dir="$(delivery_resolve_contained_path "$artifact_root/delivery" "artifact delivery directory")"
  resolved="$(delivery_resolve_contained_path "$value" "$label")"
  python3 - "$delivery_dir" "$resolved" <<'PY'
import sys
from pathlib import Path

delivery_dir = Path(sys.argv[1]).resolve(strict=True)
candidate = Path(sys.argv[2]).resolve(strict=False)
try:
    candidate.relative_to(delivery_dir)
except ValueError:
    raise SystemExit(1)
print(candidate)
PY
}

delivery_command_label() {
  python3 - "$1" <<'PY'
import hashlib
import sys

print(f"sha256:{hashlib.sha256(sys.argv[1].encode()).hexdigest()}")
PY
}

delivery_value_sha256() {
  python3 - "$1" <<'PY'
import hashlib
import sys

print(hashlib.sha256(sys.argv[1].encode()).hexdigest())
PY
}

delivery_run_bounded_command() {
  local name="$1"
  local command="$2"
  local timeout_value="$3"
  local status outcome label status_dir status_marker wrapper_status
  local evidence_dir stdout_path stderr_path

  label="$(delivery_command_label "$command")"
  status_dir="$(mktemp -d "${TMPDIR:-/tmp}/delivery-command-status.XXXXXX")" || \
    delivery_fail "failed to create $name status directory"
  status_marker="$status_dir/status"
  evidence_dir="$(dirname "$VERIFY_EVIDENCE")"
  [ -d "$evidence_dir" ] || { rm -rf -- "$status_dir"; delivery_fail "verification evidence directory is unavailable"; }
  stdout_path="$(mktemp "$evidence_dir/$name.stdout.log.XXXXXX")" || \
    { rm -rf -- "$status_dir"; delivery_fail "failed to create $name stdout capture"; }
  stderr_path="$(mktemp "$evidence_dir/$name.stderr.log.XXXXXX")" || \
    { rm -f -- "$stdout_path"; rm -rf -- "$status_dir"; delivery_fail "failed to create $name stderr capture"; }

  # GNU timeout uses 124, 125, and 137 for its own outcomes, but a managed
  # command may legitimately return any of those statuses.  The inner Bash
  # writes a marker only after the strict managed command has returned; the
  # outer wrapper can therefore distinguish its own result from the child's.
  if timeout --kill-after=5s "$timeout_value" bash -u -o pipefail -c '
    set +e
    bash -euo pipefail -c "$1"
    child_status=$?
    if ! printf "%s\\n" "$child_status" >"$2/status.tmp"; then
      exit 125
    fi
    if ! mv "$2/status.tmp" "$2/status"; then
      exit 125
    fi
    exit "$child_status"
  ' delivery-bounded-command "$command" "$status_dir" >"$stdout_path" 2>"$stderr_path"; then
    wrapper_status=0
  else
    wrapper_status=$?
  fi

  if [ -f "$status_marker" ]; then
    status="$(<"$status_marker")"
    if [ "$status" -eq 0 ]; then
      outcome=passed
    else
      outcome=command_failure
    fi
  else
    status="$wrapper_status"
    case "$wrapper_status" in
      124|137) outcome=timeout ;;
      *) outcome=timeout_utility_failure ;;
    esac
  fi
  printf 'command=%s label=%s timeout=%s outcome=%s status=%s stdout_path=%s stderr_path=%s\n' \
    "$name" "$label" "$timeout_value" "$outcome" "$status" \
    "$stdout_path" "$stderr_path" >>"$VERIFY_EVIDENCE" || \
    { rm -f -- "$stdout_path" "$stderr_path"; rm -rf -- "$status_dir"; delivery_fail "failed to record $name verification evidence"; }
  rm -rf -- "$status_dir" || delivery_fail "failed to clean $name status directory"
  [ "$status" -eq 0 ]
}

delivery_run_deploy_command() {
  local artifact_root delivery_dir evidence_path evidence_tmp stdout_path stderr_path stdout_tmp stderr_tmp
  local status_dir status_marker command_label child_status wrapper_status outcome deploy_status
  local previous_status previous_sha

  MERGE_SHA="$(delivery_root_metadata delivery.merge_sha)"
  DEPLOY_COMMAND="$(delivery_var deploy_command '')"
  DEPLOY_TIMEOUT="$(delivery_var deploy_timeout 5m)"
  DELIVERY_REPO="$(delivery_root_metadata delivery.repo)"
  DELIVERY_PR="$(delivery_root_metadata delivery.pr_number)"
  artifact_root="$(delivery_var artifact_root '')"

  [ -n "$MERGE_SHA" ] || delivery_fail "delivery.merge_sha is missing"
  [[ "$MERGE_SHA" =~ ^[0-9a-f]{40}$ ]] || \
    delivery_fail "delivery.merge_sha is not a full lowercase Git SHA"
  delivery_command_is_nonblank "$DEPLOY_COMMAND" || \
    delivery_fail "deploy_command is required for deploy_mode=command"
  delivery_timeout_is_bounded "$DEPLOY_TIMEOUT" || \
    delivery_fail "deploy_timeout must be a positive finite duration no greater than 1h"
  [ -n "$DELIVERY_REPO" ] || delivery_fail "delivery.repo is missing"
  [ -n "$DELIVERY_PR" ] || delivery_fail "delivery.pr_number is missing"
  [ -n "$artifact_root" ] || delivery_fail "gc.var.artifact_root is missing"
  command -v timeout >/dev/null 2>&1 || delivery_fail "timeout is required on PATH"

  previous_status="$(delivery_root_metadata delivery.deploy_status)"
  previous_sha="$(delivery_root_metadata delivery.deploy_merge_sha)"
  if [ "$previous_sha" = "$MERGE_SHA" ] && [ -n "$previous_status" ]; then
    delivery_fail "deploy_command already ran for $MERGE_SHA; exact-once deployment forbids a rerun"
  fi

  artifact_root="$(delivery_resolve_contained_path "$artifact_root" "artifact_root")"
  mkdir -p "$artifact_root/delivery" || delivery_fail "failed to create deployment evidence directory"
  delivery_dir="$(delivery_resolve_contained_path "$artifact_root/delivery" "deployment evidence directory")"
  evidence_path="$delivery_dir/deploy.log"
  stdout_path="$delivery_dir/deploy.stdout.log"
  stderr_path="$delivery_dir/deploy.stderr.log"
  for final_capture in "$evidence_path" "$stdout_path" "$stderr_path"; do
    [ ! -e "$final_capture" ] && [ ! -L "$final_capture" ] || \
      delivery_fail "deploy evidence or capture already exists; exact-once deployment forbids a rerun"
  done

  # Record the same-SHA execution guard before allocating captures or running
  # the side-effecting command.  A crash after any later publication leaves
  # this fail-closed state in place, so a retry cannot deploy twice.
  gc bd update "$DELIVERY_ROOT_ID" \
    --set-metadata "delivery.deploy_merge_sha=$MERGE_SHA" \
    --set-metadata "delivery.deploy_status=started" || \
    delivery_fail "failed to atomically record deployment execution-started guard"

  evidence_tmp="$(mktemp "$delivery_dir/deploy.log.tmp.XXXXXX")" || \
    delivery_fail "failed to create deployment evidence file"
  stdout_tmp=""
  stderr_tmp=""
  status_dir=""
  trap 'rm -f -- "$evidence_tmp" "$stdout_tmp" "$stderr_tmp"; rm -rf -- "$status_dir"' EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
  stdout_tmp="$(mktemp "$delivery_dir/deploy.stdout.log.tmp.XXXXXX")" || \
    { rm -f "$evidence_tmp"; delivery_fail "failed to create deployment stdout capture"; }
  stderr_tmp="$(mktemp "$delivery_dir/deploy.stderr.log.tmp.XXXXXX")" || \
    { rm -f "$evidence_tmp" "$stdout_tmp"; delivery_fail "failed to create deployment stderr capture"; }
  status_dir="$(mktemp -d "${TMPDIR:-/tmp}/delivery-deploy-status.XXXXXX")" || \
    { rm -f "$evidence_tmp" "$stdout_tmp" "$stderr_tmp"; delivery_fail "failed to create deployment status directory"; }
  status_marker="$status_dir/status"
  command_label="$(delivery_command_label "$DEPLOY_COMMAND")"

  export DELIVERY_SHA="$MERGE_SHA" DELIVERY_REPO DELIVERY_PR
  if timeout --kill-after=5s "$DEPLOY_TIMEOUT" bash -u -o pipefail -c '
    set +e
    bash -euo pipefail -c "$1"
    child_status=$?
    if ! printf "%s\\n" "$child_status" >"$2/status.tmp"; then
      exit 125
    fi
    if ! mv "$2/status.tmp" "$2/status"; then
      exit 125
    fi
    exit "$child_status"
  ' delivery-deploy-command "$DEPLOY_COMMAND" "$status_dir" >"$stdout_tmp" 2>"$stderr_tmp"; then
    wrapper_status=0
  else
    wrapper_status=$?
  fi

  if [ -f "$status_marker" ]; then
    child_status="$(<"$status_marker")"
    if [ "$child_status" -eq 0 ]; then
      outcome=passed
      deploy_status=deployed
    else
      outcome=command_failure
      deploy_status=failed
    fi
  else
    child_status=unavailable
    case "$wrapper_status" in
      124|137) outcome=timeout ;;
      *) outcome=timeout_utility_failure ;;
    esac
    deploy_status=failed
  fi

  if ! mv "$stdout_tmp" "$stdout_path" || ! mv "$stderr_tmp" "$stderr_path"; then
    rm -f -- "$evidence_tmp" "$stdout_tmp" "$stderr_tmp" \
      "$evidence_path" "$stdout_path" "$stderr_path"
    rm -rf -- "$status_dir"
    delivery_fail "failed to publish deployment command captures"
  fi
  printf '%s\n' \
    'schema=complete-delivery.deploy.v1' \
    "command_label=$command_label" \
    "timeout=$DEPLOY_TIMEOUT" \
    "outcome=$outcome" \
    "child_status=$child_status" \
    "wrapper_status=$wrapper_status" \
    "merge_sha=$MERGE_SHA" \
    "stdout_path=$stdout_path" \
    "stderr_path=$stderr_path" >"$evidence_tmp" || {
      rm -f -- "$evidence_tmp" "$stdout_tmp" "$stderr_tmp" \
        "$evidence_path" "$stdout_path" "$stderr_path"
      rm -rf -- "$status_dir"
      delivery_fail "failed to write deployment evidence"
    }
  mv "$evidence_tmp" "$evidence_path" || {
    rm -f -- "$evidence_tmp" "$stdout_tmp" "$stderr_tmp" \
      "$evidence_path" "$stdout_path" "$stderr_path"
    rm -rf -- "$status_dir"
    delivery_fail "failed to publish deployment evidence"
  }
  rm -rf -- "$status_dir" || delivery_fail "failed to clean deployment status directory"

  gc bd update "$DELIVERY_ROOT_ID" \
    --set-metadata "delivery.deploy_evidence_path=$evidence_path" \
    --set-metadata "delivery.deploy_command_label=$command_label" \
    --set-metadata "delivery.deploy_timeout=$DEPLOY_TIMEOUT" \
    --set-metadata "delivery.deploy_outcome=$outcome" \
    --set-metadata "delivery.deploy_child_status=$child_status" \
    --set-metadata "delivery.deploy_wrapper_status=$wrapper_status" \
    --set-metadata "delivery.deploy_merge_sha=$MERGE_SHA" \
    --set-metadata "delivery.deploy_stdout_path=$stdout_path" \
    --set-metadata "delivery.deploy_stderr_path=$stderr_path" \
    --set-metadata "delivery.deploy_status=$deploy_status" || {
      rm -f -- "$evidence_path" "$stdout_path" "$stderr_path"
      delivery_fail "failed to atomically record deployment evidence metadata"
    }
  trap - EXIT HUP INT TERM

  [ "$outcome" = passed ] && [ "$child_status" = 0 ] && [ "$wrapper_status" -eq 0 ] || \
    delivery_fail "deploy_command failed; see deployment evidence"
  echo "complete-delivery deploy command passed at $MERGE_SHA"
}

delivery_validate_command_deploy_evidence() {
  local evidence_path expected_label expected_timeout expected_sha expected_stdout expected_stderr
  evidence_path="$1"
  expected_label="$2"
  expected_timeout="$3"
  expected_sha="$4"
  expected_stdout="$5"
  expected_stderr="$6"
  python3 - "$evidence_path" "$expected_label" "$expected_timeout" "$expected_sha" "$expected_stdout" "$expected_stderr" <<'PY'
import sys

path, label, timeout, merge_sha, stdout_path, stderr_path = sys.argv[1:]
try:
    with open(path, encoding="utf-8") as handle:
        lines = [line.rstrip("\n") for line in handle]
except OSError as exc:
    raise SystemExit(f"cannot read deploy evidence: {exc}")

fields = {}
for line in lines:
    if "=" not in line:
        raise SystemExit("deploy evidence has a malformed line")
    key, value = line.split("=", 1)
    if not key or key in fields:
        raise SystemExit("deploy evidence has missing or duplicate fields")
    fields[key] = value

expected = {
    "schema": "complete-delivery.deploy.v1",
    "command_label": label,
    "timeout": timeout,
    "outcome": "passed",
    "child_status": "0",
    "wrapper_status": "0",
    "merge_sha": merge_sha,
    "stdout_path": stdout_path,
    "stderr_path": stderr_path,
}
if fields != expected:
    raise SystemExit("deploy evidence does not bind the current command, timeout, and merge SHA")
for capture_path in (stdout_path, stderr_path):
    try:
        with open(capture_path, "rb"):
            pass
    except OSError as exc:
        raise SystemExit(f"deploy evidence capture is missing: {exc}")
PY
}

delivery_ci_evidence_field() {
  python3 - "$1" "$2" <<'PY'
import sys

path, wanted = sys.argv[1:]
fields = {}
with open(path, encoding="utf-8") as handle:
    for raw_line in handle:
        line = raw_line.rstrip("\n")
        if "=" not in line:
            raise SystemExit(1)
        key, value = line.split("=", 1)
        if not key or key in fields:
            raise SystemExit(1)
        fields[key] = value
if wanted not in fields:
    raise SystemExit(1)
print(fields[wanted])
PY
}

delivery_collect_ci_deploy_evidence() {
  local output_path="$1"
  local ci_repo ci_pr ci_sha ci_run_id ci_workflow ci_environment ci_base
  local api_dir deployment_id

  ci_repo="$(delivery_root_metadata delivery.repo)"
  ci_pr="$(delivery_root_metadata delivery.pr_number)"
  ci_sha="$(delivery_root_metadata delivery.merge_sha)"
  ci_run_id="$(delivery_root_metadata delivery.deploy_run_id)"
  ci_workflow="$(delivery_var deploy_ci_workflow '')"
  ci_environment="$(delivery_var deploy_environment '')"
  ci_base="$(delivery_var base_branch main)"

  if ! python3 - "$ci_repo" "$ci_pr" "$ci_sha" "$ci_run_id" "$ci_workflow" "$ci_environment" "$ci_base" <<'PY'
import re
import sys

repo, pr, sha, run_id, workflow, environment, base = sys.argv[1:]
valid = (
    re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
    and re.fullmatch(r"[1-9][0-9]*", pr)
    and re.fullmatch(r"[0-9a-f]{40}", sha)
    and re.fullmatch(r"[1-9][0-9]*", run_id)
    and re.fullmatch(r"\.github/workflows/[A-Za-z0-9_./-]+\.ya?ml", workflow)
    and ".." not in workflow.split("/")
    and environment.strip() == environment
    and environment
    and len(environment) <= 255
    and not any(character in environment for character in "\r\n\t")
    and base.strip() == base
    and base
    and not any(character in base for character in "\r\n\t")
)
raise SystemExit(0 if valid else 1)
PY
  then
    delivery_fail "CI deployment requires valid delivery.repo, PR, merge SHA, delivery.deploy_run_id, deploy_ci_workflow, deploy_environment, and base_branch"
  fi
  command -v gh >/dev/null 2>&1 || delivery_fail "gh is required on PATH for deploy_mode=ci"

  api_dir="$(mktemp -d "${TMPDIR:-/tmp}/delivery-ci-api.XXXXXX")" || \
    delivery_fail "failed to create CI deployment API evidence directory"
  if ! gh api "repos/$ci_repo/actions/runs/$ci_run_id" >"$api_dir/run.json"; then
    rm -rf -- "$api_dir"
    delivery_fail "failed to query GitHub Actions run $ci_run_id"
  fi
  if ! gh api "repos/$ci_repo/pulls/$ci_pr" >"$api_dir/pr.json"; then
    rm -rf -- "$api_dir"
    delivery_fail "failed to query merged PR $ci_pr for CI deployment evidence"
  fi
  if ! gh api --paginate --slurp -X GET "repos/$ci_repo/deployments" \
    -f "sha=$ci_sha" -f "environment=$ci_environment" -f per_page=100 \
    >"$api_dir/deployments.json"; then
    rm -rf -- "$api_dir"
    delivery_fail "failed to query GitHub deployments for the merge SHA and environment"
  fi

  if ! python3 - "$api_dir/run.json" "$api_dir/pr.json" "$api_dir/deployments.json" \
    "$api_dir/selection.json" "$ci_repo" "$ci_pr" "$ci_sha" "$ci_run_id" \
    "$ci_workflow" "$ci_environment" "$ci_base" <<'PY'
from datetime import datetime
import json
import sys

(
    run_path, pr_path, deployments_path, selection_path, repo, pr_number,
    merge_sha, run_id, workflow, environment, base_branch,
) = sys.argv[1:]


def read_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def paginated_items(value, label):
    if not isinstance(value, list):
        raise SystemExit(f"{label} API returned an unexpected shape")
    if all(isinstance(item, dict) for item in value):
        return value
    if all(isinstance(page, list) for page in value):
        flattened = []
        for page in value:
            if not all(isinstance(item, dict) for item in page):
                raise SystemExit(f"{label} API returned an unexpected shape")
            flattened.extend(page)
        return flattened
    raise SystemExit(f"{label} API returned an unexpected shape")


def timestamp(value, label):
    if not isinstance(value, str) or not value:
        raise SystemExit(f"{label} timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SystemExit(f"{label} timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise SystemExit(f"{label} timestamp has no timezone")
    return parsed


run = read_json(run_path)
pull = read_json(pr_path)
deployments = read_json(deployments_path)
if not isinstance(run, dict) or not isinstance(pull, dict):
    raise SystemExit("GitHub deployment API returned an unexpected shape")
deployments = paginated_items(deployments, "GitHub deployment")
if str(run.get("id", "")) != run_id:
    raise SystemExit("GitHub Actions run ID does not match delivery.deploy_run_id")
if (run.get("repository") or {}).get("full_name") != repo:
    raise SystemExit("GitHub Actions run repository does not match delivery.repo")
if run.get("head_sha") != merge_sha:
    raise SystemExit("GitHub Actions run head SHA does not match delivery.merge_sha")
if run.get("head_branch") != base_branch:
    raise SystemExit("GitHub Actions run branch does not match base_branch")
workflow_run_path = run.get("path")
allowed_workflow_run_paths = {
    workflow,
    f"{workflow}@{base_branch}",
    f"{workflow}@refs/heads/{base_branch}",
}
if workflow_run_path not in allowed_workflow_run_paths:
    raise SystemExit("GitHub Actions workflow path or ref does not match deploy_ci_workflow and base_branch")
if run.get("status") != "completed" or run.get("conclusion") != "success":
    raise SystemExit("GitHub Actions deployment run is not completed successfully")
workflow_id = run.get("workflow_id")
if not isinstance(workflow_id, int) or workflow_id <= 0:
    raise SystemExit("GitHub Actions deployment run has no workflow ID")
expected_run_url = f"https://github.com/{repo}/actions/runs/{run_id}"
if run.get("html_url") != expected_run_url:
    raise SystemExit("GitHub Actions run URL is not the canonical repository run URL")

if str(pull.get("number", "")) != pr_number:
    raise SystemExit("GitHub PR number does not match delivery.pr_number")
if (pull.get("base") or {}).get("ref") != base_branch:
    raise SystemExit("GitHub PR base does not match base_branch")
if ((pull.get("base") or {}).get("repo") or {}).get("full_name") != repo:
    raise SystemExit("GitHub PR repository does not match delivery.repo")
if pull.get("merge_commit_sha") != merge_sha or not pull.get("merged_at"):
    raise SystemExit("GitHub PR is not merged at delivery.merge_sha")

merged_at = timestamp(pull["merged_at"], "PR merge")
run_created_at = timestamp(run.get("created_at"), "workflow run")
if run_created_at < merged_at:
    raise SystemExit("GitHub Actions deployment run predates the PR merge")

candidates = []
for deployment in deployments:
    if not isinstance(deployment, dict):
        continue
    if deployment.get("sha") != merge_sha or deployment.get("environment") != environment:
        continue
    deployment_id = deployment.get("id")
    if not isinstance(deployment_id, int) or deployment_id <= 0:
        continue
    created_at = timestamp(deployment.get("created_at"), "deployment")
    if created_at < run_created_at:
        continue
    candidates.append((created_at, deployment_id, deployment))
if not candidates:
    raise SystemExit("no GitHub deployment binds the successful run to the merge SHA and environment")
_, _, deployment = min(candidates, key=lambda item: (item[0], item[1]))
with open(selection_path, "w", encoding="utf-8") as handle:
    json.dump({"run": run, "pull": pull, "deployment": deployment}, handle, sort_keys=True)
PY
  then
    rm -rf -- "$api_dir"
    delivery_fail "GitHub CI deployment run is failed, stale, or does not bind the merge SHA and environment"
  fi

  deployment_id="$(python3 - "$api_dir/selection.json" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as handle:
    print(json.load(handle)["deployment"]["id"])
PY
)"
  if ! gh api --paginate --slurp "repos/$ci_repo/deployments/$deployment_id/statuses?per_page=100" \
    >"$api_dir/statuses.json"; then
    rm -rf -- "$api_dir"
    delivery_fail "failed to query GitHub deployment statuses"
  fi

  if ! python3 - "$api_dir/selection.json" "$api_dir/statuses.json" "$output_path" \
    "$ci_repo" "$ci_pr" "$ci_sha" "$ci_run_id" "$ci_workflow" \
    "$ci_environment" "$ci_base" <<'PY'
from datetime import datetime
import json
import os
import sys

(
    selection_path, statuses_path, output_path, repo, pr_number, merge_sha,
    run_id, workflow, environment, base_branch,
) = sys.argv[1:]


def timestamp(value, label):
    if not isinstance(value, str) or not value:
        raise SystemExit(f"{label} timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SystemExit(f"{label} timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise SystemExit(f"{label} timestamp has no timezone")
    return parsed


def paginated_items(value, label):
    if not isinstance(value, list):
        raise SystemExit(f"{label} API returned an unexpected shape")
    if all(isinstance(item, dict) for item in value):
        return value
    if all(isinstance(page, list) for page in value):
        flattened = []
        for page in value:
            if not all(isinstance(item, dict) for item in page):
                raise SystemExit(f"{label} API returned an unexpected shape")
            flattened.extend(page)
        return flattened
    raise SystemExit(f"{label} API returned an unexpected shape")


with open(selection_path, encoding="utf-8") as handle:
    selection = json.load(handle)
with open(statuses_path, encoding="utf-8") as handle:
    statuses = json.load(handle)
run = selection["run"]
deployment = selection["deployment"]
if not isinstance(statuses, list):
    raise SystemExit("GitHub deployment statuses have an unexpected shape")
statuses = paginated_items(statuses, "GitHub deployment statuses")
deployment_created = timestamp(deployment.get("created_at"), "deployment")
successful = []
for status in statuses:
    if not isinstance(status, dict) or status.get("state") != "success":
        continue
    status_environment = status.get("environment")
    if status_environment not in (None, "", environment):
        continue
    status_id = status.get("id")
    if not isinstance(status_id, int) or status_id <= 0:
        continue
    created_at = timestamp(status.get("created_at"), "deployment status")
    if created_at < deployment_created:
        continue
    successful.append((created_at, status_id, status))
if not successful:
    raise SystemExit("GitHub deployment has no successful status for the configured environment")
_, _, status = min(successful, key=lambda item: (item[0], item[1]))
# A deployment status is read only through the selected deployment endpoint,
# which is already bound above to the exact repository, merge SHA, and
# environment. GitHub documents log_url as optional and deployment-specific.
log_url = status.get("log_url", "")
if log_url is None:
    log_url = ""
if not isinstance(log_url, str):
    raise SystemExit("GitHub deployment status log URL has an unexpected shape")

fields = [
    ("schema", "complete-delivery.ci-deploy.v1"),
    ("repository", repo),
    ("pr_number", pr_number),
    ("merge_sha", merge_sha),
    ("base_branch", base_branch),
    ("workflow_id", str(run["workflow_id"])),
    ("workflow_path", workflow),
    ("workflow_run_path", run["path"]),
    ("run_id", run_id),
    ("run_url", run["html_url"]),
    ("run_status", "completed"),
    ("run_conclusion", "success"),
    ("run_created_at", run["created_at"]),
    ("environment", environment),
    ("deployment_id", str(deployment["id"])),
    ("deployment_created_at", deployment["created_at"]),
    ("deployment_status_id", str(status["id"])),
    ("deployment_status", "success"),
    ("deployment_log_url", log_url),
    ("deployment_status_created_at", status["created_at"]),
]
if any("\n" in value or "\r" in value for _, value in fields):
    raise SystemExit("CI deployment evidence contains a multiline value")
with open(output_path, "w", encoding="utf-8") as handle:
    for key, value in fields:
        handle.write(f"{key}={value}\n")
PY
  then
    rm -rf -- "$api_dir"
    delivery_fail "GitHub deployment has no successful status bound to the configured environment"
  fi
  rm -rf -- "$api_dir" || delivery_fail "failed to clean CI deployment API evidence"
}

delivery_run_ci_deploy_check() {
  local artifact_root delivery_dir evidence_path evidence_tmp
  local workflow_id workflow_run_path run_id run_url environment deployment_id deployment_status_id

  artifact_root="$(delivery_var artifact_root '')"
  [ -n "$artifact_root" ] || delivery_fail "gc.var.artifact_root is missing"
  artifact_root="$(delivery_resolve_contained_path "$artifact_root" "artifact_root")"
  mkdir -p "$artifact_root/delivery" || delivery_fail "failed to create deployment evidence directory"
  delivery_dir="$(delivery_resolve_contained_path "$artifact_root/delivery" "deployment evidence directory")"
  evidence_path="$delivery_dir/deploy.log"
  evidence_tmp="$(mktemp "$delivery_dir/deploy.log.tmp.XXXXXX")" || \
    delivery_fail "failed to create CI deployment evidence file"
  trap 'rm -f -- "$evidence_tmp"' EXIT
  if ! delivery_collect_ci_deploy_evidence "$evidence_tmp"; then
    rm -f "$evidence_tmp"
    delivery_fail "failed to collect CI deployment evidence"
  fi
  mv "$evidence_tmp" "$evidence_path" || {
    rm -f "$evidence_tmp"
    delivery_fail "failed to atomically publish CI deployment evidence"
  }
  trap - EXIT

  workflow_id="$(delivery_ci_evidence_field "$evidence_path" workflow_id)" || \
    delivery_fail "CI deployment evidence has no workflow ID"
  workflow_run_path="$(delivery_ci_evidence_field "$evidence_path" workflow_run_path)" || \
    delivery_fail "CI deployment evidence has no workflow run path"
  run_id="$(delivery_ci_evidence_field "$evidence_path" run_id)" || \
    delivery_fail "CI deployment evidence has no run ID"
  run_url="$(delivery_ci_evidence_field "$evidence_path" run_url)" || \
    delivery_fail "CI deployment evidence has no run URL"
  environment="$(delivery_ci_evidence_field "$evidence_path" environment)" || \
    delivery_fail "CI deployment evidence has no environment"
  deployment_id="$(delivery_ci_evidence_field "$evidence_path" deployment_id)" || \
    delivery_fail "CI deployment evidence has no deployment ID"
  deployment_status_id="$(delivery_ci_evidence_field "$evidence_path" deployment_status_id)" || \
    delivery_fail "CI deployment evidence has no deployment status ID"

  gc bd update "$DELIVERY_ROOT_ID" \
    --set-metadata "delivery.deploy_evidence_path=$evidence_path" \
    --set-metadata "delivery.deploy_status=deployed" \
    --set-metadata "delivery.deploy_run_id=$run_id" \
    --set-metadata "delivery.deploy_run_url=$run_url" \
    --set-metadata "delivery.deploy_workflow_id=$workflow_id" \
    --set-metadata "delivery.deploy_workflow=$(delivery_var deploy_ci_workflow '')" \
    --set-metadata "delivery.deploy_workflow_run_path=$workflow_run_path" \
    --set-metadata "delivery.deploy_environment=$environment" \
    --set-metadata "delivery.deploy_merge_sha=$(delivery_root_metadata delivery.merge_sha)" \
    --set-metadata "delivery.deploy_conclusion=success" \
    --set-metadata "delivery.deploy_deployment_id=$deployment_id" \
    --set-metadata "delivery.deploy_deployment_status_id=$deployment_status_id" || \
    delivery_fail "failed to atomically record CI deployment evidence metadata"
  echo "complete-delivery CI deploy passed at $(delivery_root_metadata delivery.merge_sha)"
}

delivery_validate_ci_deploy_evidence() {
  local evidence_path="$1"
  local current_tmp evidence_dir

  [ "$(delivery_root_metadata delivery.deploy_merge_sha)" = "$MERGE_SHA" ] || \
    delivery_fail "CI deploy evidence merge SHA does not match delivery.merge_sha"
  [ "$(delivery_root_metadata delivery.deploy_workflow)" = "$(delivery_var deploy_ci_workflow '')" ] || \
    delivery_fail "CI deploy evidence workflow does not match deploy_ci_workflow"
  [ "$(delivery_root_metadata delivery.deploy_environment)" = "$(delivery_var deploy_environment '')" ] || \
    delivery_fail "CI deploy evidence environment does not match deploy_environment"
  [ "$(delivery_root_metadata delivery.deploy_conclusion)" = success ] || \
    delivery_fail "CI deploy evidence conclusion must be success"

  evidence_dir="$(dirname "$evidence_path")"
  current_tmp="$(mktemp "$evidence_dir/ci-deploy-current.tmp.XXXXXX")" || \
    delivery_fail "failed to create current CI deployment evidence file"
  trap 'rm -f -- "$current_tmp"' EXIT
  if ! delivery_collect_ci_deploy_evidence "$current_tmp"; then
    rm -f "$current_tmp"
    delivery_fail "failed to re-query current CI deployment evidence"
  fi
  if ! cmp -s "$evidence_path" "$current_tmp"; then
    rm -f "$current_tmp"
    delivery_fail "CI deployment evidence is forged, stale, or incomplete"
  fi
  rm -f "$current_tmp" || delivery_fail "failed to clean current CI deployment evidence"
  trap - EXIT

  [ "$(delivery_root_metadata delivery.deploy_workflow_id)" = "$(delivery_ci_evidence_field "$evidence_path" workflow_id)" ] || \
    delivery_fail "CI deploy workflow ID metadata does not match evidence"
  [ "$(delivery_root_metadata delivery.deploy_workflow_run_path)" = "$(delivery_ci_evidence_field "$evidence_path" workflow_run_path)" ] || \
    delivery_fail "CI deploy workflow run path metadata does not match evidence"
  [ "$(delivery_root_metadata delivery.deploy_run_url)" = "$(delivery_ci_evidence_field "$evidence_path" run_url)" ] || \
    delivery_fail "CI deploy run URL metadata does not match evidence"
  [ "$(delivery_root_metadata delivery.deploy_deployment_id)" = "$(delivery_ci_evidence_field "$evidence_path" deployment_id)" ] || \
    delivery_fail "CI deployment ID metadata does not match evidence"
  [ "$(delivery_root_metadata delivery.deploy_deployment_status_id)" = "$(delivery_ci_evidence_field "$evidence_path" deployment_status_id)" ] || \
    delivery_fail "CI deployment status ID metadata does not match evidence"
}

MERGE_SHA="$(delivery_root_metadata delivery.merge_sha)"
DEPLOYED_SHA="$(delivery_root_metadata delivery.deployed_sha)"
DEPLOY_STATUS="$(delivery_root_metadata delivery.deploy_status)"
DEPLOY_EVIDENCE="$(delivery_root_metadata delivery.deploy_evidence_path)"
VERIFY_EVIDENCE="$(delivery_root_metadata delivery.verify_evidence_path)"
DEPLOY_MODE="$(delivery_var deploy_mode command)"
DEPLOY_COMMAND="$(delivery_var deploy_command "")"
DEPLOY_TIMEOUT="$(delivery_var deploy_timeout 5m)"
NA_REASON="$(delivery_var deploy_not_applicable_reason "")"
ALLOW_NO_SMOKE="$(delivery_var allow_no_smoke false)"
NO_SMOKE_REASON="$(delivery_var no_smoke_reason '')"

# This checker is shared by the deploy and production-verification graph
# steps. Empty metadata retains the formula's declared command default, while
# an unknown value must fail before either step can report success.
case "$DEPLOY_MODE" in
  "") DEPLOY_MODE="command" ;;
  command|ci|not-applicable) ;;
  *) delivery_fail "deploy_mode must be command, ci, or not-applicable" ;;
esac

STEP_REF="$(delivery_metadata_value "$DELIVERY_STEP_JSON" "gc.step_ref")"
case "$STEP_REF" in
  complete-delivery.deploy)
    case "$DEPLOY_MODE" in
      command) delivery_run_deploy_command ;;
      ci) delivery_run_ci_deploy_check ;;
      not-applicable)
        echo "complete-delivery deploy check requires agent-provided evidence for deploy_mode=not-applicable"
        ;;
    esac
    exit 0
    ;;
  complete-delivery.verify-production) ;;
  "") delivery_fail "gc.step_ref is required for deployment lifecycle checks" ;;
  *) delivery_fail "unexpected deployment lifecycle gc.step_ref: $STEP_REF" ;;
esac

if [ "$ALLOW_NO_SMOKE" = "true" ]; then
  [[ "$NO_SMOKE_REASON" =~ [^[:space:]] ]] || \
    delivery_fail "no_smoke_reason is required and must be nonblank when allow_no_smoke=true"
  RECORDED_NO_SMOKE_REASON="$(delivery_root_metadata delivery.no_smoke_reason)"
  [ "$RECORDED_NO_SMOKE_REASON" = "$NO_SMOKE_REASON" ] || \
    delivery_fail "delivery.no_smoke_reason must exactly match gc.var.no_smoke_reason"
fi

[ -n "$MERGE_SHA" ] || delivery_fail "delivery.merge_sha is missing"
[[ "$MERGE_SHA" =~ ^[0-9a-f]{40}$ ]] || \
  delivery_fail "delivery.merge_sha is not a full lowercase Git SHA"
if [ "$DEPLOY_STATUS" = "not_applicable" ]; then
  [ "$DEPLOY_MODE" = "not-applicable" ] || \
    delivery_fail "not_applicable status is invalid for deploy_mode=$DEPLOY_MODE"
  delivery_command_is_nonblank "$NA_REASON" || \
    delivery_fail "not-applicable deployment requires a nonblank deploy_not_applicable_reason"
  [ -n "$DEPLOY_EVIDENCE" ] || delivery_fail "delivery.deploy_evidence_path is missing"
  DEPLOY_EVIDENCE="$(delivery_resolve_artifact_delivery_evidence "$DEPLOY_EVIDENCE" "deployment evidence path")" || \
    delivery_fail "deployment evidence path must resolve within the canonical artifact delivery directory"
  if [ ! -f "$DEPLOY_EVIDENCE" ] || [ ! -s "$DEPLOY_EVIDENCE" ]; then
    delivery_fail "deploy evidence is missing, not a file, or empty: $DEPLOY_EVIDENCE"
  fi
  echo "complete-delivery deployment explicitly not applicable: $NA_REASON"
  exit 0
fi

DELIVERY_REPO="$(delivery_root_metadata delivery.repo)"
DELIVERY_PR="$(delivery_root_metadata delivery.pr_number)"
[ -n "$DELIVERY_REPO" ] || delivery_fail "delivery.repo is missing"
[ -n "$DELIVERY_PR" ] || delivery_fail "delivery.pr_number is missing"

[ "$DEPLOY_STATUS" = "verified" ] || delivery_fail "delivery.deploy_status must be verified"
[ -n "$DEPLOYED_SHA" ] || delivery_fail "delivery.deployed_sha is missing"
[ "$DEPLOYED_SHA" = "$MERGE_SHA" ] || \
  delivery_fail "deployed SHA $DEPLOYED_SHA does not match merge SHA $MERGE_SHA"
[ -n "$DEPLOY_EVIDENCE" ] || delivery_fail "delivery.deploy_evidence_path is missing"
[ -n "$VERIFY_EVIDENCE" ] || delivery_fail "delivery.verify_evidence_path is missing"
DEPLOY_EVIDENCE="$(delivery_resolve_artifact_delivery_evidence "$DEPLOY_EVIDENCE" "deployment evidence path")"
VERIFY_EVIDENCE="$(delivery_resolve_artifact_delivery_evidence "$VERIFY_EVIDENCE" "verification evidence path")"
[ -f "$DEPLOY_EVIDENCE" ] && [ -s "$DEPLOY_EVIDENCE" ] || \
  delivery_fail "deploy evidence is missing, not a regular file, or empty: $DEPLOY_EVIDENCE"
[ -f "$VERIFY_EVIDENCE" ] && [ -s "$VERIFY_EVIDENCE" ] || \
  delivery_fail "verification evidence is missing, not a regular file, or empty: $VERIFY_EVIDENCE"

case "$DEPLOY_MODE" in
  command)
    delivery_command_is_nonblank "$DEPLOY_COMMAND" || \
      delivery_fail "deploy_command is required for deploy_mode=command"
    delivery_timeout_is_bounded "$DEPLOY_TIMEOUT" || \
      delivery_fail "deploy_timeout must be a positive finite duration no greater than 1h"
    DEPLOY_COMMAND_LABEL="$(delivery_command_label "$DEPLOY_COMMAND")"
    DEPLOY_EVIDENCE_LABEL="$(delivery_root_metadata delivery.deploy_command_label)"
    DEPLOY_EVIDENCE_TIMEOUT="$(delivery_root_metadata delivery.deploy_timeout)"
    DEPLOY_OUTCOME="$(delivery_root_metadata delivery.deploy_outcome)"
    DEPLOY_CHILD_STATUS="$(delivery_root_metadata delivery.deploy_child_status)"
    DEPLOY_WRAPPER_STATUS="$(delivery_root_metadata delivery.deploy_wrapper_status)"
    DEPLOY_MERGE_SHA="$(delivery_root_metadata delivery.deploy_merge_sha)"
    DEPLOY_STDOUT="$(delivery_root_metadata delivery.deploy_stdout_path)"
    DEPLOY_STDERR="$(delivery_root_metadata delivery.deploy_stderr_path)"
    [ -n "$DEPLOY_STDOUT" ] && [ -n "$DEPLOY_STDERR" ] || \
      delivery_fail "deploy evidence capture paths are missing"
    DEPLOY_STDOUT="$(delivery_resolve_artifact_delivery_evidence "$DEPLOY_STDOUT" "deployment stdout evidence path")"
    DEPLOY_STDERR="$(delivery_resolve_artifact_delivery_evidence "$DEPLOY_STDERR" "deployment stderr evidence path")"
    [ "$DEPLOY_EVIDENCE_LABEL" = "$DEPLOY_COMMAND_LABEL" ] || \
      delivery_fail "deploy evidence command label does not match deploy_command"
    [ "$DEPLOY_EVIDENCE_TIMEOUT" = "$DEPLOY_TIMEOUT" ] || \
      delivery_fail "deploy evidence timeout does not match deploy_timeout"
    [ "$DEPLOY_OUTCOME" = passed ] || delivery_fail "deploy evidence outcome must be passed"
    [ "$DEPLOY_CHILD_STATUS" = 0 ] || delivery_fail "deploy evidence child status must be 0"
    [ "$DEPLOY_WRAPPER_STATUS" = 0 ] || delivery_fail "deploy evidence wrapper status must be 0"
    [ "$DEPLOY_MERGE_SHA" = "$MERGE_SHA" ] || \
      delivery_fail "deploy evidence merge SHA does not match delivery.merge_sha"
    delivery_validate_command_deploy_evidence "$DEPLOY_EVIDENCE" \
      "$DEPLOY_COMMAND_LABEL" "$DEPLOY_TIMEOUT" "$MERGE_SHA" \
      "$DEPLOY_STDOUT" "$DEPLOY_STDERR" || \
      delivery_fail "deploy evidence is forged, stale, or incomplete"
    ;;
  ci)
    delivery_validate_ci_deploy_evidence "$DEPLOY_EVIDENCE"
    ;;
  *) delivery_fail "deploy_mode must be command, ci, or not-applicable" ;;
esac

export DELIVERY_SHA="$MERGE_SHA"
export DELIVERY_REPO DELIVERY_PR
cd "$DELIVERY_WORK_DIR"

VERIFY_COMMAND="$(delivery_var deploy_verify_command "")"
SMOKE_COMMAND="$(delivery_var smoke_command "")"
VERIFY_TIMEOUT="$(delivery_var deploy_verify_timeout 5m)"
SMOKE_TIMEOUT="$(delivery_var smoke_timeout 5m)"

delivery_command_is_nonblank "$VERIFY_COMMAND" || \
  delivery_fail "deploy_verify_command is required for deploy_mode=$DEPLOY_MODE"
command -v timeout >/dev/null 2>&1 || delivery_fail "timeout is required on PATH"
delivery_timeout_is_bounded "$VERIFY_TIMEOUT" || \
  delivery_fail "deploy_verify_timeout must be a positive finite duration no greater than 1h"

delivery_run_bounded_command deploy_verify "$VERIFY_COMMAND" "$VERIFY_TIMEOUT" || \
  delivery_fail "deploy_verify_command failed; see verification evidence"
if delivery_command_is_nonblank "$SMOKE_COMMAND"; then
  delivery_timeout_is_bounded "$SMOKE_TIMEOUT" || \
    delivery_fail "smoke_timeout must be a positive finite duration no greater than 1h"
  delivery_run_bounded_command smoke "$SMOKE_COMMAND" "$SMOKE_TIMEOUT" || \
    delivery_fail "smoke_command failed; see verification evidence"
elif [ "$ALLOW_NO_SMOKE" != "true" ]; then
  delivery_fail "smoke_command is required unless allow_no_smoke=true"
else
  NO_SMOKE_REASON_SHA256="$(delivery_value_sha256 "$NO_SMOKE_REASON")"
  printf 'command=smoke outcome=not_run reason=allow_no_smoke_true no_smoke_reason_sha256=%s\n' "$NO_SMOKE_REASON_SHA256" >>"$VERIFY_EVIDENCE" || \
    delivery_fail "failed to record no-smoke verification exception"
fi

echo "complete-delivery release verified at $MERGE_SHA"
