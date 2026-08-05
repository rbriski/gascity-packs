#!/usr/bin/env python3
"""Create and update a compact living HTML delivery report."""

from __future__ import annotations

import argparse
import html
import ipaddress
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


SCHEMA = "gc.complete-delivery.report.v1"
STAGES = (
    "intake",
    "plan",
    "implementation",
    "local-gates",
    "review",
    "qa",
    "pull-request",
    "external-review",
    "merge",
    "deploy",
    "verify",
    "complete",
)
STATUSES = frozenset({"pending", "active", "passed", "failed", "blocked", "skipped"})
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
SOURCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")

CSS = """
:root{--ink:#17201d;--muted:#68736e;--paper:#f5f3ed;--card:#fffefa;--line:#d9ddd6;--green:#176b50;--soft:#dff2e9;--gold:#d89a32;--red:#b84e3a;--navy:#142d31;font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:var(--ink);background:var(--paper)}
*{box-sizing:border-box}body{margin:0;line-height:1.5}.shell{width:min(1080px,calc(100% - 2rem));margin:auto}.top{padding:4.5rem 0 3rem;background:linear-gradient(145deg,#f8f6ef 55%,#e1eee7)}.eyebrow{margin:0 0 .7rem;color:var(--green);font-size:.72rem;font-weight:800;letter-spacing:.13em;text-transform:uppercase}h1{max-width:800px;margin:0 0 1rem;font:750 clamp(2.6rem,6vw,5.1rem)/1 Georgia,serif;letter-spacing:-.05em}.lede{max-width:720px;color:#4e5c56;font-size:1.12rem}.summary{display:grid;grid-template-columns:repeat(4,1fr);gap:.8rem;margin-top:2.2rem}.summary div{padding:1.1rem;border:1px solid var(--line);border-radius:.85rem;background:var(--card)}.summary span{display:block;color:var(--muted);font-size:.76rem;text-transform:uppercase;letter-spacing:.06em}.summary strong{display:block;margin-top:.3rem}.state{color:var(--green)!important}.state.blocked,.state.failed{color:var(--red)!important}.section{padding:4rem 0}.section h2{margin:0 0 1.6rem;font:700 clamp(1.8rem,4vw,2.8rem)/1.1 Georgia,serif;letter-spacing:-.03em}.timeline{list-style:none;margin:0;padding:0;border-top:1px solid var(--line)}.timeline li{display:grid;grid-template-columns:2.3rem 1fr auto;gap:1rem;padding:1.2rem 0;border-bottom:1px solid var(--line)}.num{display:grid;place-items:center;width:2.2rem;height:2.2rem;border:1px solid var(--line);border-radius:50%;font-weight:800}.timeline p{margin:.3rem 0 0;color:var(--muted)}.badge{height:max-content;padding:.25rem .6rem;border-radius:2rem;background:#e7e8e3;color:var(--muted);font-size:.72rem;font-weight:800;text-transform:uppercase}.passed .num{color:#fff;border-color:var(--green);background:var(--green)}.passed .badge{color:var(--green);background:var(--soft)}.active{margin-inline:-.7rem;padding-inline:.7rem!important;border-radius:.7rem;background:#fff}.active .num,.active .badge{color:#69420e;border-color:var(--gold);background:#f7e8ca}.failed .badge,.blocked .badge{color:#7b2f22;background:#f6ddd7}.evidence{margin:.55rem 0 0;padding-left:1rem;color:var(--muted);font-size:.88rem}.next{padding:1.5rem;border-radius:1rem;color:#fff;background:var(--navy)}.next p{margin:.4rem 0 0;color:#c4d5cf}.links{display:flex;flex-wrap:wrap;gap:.6rem;margin-top:1rem}.links a{padding:.5rem .8rem;border-radius:.55rem;color:#d8f5e9;background:#0d2225;text-decoration:none}footer{padding:2rem 0;color:#b9cbc5;background:#102629}footer .shell{display:flex;justify-content:space-between;gap:1rem}@media(max-width:760px){.summary{grid-template-columns:repeat(2,1fr)}.timeline li{grid-template-columns:2.3rem 1fr}.badge{grid-column:2;justify-self:start}footer .shell{flex-direction:column}}@media(max-width:440px){.summary{grid-template-columns:1fr}}
""".strip()


class ReportError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def load_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportError(f"cannot read report state {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise ReportError(f"{path} is not a {SCHEMA} state file")
    return value


def overall_status(state: dict[str, Any]) -> str:
    stages = state.get("stages") or {}
    values = [item.get("status") for item in stages.values() if isinstance(item, dict)]
    if "failed" in values:
        return "failed"
    if "blocked" in values:
        return "blocked"
    if final_state_is_live(state):
        return "live"
    return "in progress"


def final_state_is_live(state: dict[str, Any]) -> bool:
    """Return true only for a mechanically finalized, internally bound state."""

    stages = state.get("stages")
    delivery = state.get("delivery")
    validation = state.get("final_validation")
    if not all(isinstance(value, dict) for value in (stages, delivery, validation)):
        return False
    deploy_status = delivery.get("deploy_status")
    if deploy_status not in {"verified", "not_applicable"}:
        return False
    for stage in (stage for stage in STAGES if stage != "deploy"):
        if (stages.get(stage) or {}).get("status") != "passed":
            return False
    allowed_deploy = {"passed", "skipped"} if deploy_status == "not_applicable" else {"passed"}
    if (stages.get("deploy") or {}).get("status") not in allowed_deploy:
        return False

    merge_sha = delivery.get("merge_sha")
    if not isinstance(merge_sha, str) or not FULL_SHA.fullmatch(merge_sha):
        return False
    if state.get("sha") != merge_sha:
        return False
    if deploy_status == "verified":
        if delivery.get("deployed_sha") != merge_sha:
            return False
    elif delivery.get("deployed_sha") != "":
        return False

    pr_url = state.get("pr_url")
    production_url = state.get("production_url") or ""
    if not safe_href(pr_url) or delivery.get("pr_url") != pr_url:
        return False
    if delivery.get("production_url") != production_url:
        return False
    if production_url and not safe_href(production_url):
        return False

    if validation.get("schema") != "gc.complete-delivery.final-validation.v1":
        return False
    if validation.get("passed") is not True:
        return False
    for field in (
        "merge_sha",
        "deployed_sha",
        "deploy_status",
        "pr_url",
        "production_url",
    ):
        if validation.get(field) != delivery.get(field):
            return False

    if validation.get("source_binding_required") is True:
        source_id = state.get("source_bead_id")
        source_title = state.get("source_title")
        if not isinstance(source_id, str) or not SOURCE_ID.fullmatch(source_id):
            return False
        if not isinstance(source_title, str) or not source_title:
            return False
        if state.get("bead_id") != source_id or state.get("title") != source_title:
            return False
        if validation.get("source_bead_id") != source_id:
            return False
        if validation.get("source_title") != source_title:
            return False

    no_smoke_required = delivery.get("no_smoke_required") is True
    reason = delivery.get("no_smoke_reason")
    if no_smoke_required:
        if not isinstance(reason, str) or not reason.strip():
            return False
        if state.get("no_smoke_reason") != reason:
            return False
    elif reason != "" or state.get("no_smoke_reason") not in (None, ""):
        return False
    if validation.get("no_smoke_required") is not no_smoke_required:
        return False
    if validation.get("no_smoke_reason") != (reason or ""):
        return False
    return True


def esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def safe_href(value: Any) -> str:
    raw = str(value or "")
    if raw != raw.strip() or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in raw
    ):
        return ""
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname
        parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return ""
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        if (
            len(hostname) > 253
            or hostname.endswith(".")
            or not all(HOST_LABEL.fullmatch(label) for label in hostname.split("."))
        ):
            return ""
    if not raw:
        return ""
    return esc(raw)


def render(state: dict[str, Any]) -> str:
    stages = state.get("stages") or {}
    stage_rows: list[str] = []
    for index, stage in enumerate(STAGES, start=1):
        item = stages.get(stage) or {"status": "pending", "summary": "Waiting"}
        status = str(item.get("status") or "pending")
        evidence = item.get("evidence") or []
        evidence_html = ""
        if evidence:
            evidence_html = '<ul class="evidence">' + "".join(
                f"<li>{esc(entry)}</li>" for entry in evidence
            ) + "</ul>"
        label = stage.replace("-", " ").title()
        stage_rows.append(
            f'<li class="{esc(status)}"><span class="num">{index}</span>'
            f"<div><strong>{esc(label)}</strong><p>{esc(item.get('summary'))}</p>"
            f"{evidence_html}</div><span class=\"badge\">{esc(status)}</span></li>"
        )
    status = overall_status(state)
    links: list[str] = []
    pr_url = safe_href(state.get("pr_url"))
    production_url = safe_href(state.get("production_url"))
    if pr_url:
        links.append(f'<a href="{pr_url}">Pull request</a>')
    if production_url:
        links.append(f'<a href="{production_url}">Production</a>')
    links_html = f'<div class="links">{"".join(links)}</div>' if links else ""
    no_smoke_reason = str(state.get("no_smoke_reason") or "").strip()
    no_smoke_html = ""
    if no_smoke_reason:
        no_smoke_html = (
            '<section class="section"><div class="shell"><div class="next">'
            '<strong>Production smoke-test exception</strong>'
            f'<p>{esc(no_smoke_reason)}</p></div></div></section>'
        )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow,noarchive"><meta http-equiv="Content-Security-Policy" content="default-src 'self'; style-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'"><title>{esc(state.get('title'))} — Delivery Report</title><link rel="stylesheet" href="styles.css"></head>
<body><main><section class="top"><div class="shell"><p class="eyebrow">Living delivery report · {esc(state.get('bead_id'))}</p><h1>{esc(state.get('title'))}</h1><p class="lede">{esc(state.get('goal'))}</p><div class="summary"><div><span>Status</span><strong class="state {esc(status)}">{esc(status.title())}</strong></div><div><span>Repository</span><strong>{esc(state.get('repo') or 'Resolving')}</strong></div><div><span>Delivery SHA</span><strong>{esc(state.get('sha') or 'Pending')}</strong></div><div><span>Updated</span><strong>{esc(state.get('updated_at'))}</strong></div></div></div></section>
<section class="section"><div class="shell"><p class="eyebrow">Lifecycle</p><h2>One path from intent to verified production</h2><ol class="timeline">{"".join(stage_rows)}</ol></div></section>
<section class="section"><div class="shell"><div class="next"><strong>Next action</strong><p>{esc(state.get('next_action') or 'Continue the active lifecycle stage.')}</p>{links_html}</div></div></section>{no_smoke_html}</main>
<footer><div class="shell"><strong>Complete Delivery</strong><span>Generated from durable milestone evidence · {esc(SCHEMA)}</span></div></footer></body></html>
"""


def persist(state_path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = now()
    output_dir = state_path.parent
    atomic_write(state_path, json.dumps(state, indent=2, sort_keys=True) + "\n")
    atomic_write(output_dir / "index.html", render(state))
    atomic_write(output_dir / "styles.css", CSS + "\n")


def initialize(args: argparse.Namespace) -> dict[str, Any]:
    if args.state.exists():
        state = load_state(args.state)
        state.update(
            title=args.title,
            goal=args.goal,
            repo=args.repo or state.get("repo", ""),
            bead_id=args.bead_id or state.get("bead_id", ""),
            source_bead_id=args.bead_id or state.get("source_bead_id", ""),
            source_title=args.title,
        )
    else:
        state = {
            "schema": SCHEMA,
            "title": args.title,
            "goal": args.goal,
            "repo": args.repo,
            "bead_id": args.bead_id,
            "source_bead_id": args.bead_id,
            "source_title": args.title,
            "sha": "",
            "pr_url": "",
            "production_url": "",
            "no_smoke_reason": "",
            "next_action": "Produce and approve the implementation plan.",
            "created_at": now(),
            "updated_at": now(),
            "stages": {
                stage: {"status": "pending", "summary": "Waiting", "evidence": []}
                for stage in STAGES
            },
        }
        state["stages"]["intake"] = {
            "status": "passed",
            "summary": "Goal captured and delivery report initialized.",
            "evidence": [args.bead_id] if args.bead_id else [],
        }
    persist(args.state, state)
    return state


def update(args: argparse.Namespace) -> dict[str, Any]:
    state = load_state(args.state)
    if args.stage not in STAGES:
        raise ReportError(f"stage must be one of: {', '.join(STAGES)}")
    if args.status not in STATUSES:
        raise ReportError(f"status must be one of: {', '.join(sorted(STATUSES))}")
    state.setdefault("stages", {})[args.stage] = {
        "status": args.status,
        "summary": args.summary,
        "evidence": args.evidence,
    }
    for field in ("repo", "sha", "pr_url", "production_url", "next_action"):
        value = getattr(args, field)
        if value:
            state[field] = value
    no_smoke_reason = getattr(args, "no_smoke_reason", "")
    if no_smoke_reason:
        state["no_smoke_reason"] = no_smoke_reason
    persist(args.state, state)
    return state


def require_exact_rendered_bundle(state_path: Path, state: dict[str, Any]) -> None:
    report = state_path.parent / "index.html"
    stylesheet = state_path.parent / "styles.css"
    try:
        document = report.read_text(encoding="utf-8")
        css = stylesheet.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReportError(f"cannot read rendered report bundle: {exc}") from exc
    if document != render(state):
        raise ReportError("HTML report is stale or tampered; it is not the exact rendering of state")
    if css != CSS + "\n":
        raise ReportError("report stylesheet is stale or tampered")


def validate_final(args: argparse.Namespace) -> dict[str, Any]:
    state = load_state(args.state)
    # Reject a stale or modified public bundle before final validation can
    # rewrite it. This makes the existing state/render pair part of authority.
    require_exact_rendered_bundle(args.state, state)
    if not FULL_SHA.fullmatch(args.merge_sha):
        raise ReportError("merge SHA must be a full lowercase Git SHA")
    if args.deploy_status not in {"verified", "not_applicable"}:
        raise ReportError("deploy status must be verified or not_applicable")

    stages = state.get("stages") or {}
    # Every lifecycle stage remains mandatory unless it is the sole
    # deployment exception handled below.  Deriving this from STAGES keeps a
    # newly-added stage from silently bypassing final validation.
    for stage in (stage for stage in STAGES if stage != "deploy"):
        status = (stages.get(stage) or {}).get("status")
        if status != "passed":
            raise ReportError(f"final report stage {stage!r} must be passed (got {status!r})")
    deploy_stage = (stages.get("deploy") or {}).get("status")
    allowed_deploy_stages = (
        {"passed", "skipped"}
        if args.deploy_status == "not_applicable"
        else {"passed"}
    )
    if deploy_stage not in allowed_deploy_stages:
        raise ReportError(
            f"final report deploy stage is invalid for {args.deploy_status}: {deploy_stage!r}"
        )

    if state.get("sha") != args.merge_sha:
        raise ReportError("report delivery SHA does not match the verified merge SHA")
    if args.deploy_status == "verified" and args.deployed_sha != args.merge_sha:
        raise ReportError("verified deployed SHA does not match the merge SHA")
    if args.deploy_status == "not_applicable" and args.deployed_sha:
        raise ReportError("not_applicable deployment must not record a deployed SHA")
    if not safe_href(args.pr_url) or state.get("pr_url") != args.pr_url:
        raise ReportError("report pull-request URL is missing, unsafe, or stale")
    state_production_url = state.get("production_url") or ""
    if state_production_url != args.production_url:
        raise ReportError("report production URL is stale")
    if args.production_url and not safe_href(args.production_url):
        raise ReportError("report production URL is unsafe")

    source_id = getattr(args, "source_bead_id", "")
    source_title = getattr(args, "source_title", "")
    source_binding_required = bool(source_id or source_title)
    if source_binding_required:
        if not source_id or not source_title:
            raise ReportError("final report source ID and title must both be provided")
        if not isinstance(source_id, str) or not SOURCE_ID.fullmatch(source_id):
            raise ReportError("final report source ID is invalid")
        if any(ord(character) < 32 or ord(character) == 127 for character in source_title):
            raise ReportError("final report source title contains control characters")
        if state.get("bead_id") != source_id or state.get("source_bead_id") != source_id:
            raise ReportError("report source bead ID does not match durable workflow authority")
        if state.get("title") != source_title or state.get("source_title") != source_title:
            raise ReportError("report source title does not match durable workflow authority")

    no_smoke_required = bool(getattr(args, "require_no_smoke_reason", False))
    recorded_reason = getattr(args, "no_smoke_reason", "")
    expected_reason = getattr(args, "expected_no_smoke_reason", "")
    if no_smoke_required:
        if not recorded_reason.strip() or not expected_reason.strip():
            raise ReportError("a nonblank no-smoke reason is required for the final report")
        if recorded_reason != expected_reason:
            raise ReportError("delivery.no_smoke_reason does not match gc.var.no_smoke_reason")
        if state.get("no_smoke_reason") != recorded_reason:
            raise ReportError("living report does not expose the exact no-smoke reason")
    elif recorded_reason or expected_reason:
        raise ReportError("no-smoke reason is stale because a smoke exception is not required")
    elif state.get("no_smoke_reason") not in (None, ""):
        raise ReportError("living report contains a stale no-smoke reason")

    delivery = {
        "merge_sha": args.merge_sha,
        "deployed_sha": args.deployed_sha,
        "deploy_status": args.deploy_status,
        "pr_url": args.pr_url,
        "production_url": args.production_url,
        "no_smoke_required": no_smoke_required,
        "no_smoke_reason": recorded_reason if no_smoke_required else "",
    }
    state["delivery"] = delivery
    state["final_validation"] = {
        "schema": "gc.complete-delivery.final-validation.v1",
        "passed": True,
        **delivery,
        "source_binding_required": source_binding_required,
        "source_bead_id": source_id if source_binding_required else "",
        "source_title": source_title if source_binding_required else "",
        "validated_at": now(),
    }
    persist(args.state, state)
    if overall_status(state) != "live":
        raise ReportError("report top-line status is not live after final validation")
    require_exact_rendered_bundle(args.state, state)
    return state


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    init = subparsers.add_parser("init")
    init.add_argument("--state", required=True, type=Path)
    init.add_argument("--title", required=True)
    init.add_argument("--goal", required=True)
    init.add_argument("--repo", default="")
    init.add_argument("--bead-id", default="")

    change = subparsers.add_parser("update")
    change.add_argument("--state", required=True, type=Path)
    change.add_argument("--stage", required=True)
    change.add_argument("--status", required=True)
    change.add_argument("--summary", required=True)
    change.add_argument("--evidence", action="append", default=[])
    change.add_argument("--repo", default="")
    change.add_argument("--sha", default="")
    change.add_argument("--pr-url", default="")
    change.add_argument("--production-url", default="")
    change.add_argument("--next-action", default="")
    change.add_argument("--no-smoke-reason", default="")

    validate = subparsers.add_parser("validate")
    validate.add_argument("--state", required=True, type=Path)
    validate.add_argument("--merge-sha", required=True)
    validate.add_argument("--deployed-sha", default="")
    validate.add_argument(
        "--deploy-status", choices=("verified", "not_applicable"), required=True
    )
    validate.add_argument("--pr-url", required=True)
    validate.add_argument("--production-url", default="")
    validate.add_argument("--source-bead-id", default="")
    validate.add_argument("--source-title", default="")
    validate.add_argument("--no-smoke-reason", default="")
    validate.add_argument("--expected-no-smoke-reason", default="")
    validate.add_argument("--require-no-smoke-reason", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or [])
    try:
        if args.command == "init":
            state = initialize(args)
        elif args.command == "update":
            state = update(args)
        else:
            state = validate_final(args)
    except (OSError, ReportError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "state": str(args.state),
                "report": str(args.state.parent / "index.html"),
                "status": overall_status(state),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
