#!/usr/bin/env python3
"""Incrementally discover public GitHub repositories and import only policy-eligible software."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if not os.environ.get("APPHORYN_GITHUB_TOKEN") and os.environ.get("GITHUB_TOKEN"):
    os.environ["APPHORYN_GITHUB_TOKEN"] = os.environ["GITHUB_TOKEN"]

from app.config import settings
from app.build_support import build_adapter_revision, detect_supported_build_targets
from app.github_client import GitHubClient
from app.github_release_mirror import GitHubReleaseMirror
from app.repository_crawler import RepositoryCrawler
from app.services import CatalogService
from app.storefront_eligibility import has_installable_path


RETRY_DELAYS_SECONDS = (6 * 3600, 24 * 3600, 3 * 86400, 7 * 86400, 30 * 86400)

DEFAULT_GLOBAL_SCOUT_QUERIES = (
    "topic:android-app pushed:>2025-01-01",
    "topic:desktop-app pushed:>2025-01-01",
    "topic:linux-app pushed:>2025-01-01",
    "topic:windows-app pushed:>2025-01-01",
    "topic:macos-app pushed:>2025-01-01",
    "topic:command-line-tool pushed:>2025-01-01",
    "topic:productivity-app pushed:>2025-01-01",
    "topic:utility-app pushed:>2025-01-01",
    "topic:electron pushed:>2025-01-01",
    "topic:flutter pushed:>2025-01-01",
)


def configured_discovery_queries() -> list[str]:
    configured = [
        line.strip()
        for line in os.getenv("APPHORYN_GLOBAL_SCOUT_DISCOVERY_QUERIES", "").splitlines()
        if line.strip()
    ]
    return list(dict.fromkeys(configured or DEFAULT_GLOBAL_SCOUT_QUERIES))



def repo_key(value: str) -> str:
    return value.strip().rstrip("/").casefold().removesuffix(".git")


def load_catalog_index(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
        if not raw.strip():
            return []
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Apphoryn catalog index cannot be read safely: {exc}") from exc
    rows = payload.get("apps", []) if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        raise RuntimeError("Apphoryn catalog index has an invalid apps list.")
    return [item for item in rows if isinstance(item, dict)]


def load_retry_queue(path: Path, now: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not path.exists():
        return [], []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Scout retry queue cannot be read safely: {exc}") from exc
    if not isinstance(payload, list):
        raise RuntimeError("Scout retry queue must contain a JSON array.")
    due, waiting = [], []
    for row in payload:
        if not isinstance(row, dict) or not row.get("repository"):
            continue
        try:
            ready = float(row.get("next_attempt_at") or 0) <= now
        except (TypeError, ValueError):
            ready = True
        (due if ready else waiting).append(row)
    return due, waiting


def save_retry_queue(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def known_current(candidate: dict[str, Any], by_id: dict[str, dict], by_url: dict[str, dict], adapter_revision: str | None = None) -> bool:
    repository_id = str(candidate.get("id") or "")
    url = str(candidate.get("html_url") or candidate.get("repository_url") or "")
    existing = by_id.get(repository_id) if repository_id else None
    existing = existing or by_url.get(repo_key(url))
    if not existing or not existing.get("source_updated_at"):
        return False
    upstream_updated = str(candidate.get("updated_at") or "")
    indexed_updated = str(existing.get("source_updated_at") or "")
    if upstream_updated and upstream_updated > indexed_updated:
        return False
    if adapter_revision:
        attempts = existing.get("build_attempts") or []
        if attempts:
            latest = attempts[-1] if isinstance(attempts[-1], dict) else {}
            requested_targets = {str(value) for value in latest.get("targets", [])}
            available_targets = {
                str(item.get("platform")) for item in existing.get("artifacts", [])
                if isinstance(item, dict) and item.get("platform")
            }
            pending_targets = requested_targets - available_targets
            if pending_targets:
                if latest.get("adapter_revision") != adapter_revision:
                    return False
                try:
                    requested_at = datetime.fromisoformat(
                        str(latest.get("requested_at") or "").replace("Z", "+00:00")
                    )
                    if requested_at.tzinfo is None:
                        requested_at = requested_at.replace(tzinfo=timezone.utc)
                    age_hours = (
                        __import__("datetime").datetime.now(__import__("datetime").timezone.utc) - requested_at
                    ).total_seconds() / 3600
                    if age_hours >= 72:
                        return False
                except (TypeError, ValueError):
                    return False
    return True


def retry_record(entry: dict[str, Any], now: float, reason: str) -> dict[str, Any]:
    attempts = int(entry.get("attempts") or 0) + 1
    delay = RETRY_DELAYS_SECONDS[min(attempts - 1, len(RETRY_DELAYS_SECONDS) - 1)]
    return {
        "repository": str(entry["repository"]),
        "candidate": entry.get("candidate") or {},
        "reason": reason[:500],
        "attempts": attempts,
        "next_attempt_at": now + delay,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Apphoryn incremental public GitHub repository scanner.")
    parser.add_argument("--max-repositories", type=int, default=200)
    parser.add_argument("--max-pages", type=int, default=25)
    parser.add_argument("--state-file", default=str(ROOT / "data" / "scout-state.json"))
    parser.add_argument("--retry-file", default=str(ROOT / "data" / "scout-retry.json"))
    parser.add_argument("--catalog-file", default=str(ROOT / "apps" / "web" / "catalog" / "index.json"))
    parser.add_argument("--output", default="global-scout-results.json")
    args = parser.parse_args()

    token = os.getenv("GITHUB_TOKEN") or os.getenv("APPHORYN_GITHUB_TOKEN") or settings.github_token
    target_repository = os.getenv("GITHUB_REPOSITORY") or ""
    if not token or "/" not in target_repository:
        raise SystemExit("Global Scout requires GITHUB_TOKEN and GITHUB_REPOSITORY.")

    github = GitHubClient(token)
    catalog = CatalogService(github=github)
    mirror = GitHubReleaseMirror(token, target_repository, os.getenv("APPHORYN_GITHUB_REF", "main"))
    now = time.time()
    adapter_revision = build_adapter_revision()

    configured_targets = os.getenv("APPHORYN_GLOBAL_SCOUT_BUILD_TARGETS", "off").strip().lower()
    allowed_targets = {"android", "windows", "linux", "macos"}
    build_targets_requested = [] if configured_targets in {"", "off", "none", "disabled"} else sorted({
        part.strip() for part in configured_targets.split(",") if part.strip()
    })
    if build_targets_requested and not set(build_targets_requested).issubset(allowed_targets):
        raise SystemExit("APPHORYN_GLOBAL_SCOUT_BUILD_TARGETS contains unsupported targets.")
    build_budget = max(0, min(int(os.getenv("APPHORYN_GLOBAL_SCOUT_MAX_BUILDS_PER_RUN", "0")), 5))

    catalog_rows = load_catalog_index(Path(args.catalog_file))
    known_by_id = {
        str(row.get("github_repo_id")): row for row in catalog_rows if row.get("github_repo_id")
    }
    known_by_url = {
        repo_key(str(row.get("repository_url") or "")): row
        for row in catalog_rows if row.get("repository_url")
    }

    due_retries, waiting_retries = load_retry_queue(Path(args.retry_file), now)
    queue: list[dict[str, Any]] = []
    queued: set[str] = set()

    def enqueue(entry: dict[str, Any]) -> None:
        key = repo_key(str(entry.get("repository") or ""))
        if key and key not in queued:
            queue.append(entry)
            queued.add(key)

    for retry in due_retries:
        enqueue({**retry, "force_refresh": True, "reason": retry.get("reason") or "Scheduled retry"})

    # Search app-focused topics first. The public /repositories endpoint is
    # ordered by repository ID and would otherwise spend many cycles on old libraries.
    search_limit = max(1, min(int(os.getenv("APPHORYN_GLOBAL_SCOUT_SEARCH_LIMIT", "6")), 10))
    search_budget = max(1, min(int(os.getenv("APPHORYN_GLOBAL_SCOUT_MAX_DISCOVERY_PER_RUN", "60")), 100))
    targeted_count = 0
    for query in configured_discovery_queries():
        if targeted_count >= search_budget:
            break
        try:
            candidates = catalog.discover(query, search_limit)
        except Exception as exc:
            print(json.dumps({
                "event": "targeted_discovery_failed",
                "query": query,
                "error_type": type(exc).__name__,
            }), flush=True)
            continue
        for candidate in candidates:
            url = str(candidate.get("repository_url") or "")
            key = repo_key(url)
            if not key or key in queued:
                continue
            enqueue({
                "repository": url,
                "candidate": candidate,
                "reason": f"Targeted Apphoryn app discovery: {query}",
            })
            targeted_count += 1
            if targeted_count >= search_budget:
                break

    crawler = RepositoryCrawler(github, args.state_file)
    discovered, scan_report = crawler.scan(
        max_repositories=args.max_repositories,
        max_pages=args.max_pages,
        cursor_key="last_repository_id",
    )
    for candidate in discovered:
        url = str(candidate.get("html_url") or "")
        if url:
            enqueue({
                "repository": url,
                "candidate": candidate,
                "reason": "Incremental GitHub public repository listing",
            })

    refresh_report: dict[str, Any] | None = None
    if scan_report.get("reached_current_end"):
        refreshed, refresh_report = crawler.scan(
            max_repositories=args.max_repositories,
            max_pages=args.max_pages,
            cursor_key="last_refresh_repository_id",
        )
        for candidate in refreshed:
            url = str(candidate.get("html_url") or "")
            if url:
                enqueue({
                    "repository": url,
                    "candidate": candidate,
                    "force_refresh": True,
                    "reason": "Scheduled upstream release refresh",
                })
        if refresh_report.get("reached_current_end"):
            crawler.state["last_refresh_repository_id"] = 0
            crawler.state["totals"]["refresh_passes_completed"] = int(
                crawler.state["totals"].get("refresh_passes_completed", 0)
            ) + 1

    results: list[dict[str, Any]] = []
    scheduled_retries = list(waiting_retries)
    for entry in queue:
        repository_url = str(entry["repository"])
        candidate = entry.get("candidate") or {}
        result: dict[str, Any] = {
            "repository": repository_url,
            "reason": str(entry.get("reason") or ""),
            "status": "started",
        }
        try:
            if candidate.get("fork") is True:
                result.update({
                    "status": "fork_skipped",
                    "reason": "Fork repositories are scanned but not imported as duplicate app listings.",
                })
                results.append(result)
                continue
            if candidate.get("archived") is True or candidate.get("disabled") is True:
                result.update({
                    "status": "inactive_repository_skipped",
                    "reason": "Archived or disabled upstream repositories are not imported.",
                })
                results.append(result)
                continue
            existing = (
                known_by_id.get(str(candidate.get("id") or ""))
                or known_by_url.get(repo_key(repository_url))
            )
            if (
                not entry.get("force_refresh")
                and known_current(candidate, known_by_id, known_by_url, adapter_revision)
            ):
                result["status"] = "already_current"
                results.append(result)
                continue

            imported = catalog.import_repository(repository_url, mirror_release_assets=True)
            app = imported["app"]
            if candidate.get("id") and not app.get("github_repo_id"):
                app["github_repo_id"] = str(candidate["id"])
            if candidate.get("updated_at") and not app.get("source_updated_at"):
                app["source_updated_at"] = str(candidate["updated_at"])
            artifacts = imported.get("artifacts", [])
            available = mirror.get_available_platforms(repository_url)
            missing = sorted(set(build_targets_requested) - available) if build_targets_requested else []
            buildable = (
                app.get("project_kind") in {"application", "developer_tool"}
                and bool(app.get("build_recommended"))
            )
            repository_parts = repository_url.removeprefix("https://github.com/").strip("/").split("/")
            owner, repo_name = repository_parts[:2] if len(repository_parts) == 2 else ("", "")
            git_ref = str(app.get("source_commit_sha") or app.get("default_branch") or "main")
            supported = (
                detect_supported_build_targets(owner, repo_name, git_ref, github)
                if build_targets_requested and missing and buildable and owner and repo_name else {}
            )
            selected = sorted(set(missing) & set(supported))
            if not has_installable_path(app, artifacts, selected, available):
                result.update({
                    "status": "source_project_skipped",
                    "app": app.get("name"),
                    "project_kind": app.get("project_kind"),
                    "verified_release_packages": 0,
                    "build_status": "no_verified_release_or_supported_build",
                    "reason": (
                        "Repository metadata alone is not a store listing: no verified release "
                        "installer or supported app build adapter was found."
                    ),
                })
                results.append(result)
                print(json.dumps(result, ensure_ascii=False), flush=True)
                continue

            publication = mirror.publish_app(app, artifacts)
            result.update({
                "status": "mirrored" if artifacts else "source_available",
                "app": app.get("name"),
                "license": app.get("license_spdx"),
                "project_kind": app.get("project_kind"),
                "verified_release_packages": len(artifacts),
                "rejected_release_assets": imported.get("rejected_assets", []),
                "apphoryn_release": publication.get("release_url"),
                "project_status": publication.get("project_status"),
            })

            if build_targets_requested:
                if not missing:
                    result["build_status"] = "requested_platforms_already_available"
                elif not buildable:
                    result["build_status"] = "not_dispatched_by_project_classification"
                    result["build_targets_missing"] = missing
                elif not selected:
                    result["build_status"] = "no_supported_build_adapter"
                    result["build_targets_missing"] = missing
                    result["unsupported_build_targets"] = missing
                elif mirror.has_recent_build_attempt(repository_url, git_ref, targets=selected, adapter_revision=adapter_revision):
                    result["build_status"] = "already_queued_or_published"
                    result["build_targets"] = selected
                elif build_budget <= 0:
                    result["build_status"] = "deferred_by_zero_or_exhausted_budget"
                    result["build_targets"] = selected
                else:
                    mirror.dispatch_build(repository_url, git_ref, selected, str(app.get("slug") or app.get("id")), adapter_revision=adapter_revision)
                    build_budget -= 1
                    result["build_status"] = "workflow_dispatched"
                    result["build_targets"] = selected
                    result["build_adapters"] = {target: supported[target] for target in selected}
            else:
                result["build_status"] = "automatic_build_disabled"

            record = {
                "repository_url": app.get("repository_url") or repository_url,
                "github_repo_id": app.get("github_repo_id") or candidate.get("id"),
                "source_updated_at": app.get("source_updated_at") or candidate.get("updated_at"),
            }
            known_by_url[repo_key(str(record["repository_url"]))] = record
            if record.get("github_repo_id"):
                known_by_id[str(record["github_repo_id"])] = record
        except ValueError as exc:
            message = str(exc)
            if "license text could not be retrieved" in message.lower():
                scheduled_retries.append(retry_record(entry, now, message))
                result.update({"status": "retry_scheduled", "reason": message[:500]})
            else:
                result.update({"status": "rejected_by_policy", "reason": message[:500]})
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            scheduled_retries.append(retry_record(entry, now, message))
            result.update({"status": "failed_retry_scheduled", "reason": message[:500]})
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)

    deduplicated: dict[str, dict[str, Any]] = {}
    for row in scheduled_retries:
        candidate = row.get("candidate") or {}
        key = str(candidate.get("id") or repo_key(str(row.get("repository") or "")))
        if key:
            previous = deduplicated.get(key)
            if previous is None or int(row.get("attempts") or 0) > int(previous.get("attempts") or 0):
                deduplicated[key] = row

    crawler.save()
    save_retry_queue(Path(args.retry_file), list(deduplicated.values()))
    report = {
        "forward_scan": scan_report,
        "refresh_scan": refresh_report,
        "processed": len(results),
        "mirrored": sum(row.get("status") == "mirrored" for row in results),
        "source_only": sum(row.get("status") == "source_available" for row in results),
        "rejected": sum(row.get("status") == "rejected_by_policy" for row in results),
        "failed": sum(row.get("status") in {"failed_retry_scheduled", "retry_scheduled"} for row in results),
        "retry_queue_size": len(deduplicated),
    }
    Path(args.output).write_text(json.dumps({"report": report, "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
