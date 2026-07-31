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
from typing import Any, Callable, Protocol
from urllib.parse import quote


GH_TIMEOUT_SECONDS = 60
REST_PAGE_SIZE = 100
MAX_REST_PAGES = 100
CODERABBIT_LOGINS = frozenset({"coderabbitai", "coderabbitai[bot]"})
CODERABBIT_STATUS_CONTEXTS = frozenset({"CodeRabbit", "coderabbit.ai"})
CODERABBIT_APP_SLUG = "coderabbitai"
CODERABBIT_CHECK_NAME = "CodeRabbit"
CODERABBIT_COMPLETED_STATUS_DESCRIPTION = "review completed"


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
    app_id: int | None = None
    updated_at: str = ""
    detail: str = ""


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
class RequiredCheck:
    """A required check, optionally bound to the GitHub App that owns it."""

    name: str
    app_id: int | None = None


@dataclass(frozen=True)
class BranchProtection:
    protected: bool
    required_checks: tuple[RequiredCheck, ...]

    @property
    def required_contexts(self) -> tuple[str, ...]:
        """The legacy context-name view retained in gate output."""
        return tuple(sorted({check.name for check in self.required_checks}))


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
            return BranchProtection(protected=False, required_checks=())
        if not isinstance(value, dict):
            raise GateError("branch-protection response was not an object")
        required = value.get("required_status_checks") or {}
        if not isinstance(required, dict):
            raise GateError("branch protection required_status_checks was not an object")
        contexts = [
            item for item in required.get("contexts", []) if isinstance(item, str)
        ]
        checks: list[RequiredCheck] = []
        check_contexts: set[str] = set()
        for item in required.get("checks", []):
            if isinstance(item, dict) and isinstance(item.get("context"), str):
                app_id = item.get("app_id")
                checks.append(
                    RequiredCheck(
                        item["context"],
                        app_id if isinstance(app_id, int) and not isinstance(app_id, bool) else None,
                    )
                )
                check_contexts.add(item["context"])
        # Modern branch protection repeats check names in ``contexts``.  When
        # a structured check is present, retain its app binding instead of
        # adding an unbound duplicate that a legacy status could satisfy.
        checks.extend(
            RequiredCheck(context)
            for context in contexts
            if context not in check_contexts
        )
        return BranchProtection(
            protected=True,
            required_checks=tuple(
                sorted(
                    set(checks),
                    key=lambda check: (
                        check.name,
                        -1 if check.app_id is None else check.app_id,
                    ),
                )
            ),
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
        if not isinstance(payload, dict):
            raise GateError("fixture root must be an object")
        self.payload = payload

    def _object(self, key: str) -> dict[str, Any]:
        value = self.payload.get(key)
        if not isinstance(value, dict):
            raise GateError(f"fixture {key} must be an object")
        return value

    def _object_list(self, key: str) -> list[dict[str, Any]]:
        value = self.payload.get(key)
        if not isinstance(value, list):
            raise GateError(f"fixture {key} must be a list")
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                raise GateError(f"fixture {key}[{index}] must be an object")
        return value

    def pull_request(self, repo: str, number: int) -> dict[str, Any]:
        return self._object("pull_request")

    def check_runs(self, repo: str, sha: str) -> list[dict[str, Any]]:
        return self._object_list("check_runs")

    def statuses(self, repo: str, sha: str) -> list[dict[str, Any]]:
        return self._object_list("statuses")

    def branch_protection(self, repo: str, branch: str) -> BranchProtection:
        value = self._object("branch_protection")
        names = value.get("required_contexts") or []
        raw_checks = value.get("required_checks") or []
        if not isinstance(names, list) or not isinstance(raw_checks, list):
            raise GateError("fixture branch protection contexts were not a list")
        checks = [RequiredCheck(item) for item in names if isinstance(item, str)]
        for item in raw_checks:
            if not isinstance(item, dict) or not isinstance(item.get("context"), str):
                continue
            app_id = item.get("app_id")
            checks.append(
                RequiredCheck(
                    item["context"],
                    app_id if isinstance(app_id, int) and not isinstance(app_id, bool) else None,
                )
            )
        return BranchProtection(
            protected=bool(value.get("protected")),
            required_checks=tuple(
                sorted(
                    set(checks),
                    key=lambda check: (
                        check.name,
                        -1 if check.app_id is None else check.app_id,
                    ),
                )
            ),
        )

    def reviews(self, repo: str, number: int) -> list[dict[str, Any]]:
        return self._object_list("reviews")

    def review_threads(self, repo: str, number: int) -> list[ReviewThread]:
        fields = {
            "thread_id": str,
            "author": str,
            "path": str,
            "url": str,
            "body": str,
            "is_resolved": bool,
            "is_outdated": bool,
        }
        threads: list[ReviewThread] = []
        for index, item in enumerate(self._object_list("review_threads")):
            for field, expected_type in fields.items():
                if not isinstance(item.get(field), expected_type):
                    type_name = "boolean" if expected_type is bool else "string"
                    raise GateError(
                        f"fixture review_threads[{index}].{field} must be a {type_name}"
                    )
            unexpected = set(item) - set(fields)
            if unexpected:
                raise GateError(
                    f"fixture review_threads[{index}] has unexpected fields: "
                    + ", ".join(sorted(unexpected))
                )
            threads.append(ReviewThread(**item))
        return threads


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
        app_id=(
            run.get("app", {}).get("id")
            if isinstance(run.get("app"), dict)
            and isinstance(run["app"].get("id"), int)
            and not isinstance(run["app"].get("id"), bool)
            else None
        ),
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
        detail=str(status.get("description") or ""),
    )


def latest_checks(client: GitHubClient, repo: str, sha: str) -> list[Check]:
    checks = [normalize_check_run(item) for item in client.check_runs(repo, sha)]
    checks.extend(normalize_status(item) for item in client.statuses(repo, sha))
    return [check for check in checks if check.name]


def latest_check(checks: list[Check], requirement: RequiredCheck) -> Check | None:
    """Return the newest signal that is eligible for one requirement.

    App-bound requirements must be satisfied by a check run from that exact
    app.  Legacy unbound contexts deliberately continue to accept the newest
    same-name check run or commit status.
    """
    candidates = [check for check in checks if check.name == requirement.name]
    if requirement.app_id is not None:
        candidates = [
            check
            for check in candidates
            if check.source == "check_run" and check.app_id == requirement.app_id
        ]
    return max(candidates, key=lambda check: check.updated_at) if candidates else None


def parse_required_checks(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def resolve_required_checks(
    protection: BranchProtection,
    configured: str,
    checks: list[Check],
) -> tuple[list[RequiredCheck], str]:
    if configured.strip().lower() != "auto":
        branch_checks = set(protection.required_checks)
        app_bound_names = {
            check.name for check in branch_checks if check.app_id is not None
        }
        configured_checks = {
            RequiredCheck(name)
            for name in parse_required_checks(configured)
            if name not in app_bound_names
        }
        source = "configured"
        if branch_checks:
            source = "configured+branch_protection"
        return (
            sorted(
                configured_checks | branch_checks,
                key=lambda check: (
                    check.name,
                    -1 if check.app_id is None else check.app_id,
                ),
            ),
            source,
        )
    if protection.required_checks:
        return list(protection.required_checks), "branch_protection"
    inferred = sorted(
        {
            RequiredCheck(check.name)
            for check in checks
            if check.name not in CODERABBIT_STATUS_CONTEXTS
            and check.app_slug != CODERABBIT_APP_SLUG
            and check.state != "neutral"
        },
        key=lambda check: check.name,
    )
    return inferred, "head_checks"


def coderabbit_completion(
    checks: list[Check], reviews: list[dict[str, Any]], head_sha: str
) -> tuple[bool, str, str, str]:
    candidates: list[Check] = []
    for check in checks:
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
        if latest.source == "status":
            detail = coderabbit_status_detail(latest.detail)
            return (
                latest.state == "success" and detail == "review_completed",
                f"{latest.source}:{latest.name}",
                latest.state,
                detail,
            )
        return (
            latest.state == "success",
            f"{latest.source}:{latest.name}",
            latest.state,
            "check_run",
        )

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
        return completed, "review", state.lower(), f"review_{state.lower()}"
    return False, "", "missing", "missing"


def coderabbit_status_detail(description: str) -> str:
    """Classify a CodeRabbit status without exposing untrusted status text.

    GitHub legacy statuses are free-form strings.  A status is evidence of a
    completed review only when CodeRabbit uses its explicit completion phrase;
    every other description fails closed.  The returned categories are a fixed
    vocabulary so gate output never treats arbitrary status text as evidence.
    """
    normalized = " ".join(description.casefold().split())
    if normalized == CODERABBIT_COMPLETED_STATUS_DESCRIPTION:
        return "review_completed"
    if "rate limit" in normalized or "rate-limit" in normalized:
        return "review_rate_limited"
    if "skip" in normalized:
        return "review_skipped"
    if "unavailable" in normalized:
        return "review_unavailable"
    return "review_not_completed"


def current_change_requests(
    reviews: list[dict[str, Any]],
    head_sha: str,
    *,
    include_login: Callable[[str], bool],
) -> list[str]:
    latest_change_request: dict[str, str] = {}
    latest_approval: dict[str, str] = {}
    for review in reviews:
        login = nested_string(review, "user", "login")
        if not login or not include_login(login):
            continue
        if str(review.get("commit_id") or "") != head_sha:
            continue
        state = str(review.get("state") or "")
        if state == "DISMISSED":
            continue

        submitted_at = str(review.get("submitted_at") or "")
        if state == "CHANGES_REQUESTED":
            current = latest_change_request.get(login)
            if current is None or submitted_at >= current:
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


def current_human_change_requests(
    reviews: list[dict[str, Any]], head_sha: str
) -> list[str]:
    return current_change_requests(
        reviews,
        head_sha,
        include_login=lambda login: login.lower() not in CODERABBIT_LOGINS,
    )


def current_coderabbit_change_requests(
    reviews: list[dict[str, Any]], head_sha: str
) -> list[str]:
    return current_change_requests(
        reviews,
        head_sha,
        include_login=lambda login: login.lower() in CODERABBIT_LOGINS,
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
    check_results: list[dict[str, Any]] = []
    if not names and not allow_no_ci:
        blockers.append("No required CI checks were configured or discoverable")
    for requirement in names:
        check = latest_check(checks, requirement)
        label = requirement.name
        if requirement.app_id is not None:
            label += f" (app {requirement.app_id})"
        if check is None:
            check_results.append(
                {
                    "name": requirement.name,
                    "app_id": requirement.app_id,
                    "state": "missing",
                    "url": "",
                }
            )
            blockers.append(f"Required check is missing: {label}")
        else:
            check_results.append(
                {
                    "name": requirement.name,
                    "app_id": requirement.app_id,
                    "state": check.state,
                    "url": check.url,
                }
            )
            if check.state != "success":
                blockers.append(f"Required check is not successful: {label}={check.state}")

    reviews = client.reviews(repo, pr_number)
    cr_completed, cr_signal, cr_state, cr_detail = coderabbit_completion(
        checks, reviews, head_sha
    )
    coderabbit_change_requests = current_coderabbit_change_requests(reviews, head_sha)
    if coderabbit_change_requests:
        # A current-head change request is authoritative review state.  Do
        # this before considering a successful status or check run complete.
        cr_completed = False
        cr_state = "changes_requested"
        cr_detail = "review_changes_requested"
    threads = client.review_threads(repo, pr_number)
    unresolved = [
        thread for thread in threads if not thread.is_resolved and not thread.is_outdated
    ]
    unresolved_coderabbit = [
        thread for thread in unresolved if thread.author.lower() in CODERABBIT_LOGINS
    ]
    unresolved_human = [
        thread for thread in unresolved if thread.author.lower() not in CODERABBIT_LOGINS
    ]
    if coderabbit_mode == "required" and not cr_completed:
        blockers.append(f"CodeRabbit has not completed successfully on head {head_sha}")
        if cr_signal.startswith("status:"):
            blockers.append(f"CodeRabbit status did not confirm completion: {cr_detail}")
    if unresolved_coderabbit and coderabbit_mode != "off":
        blockers.append(
            f"{len(unresolved_coderabbit)} unresolved CodeRabbit review thread(s) remain"
        )
    if unresolved_human:
        blockers.append(f"{len(unresolved_human)} unresolved review thread(s) remain")

    if coderabbit_change_requests and coderabbit_mode != "off":
        blockers.append(
            "Outstanding CodeRabbit change request(s): "
            + ", ".join(coderabbit_change_requests)
        )

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
            "required_checks": [asdict(check) for check in protection.required_checks],
        },
        "required_checks_source": required_source,
        "required_checks": check_results,
        "coderabbit": {
            "mode": coderabbit_mode,
            "completed": cr_completed,
            "signal": cr_signal,
            "state": cr_state,
            "detail": cr_detail,
            "unresolved_threads": len(unresolved_coderabbit),
            "active_change_requests": coderabbit_change_requests,
        },
        "unresolved_threads": [
            asdict(thread)
            for thread in unresolved_human
            + (unresolved_coderabbit if coderabbit_mode != "off" else [])
        ],
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
