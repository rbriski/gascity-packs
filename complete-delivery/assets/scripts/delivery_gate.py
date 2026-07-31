#!/usr/bin/env python3
"""Fail-closed GitHub delivery gate for a pull request's current head.

The gate combines repository-required CI, CodeRabbit completion, unresolved
review threads, and outstanding human change requests.  It never trusts a
result from an older head SHA.  The CLI emits one JSON document and exits 0
only when every configured condition passes.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote


GH_TIMEOUT_SECONDS = 60
REST_PAGE_SIZE = 100
MAX_REST_PAGES = 100
CODERABBIT_LOGINS = frozenset({"coderabbitai", "coderabbitai[bot]"})
CODERABBIT_STATUS_CONTEXTS = frozenset({"CodeRabbit", "coderabbit.ai"})
CODERABBIT_APP_SLUG = "coderabbitai"
CODERABBIT_CHECK_NAME = "CodeRabbit"


class GateError(RuntimeError):
    """A deterministic input or GitHub API failure."""


@dataclass(frozen=True)
class Check:
    name: str
    state: str
    source: str
    url: str = ""
    actor: str = ""
    app_slug: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class ReviewThread:
    thread_id: str
    author: str
    path: str
    url: str
    body: str
    is_resolved: bool
    is_outdated: bool


@dataclass(frozen=True)
class BranchProtection:
    protected: bool
    required_contexts: tuple[str, ...]


class GitHubClient(Protocol):
    def pull_request(self, repo: str, number: int) -> dict[str, Any]: ...

    def check_runs(self, repo: str, sha: str) -> list[dict[str, Any]]: ...

    def statuses(self, repo: str, sha: str) -> list[dict[str, Any]]: ...

    def branch_protection(self, repo: str, branch: str) -> BranchProtection: ...

    def reviews(self, repo: str, number: int) -> list[dict[str, Any]]: ...

    def review_threads(self, repo: str, number: int) -> list[ReviewThread]: ...


class GhClient:
    """GitHub client backed by the authenticated ``gh`` CLI."""

    @staticmethod
    def _run(*args: str, allow_not_found: bool = False) -> str:
        proc = subprocess.run(
            ["gh", "api", *args],
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_SECONDS,
            check=False,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            if allow_not_found and ("HTTP 404" in detail or "Not Found" in detail):
                return ""
            raise GateError(detail or f"gh api exited {proc.returncode}")
        return proc.stdout.strip()

    @classmethod
    def _json(cls, *args: str, allow_not_found: bool = False) -> Any:
        raw = cls._run(*args, allow_not_found=allow_not_found)
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GateError(f"gh api returned invalid JSON: {exc}") from exc

    @classmethod
    def _rest_items(
        cls, endpoint: str, *, collection_key: str | None = None
    ) -> list[dict[str, Any]]:
        """Read every REST page without relying on newer ``gh --slurp`` support."""
        items: list[dict[str, Any]] = []
        separator = "&" if "?" in endpoint else "?"
        for page_number in range(1, MAX_REST_PAGES + 1):
            value = cls._json(
                f"{endpoint}{separator}per_page={REST_PAGE_SIZE}&page={page_number}"
            )
            if collection_key is None:
                page = value
            else:
                if not isinstance(value, dict):
                    raise GateError(f"{collection_key} response was not an object")
                page = value.get(collection_key)
            if not isinstance(page, list):
                label = collection_key or "REST page"
                raise GateError(f"{label} response was not a list")
            typed_page = [item for item in page if isinstance(item, dict)]
            items.extend(typed_page)
            if len(page) < REST_PAGE_SIZE:
                return items
        raise GateError(
            f"REST pagination exceeded {MAX_REST_PAGES} pages for {endpoint}"
        )

    def pull_request(self, repo: str, number: int) -> dict[str, Any]:
        value = self._json(f"repos/{repo}/pulls/{number}")
        if not isinstance(value, dict):
            raise GateError("pull request response was not an object")
        return value

    def check_runs(self, repo: str, sha: str) -> list[dict[str, Any]]:
        return self._rest_items(
            f"repos/{repo}/commits/{sha}/check-runs",
            collection_key="check_runs",
        )

    def statuses(self, repo: str, sha: str) -> list[dict[str, Any]]:
        return self._rest_items(f"repos/{repo}/commits/{sha}/statuses")

    def branch_protection(self, repo: str, branch: str) -> BranchProtection:
        encoded = quote(branch, safe="")
        value = self._json(
            f"repos/{repo}/branches/{encoded}/protection",
            allow_not_found=True,
        )
        if not value:
            return BranchProtection(protected=False, required_contexts=())
        if not isinstance(value, dict):
            raise GateError("branch-protection response was not an object")
        required = value.get("required_status_checks") or {}
        if not isinstance(required, dict):
            raise GateError("branch protection required_status_checks was not an object")
        names = [item for item in required.get("contexts", []) if isinstance(item, str)]
        for item in required.get("checks", []):
            if isinstance(item, dict) and isinstance(item.get("context"), str):
                names.append(item["context"])
        return BranchProtection(
            protected=True,
            required_contexts=tuple(sorted(set(names))),
        )

    def reviews(self, repo: str, number: int) -> list[dict[str, Any]]:
        return self._rest_items(f"repos/{repo}/pulls/{number}/reviews")

    def review_threads(self, repo: str, number: int) -> list[ReviewThread]:
        owner, separator, name = repo.partition("/")
        if not separator or not owner or not name:
            raise GateError("repo must be owner/name")
        query = """
        query($owner: String!, $repo: String!, $pr: Int!, $cursor: String) {
          repository(owner: $owner, name: $repo) {
            pullRequest(number: $pr) {
              reviewThreads(first: 100, after: $cursor) {
                pageInfo { hasNextPage endCursor }
                nodes {
                  id isResolved isOutdated path
                  comments(first: 1) {
                    nodes { body url author { login } }
                  }
                }
              }
            }
          }
        }
        """
        result: list[ReviewThread] = []
        cursor: str | None = None
        while True:
            args = [
                "graphql",
                "-f",
                f"query={query}",
                "-F",
                f"owner={owner}",
                "-F",
                f"repo={name}",
                "-F",
                f"pr={number}",
            ]
            if cursor:
                args.extend(["-F", f"cursor={cursor}"])
            value = self._json(*args)
            try:
                page = value["data"]["repository"]["pullRequest"]["reviewThreads"]
            except (KeyError, TypeError) as exc:
                raise GateError("review-thread response was missing expected fields") from exc
            for node in page.get("nodes", []):
                comments = ((node.get("comments") or {}).get("nodes") or [])
                first = comments[0] if comments else {}
                author = ((first.get("author") or {}).get("login") or "")
                result.append(
                    ReviewThread(
                        thread_id=str(node.get("id") or ""),
                        author=str(author),
                        path=str(node.get("path") or ""),
                        url=str(first.get("url") or ""),
                        body=str(first.get("body") or ""),
                        is_resolved=bool(node.get("isResolved")),
                        is_outdated=bool(node.get("isOutdated")),
                    )
                )
            page_info = page.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            cursor = str(page_info.get("endCursor") or "")
            if not cursor:
                raise GateError("review-thread pagination omitted endCursor")
        return result


class FixtureClient:
    """Offline client for deterministic pack smoke tests."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def pull_request(self, repo: str, number: int) -> dict[str, Any]:
        return dict(self.payload.get("pull_request") or {})

    def check_runs(self, repo: str, sha: str) -> list[dict[str, Any]]:
        return list(self.payload.get("check_runs") or [])

    def statuses(self, repo: str, sha: str) -> list[dict[str, Any]]:
        return list(self.payload.get("statuses") or [])

    def branch_protection(self, repo: str, branch: str) -> BranchProtection:
        value = self.payload.get("branch_protection") or {}
        if not isinstance(value, dict):
            raise GateError("fixture branch_protection was not an object")
        names = value.get("required_contexts") or []
        if not isinstance(names, list):
            raise GateError("fixture branch protection contexts were not a list")
        return BranchProtection(
            protected=bool(value.get("protected")),
            required_contexts=tuple(
                sorted(set(item for item in names if isinstance(item, str)))
            ),
        )

    def reviews(self, repo: str, number: int) -> list[dict[str, Any]]:
        return list(self.payload.get("reviews") or [])

    def review_threads(self, repo: str, number: int) -> list[ReviewThread]:
        return [ReviewThread(**item) for item in self.payload.get("review_threads") or []]


def nested_string(value: Any, *keys: str) -> str:
    for key in keys:
        if not isinstance(value, dict):
            return ""
        value = value.get(key)
    return value if isinstance(value, str) else ""


def normalize_check_run(run: dict[str, Any]) -> Check:
    status = str(run.get("status") or "")
    conclusion = str(run.get("conclusion") or "")
    if status != "completed":
        state = "pending"
    elif conclusion == "success":
        state = "success"
    elif conclusion in {"neutral", "skipped"}:
        state = "neutral"
    else:
        state = "failure"
    return Check(
        name=str(run.get("name") or ""),
        state=state,
        source="check_run",
        url=str(run.get("html_url") or run.get("details_url") or ""),
        actor=nested_string(run, "app", "name"),
        app_slug=nested_string(run, "app", "slug"),
        updated_at=str(run.get("completed_at") or run.get("started_at") or ""),
    )


def normalize_status(status: dict[str, Any]) -> Check:
    raw_state = str(status.get("state") or "")
    state = raw_state if raw_state in {"success", "pending"} else "failure"
    return Check(
        name=str(status.get("context") or ""),
        state=state,
        source="status",
        url=str(status.get("target_url") or ""),
        actor=nested_string(status, "creator", "login"),
        updated_at=str(status.get("updated_at") or status.get("created_at") or ""),
    )


def latest_checks(client: GitHubClient, repo: str, sha: str) -> dict[str, Check]:
    checks = [normalize_check_run(item) for item in client.check_runs(repo, sha)]
    checks.extend(normalize_status(item) for item in client.statuses(repo, sha))
    latest: dict[str, Check] = {}
    for check in checks:
        if not check.name:
            continue
        current = latest.get(check.name)
        if current is None or check.updated_at >= current.updated_at:
            latest[check.name] = check
    return latest


def parse_required_checks(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def resolve_required_checks(
    protection: BranchProtection,
    configured: str,
    checks: dict[str, Check],
) -> tuple[list[str], str]:
    if configured.strip().lower() != "auto":
        configured_names = set(parse_required_checks(configured))
        branch_names = set(protection.required_contexts)
        source = "configured"
        if branch_names:
            source = "configured+branch_protection"
        return sorted(configured_names | branch_names), source
    if protection.required_contexts:
        return list(protection.required_contexts), "branch_protection"
    inferred = sorted(
        name
        for name, check in checks.items()
        if name not in CODERABBIT_STATUS_CONTEXTS
        and check.app_slug != CODERABBIT_APP_SLUG
        and check.state != "neutral"
    )
    return inferred, "head_checks"


def coderabbit_completion(
    checks: dict[str, Check], reviews: list[dict[str, Any]], head_sha: str
) -> tuple[bool, str, str]:
    candidates: list[Check] = []
    for check in checks.values():
        trusted_status = (
            check.source == "status"
            and check.name in CODERABBIT_STATUS_CONTEXTS
            and check.actor.lower() in CODERABBIT_LOGINS
        )
        trusted_run = (
            check.source == "check_run"
            and check.name == CODERABBIT_CHECK_NAME
            and check.app_slug == CODERABBIT_APP_SLUG
        )
        if trusted_status or trusted_run:
            candidates.append(check)
    if candidates:
        latest = max(candidates, key=lambda item: item.updated_at)
        return latest.state == "success", f"{latest.source}:{latest.name}", latest.state

    matching_reviews = [
        review
        for review in reviews
        if nested_string(review, "user", "login").lower() in CODERABBIT_LOGINS
        and str(review.get("commit_id") or "") == head_sha
        and str(review.get("state") or "") != "DISMISSED"
    ]
    if matching_reviews:
        latest_review = max(matching_reviews, key=lambda item: str(item.get("submitted_at") or ""))
        state = str(latest_review.get("state") or "")
        completed = state in {"APPROVED", "COMMENTED"}
        return completed, "review", state.lower()
    return False, "", "missing"


def current_human_change_requests(
    reviews: list[dict[str, Any]], head_sha: str
) -> list[str]:
    latest_change_request: dict[str, str] = {}
    latest_approval: dict[str, str] = {}
    for review in reviews:
        login = nested_string(review, "user", "login")
        if not login or login.lower() in CODERABBIT_LOGINS:
            continue
        if str(review.get("commit_id") or "") != head_sha:
            continue
        state = str(review.get("state") or "")
        if state == "DISMISSED":
            continue

        submitted_at = str(review.get("submitted_at") or "")
        if state == "CHANGES_REQUESTED":
            current = latest_change_request.get(login)
            if current is None or submitted_at >= current[0]:
                latest_change_request[login] = submitted_at
        elif state == "APPROVED":
            current = latest_approval.get(login)
            if current is None or submitted_at >= current:
                latest_approval[login] = submitted_at

    return sorted(
        login
        for login, requested_at in latest_change_request.items()
        if latest_approval.get(login, "") <= requested_at
    )


def evaluate(
    client: GitHubClient,
    *,
    repo: str,
    pr_number: int,
    required_checks: str = "auto",
    coderabbit_mode: str = "required",
    allow_no_ci: bool = False,
) -> dict[str, Any]:
    if "/" not in repo:
        raise GateError("repo must be owner/name")
    if pr_number < 1:
        raise GateError("PR number must be positive")
    if coderabbit_mode not in {"required", "optional", "off"}:
        raise GateError("coderabbit mode must be required, optional, or off")

    pull = client.pull_request(repo, pr_number)
    head_sha = nested_string(pull, "head", "sha")
    base_ref = nested_string(pull, "base", "ref")
    if not head_sha or not base_ref:
        raise GateError("pull request response omitted head SHA or base ref")

    protection = client.branch_protection(repo, base_ref)
    checks = latest_checks(client, repo, head_sha)
    names, required_source = resolve_required_checks(
        protection, required_checks, checks
    )
    blockers: list[str] = []
    if not protection.protected:
        blockers.append(f"Base branch is not protected: {base_ref}")
    check_results: list[dict[str, str]] = []
    if not names and not allow_no_ci:
        blockers.append("No required CI checks were configured or discoverable")
    for name in names:
        check = checks.get(name)
        if check is None:
            check_results.append({"name": name, "state": "missing", "url": ""})
            blockers.append(f"Required check is missing: {name}")
        else:
            check_results.append({"name": name, "state": check.state, "url": check.url})
            if check.state != "success":
                blockers.append(f"Required check is not successful: {name}={check.state}")

    reviews = client.reviews(repo, pr_number)
    cr_completed, cr_signal, cr_state = coderabbit_completion(checks, reviews, head_sha)
    threads = client.review_threads(repo, pr_number)
    unresolved = [
        thread for thread in threads if not thread.is_resolved and not thread.is_outdated
    ]
    unresolved_coderabbit = [
        thread for thread in unresolved if thread.author.lower() in CODERABBIT_LOGINS
    ]
    if coderabbit_mode == "required" and not cr_completed:
        blockers.append(f"CodeRabbit has not completed successfully on head {head_sha}")
    if unresolved_coderabbit and coderabbit_mode != "off":
        blockers.append(
            f"{len(unresolved_coderabbit)} unresolved CodeRabbit review thread(s) remain"
        )
    if unresolved:
        blockers.append(f"{len(unresolved)} unresolved review thread(s) remain")

    change_requests = current_human_change_requests(reviews, head_sha)
    if change_requests:
        blockers.append(
            "Outstanding human change request(s): " + ", ".join(change_requests)
        )
    if str(pull.get("state") or "").lower() != "open":
        blockers.append("Pull request is not open")
    if bool(pull.get("draft")):
        blockers.append("Pull request is still a draft")

    refreshed = client.pull_request(repo, pr_number)
    refreshed_sha = nested_string(refreshed, "head", "sha")
    if refreshed_sha != head_sha:
        blockers.append(
            f"Pull request head moved during evaluation: {head_sha} -> {refreshed_sha}"
        )

    return {
        "schema": "gc.complete-delivery.pr-gate.v1",
        "passed": not blockers,
        "state": "passed" if not blockers else "blocked",
        "repo": repo,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "base_ref": base_ref,
        "branch_protection": {
            "protected": protection.protected,
            "required_contexts": list(protection.required_contexts),
        },
        "required_checks_source": required_source,
        "required_checks": check_results,
        "coderabbit": {
            "mode": coderabbit_mode,
            "completed": cr_completed,
            "signal": cr_signal,
            "state": cr_state,
            "unresolved_threads": len(unresolved_coderabbit),
        },
        "unresolved_threads": [asdict(thread) for thread in unresolved],
        "human_change_requests": change_requests,
        "blockers": blockers,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="GitHub owner/repository")
    parser.add_argument("--pr", required=True, type=int, help="Pull request number")
    parser.add_argument(
        "--required-checks",
        default="auto",
        help="Comma-separated exact check names, or auto",
    )
    parser.add_argument(
        "--coderabbit",
        choices=("required", "optional", "off"),
        default="required",
    )
    parser.add_argument("--allow-no-ci", action="store_true")
    parser.add_argument("--fixture", type=Path, help="Offline JSON fixture")
    parser.add_argument("--output", type=Path, help="Also write the JSON result here")
    return parser.parse_args(argv)


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        if args.fixture:
            payload = json.loads(args.fixture.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise GateError("fixture root must be an object")
            client: GitHubClient = FixtureClient(payload)
        else:
            client = GhClient()
        result = evaluate(
            client,
            repo=args.repo,
            pr_number=args.pr,
            required_checks=args.required_checks,
            coderabbit_mode=args.coderabbit,
            allow_no_ci=args.allow_no_ci,
        )
        exit_code = 0 if result["passed"] else 1
    except (GateError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        result = {
            "schema": "gc.complete-delivery.pr-gate.v1",
            "passed": False,
            "state": "error",
            "repo": args.repo,
            "pr_number": args.pr,
            "blockers": [str(exc)],
        }
        exit_code = 2
    rendered = json.dumps(result, indent=2, sort_keys=True)
    if args.output:
        atomic_write(args.output, rendered + "\n")
    print(rendered)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
