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
from app.import_pacing import ImportPacer
from app.github_actions_client import call_apphoryn_internal_api


RETRY_DELAYS_SECONDS = (6 * 3600, 24 * 3600, 3 * 86400, 7 * 86400, 30 * 86400)

DEFAULT_GLOBAL_SCOUT_QUERIES = (
    "topic:android-app",
    "topic:desktop-app",
    "topic:linux-app",
    "topic:windows-app",
    "topic:macos-app",
    "topic:command-line-tool",
    "topic:productivity-app",
    "topic:utility-app",
    "topic:electron",
    "topic:flutter",
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


def has_arabic_translation(app: dict[str, Any]) -> bool:
    translations = app.get("translations") if isinstance(app.get("translations"), dict) else {}
    descriptions = (
        app.get("description_translations")
        if isinstance(app.get("description_translations"), dict) else {}
    )
    arabic_value = translations.get("ar")
    arabic_summary = (
        str(arabic_value.get("summary") or "").strip()
        if isinstance(arabic_value, dict) else str(arabic_value or "").strip()
    )
    arabic_description = str(descriptions.get("ar") or "").strip()
    if not arabic_description and isinstance(arabic_value, dict):
        arabic_description = str(arabic_value.get("description") or "").strip()
    return bool(
        str(app.get("translation_status") or "").lower() == "complete"
        and arabic_summary
        and arabic_description
    )


def localize_existing_catalog_rows(
    rows: list[dict[str, Any]],
    mirror: GitHubReleaseMirror,
    *,
    interval_seconds: float = 1.0,
    deadline: float | None = None,
) -> dict[str, int]:
    """Translate published descriptions in place, without rewriting source wording."""
    pacer = ImportPacer(interval_seconds)
    report = {"checked": 0, "translated": 0, "failed": 0, "deferred": 0}
    for app in rows:
        if has_arabic_translation(app):
            continue
        description = str(app.get("description") or "").strip()
        if not description:
            continue
        report["checked"] += 1
        if not pacer.wait_for_slot(deadline):
            report["deferred"] += 1
            break
        try:
            localized = call_apphoryn_internal_api(
                "/api/v1/internal/github-actions/catalog-translation",
                method="POST",
                payload={
                    "summary": str(app.get("summary") or ""),
                    "description": description,
                    "translations": app.get("translations") or {},
                    "description_translations": app.get("description_translations") or {},
                },
            )
            if not has_arabic_translation(localized):
                raise ValueError("Arabic summary and description were not both returned.")
            updated = {
                **app,
                "summary": str(app.get("summary") or localized.get("summary") or ""),
                "description": description,
                "translations": localized.get("translations") or {},
                "description_translations": localized.get("description_translations") or {},
                "translation_status": "complete",
            }
            mirror.publish_app(updated, [])
            app.update({
                "translations": updated["translations"],
                "description_translations": updated["description_translations"],
                "translation_status": "complete",
            })
            report["translated"] += 1
            print(json.dumps({
                "event": "existing_catalog_description_translated",
                "app": str(app.get("name") or app.get("slug") or ""),
                "status": "complete",
            }, ensure_ascii=False), flush=True)
        except Exception as exc:
            report["failed"] += 1
            message = f"{type(exc).__name__}: {exc}"
            print(json.dumps({
                "event": "existing_catalog_translation_failed",
                "app": str(app.get("name") or app.get("slug") or ""),
                "error": message[:300],
            }, ensure_ascii=False), flush=True)
            # Provider/API failures are unlikely to improve by retrying thousands
            # of rows in the same run. The next scheduled run will retry them.
            if any(token in message for token in ("HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504", "URLError")):
                report["deferred"] += 1
                break
    return report


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
    parser.add_argument("--max-repositories", type=int, default=10000, help="Resumable checkpoint batch size; not a total import quota.")
    parser.add_argument("--max-pages", type=int, default=20, help="Internal checkpoint batch size; repeated until the run window ends.")
    parser.add_argument("--run-budget-seconds", type=int, default=4500, help="Time available for scanning/importing before the runner timeout.")
    parser.add_argument(
        "--import-interval-seconds",
        type=float,
        default=float(os.getenv("APPHORYN_IMPORT_INTERVAL_SECONDS", "60")),
        help="Minimum seconds between import starts; set to 60 for one import start per minute.",
    )
    parser.add_argument(
        "--translation-interval-seconds",
        type=float,
        default=float(os.getenv("APPHORYN_TRANSLATION_INTERVAL_SECONDS", "1")),
        help="Minimum seconds between existing-catalog translation requests.",
    )
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

    def enqueue(entry: dict[str, Any], target: list[dict[str, Any]] | None = None) -> None:
        key = repo_key(str(entry.get("repository") or ""))
        if key and key not in queued:
            (target if target is not None else queue).append(entry)
            queued.add(key)

    for retry in due_retries:
        enqueue({**retry, "force_refresh": True, "reason": retry.get("reason") or "Scheduled retry"})

    # Search app-focused topics first. The public /repositories endpoint is
    # ordered by repository ID and would otherwise spend many cycles on old libraries.
    search_limit = max(1, min(int(os.getenv("APPHORYN_GLOBAL_SCOUT_SEARCH_LIMIT", "100")), 100))
    for query in configured_discovery_queries():
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

    last_discovery_refresh = time.monotonic()

    def refresh_targeted_discovery(target: list[dict[str, Any]]) -> None:
        nonlocal last_discovery_refresh
        if time.monotonic() - last_discovery_refresh < 60:
            return
        last_discovery_refresh = time.monotonic()
        for query in configured_discovery_queries():
            try:
                candidates = catalog.discover(query, search_limit)
            except Exception as exc:
                print(json.dumps({
                    "event": "targeted_discovery_refresh_failed",
                    "query": query,
                    "error_type": type(exc).__name__,
                }), flush=True)
                continue
            for candidate in candidates:
                url = str(candidate.get("repository_url") or "")
                if url:
                    enqueue({
                        "repository": url,
                        "candidate": candidate,
                        "reason": f"Minute discovery refresh: {query}",
                    }, target)

    crawler = RepositoryCrawler(github, args.state_file)
    scan_report: dict[str, Any] | None = None
    refresh_report: dict[str, Any] | None = None
    results: list[dict[str, Any]] = []
    scheduled_retries = list(waiting_retries)
    deadline = time.monotonic() + max(60, int(args.run_budget_seconds))
    localization_report = localize_existing_catalog_rows(
        catalog_rows,
        mirror,
        interval_seconds=args.translation_interval_seconds,
        deadline=deadline,
    )
    import_pacer = ImportPacer(args.import_interval_seconds)
    processed_this_run: set[str] = set()

    def process_entries(entries: list[dict[str, Any]]) -> bool:
        for index, entry in enumerate(entries):
            refresh_targeted_discovery(entries)
            if time.monotonic() >= deadline:
                for pending in entries[index:]:
                    scheduled_retries.append({**pending, "next_attempt_at": now})
                return False
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

                import_key = repo_key(repository_url)
                if import_key in processed_this_run:
                    result["status"] = "already_processed_this_run"
                    results.append(result)
                    continue
                if not import_pacer.wait_for_slot(deadline):
                    for pending in entries[index:]:
                        scheduled_retries.append({**pending, "next_attempt_at": now})
                    return False
                processed_this_run.add(import_key)
                imported = catalog.import_repository(repository_url, mirror_release_assets=True)
                app = imported["app"]
                result["ai_generation"] = imported.get("ai_generation") or {}
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
                project_kind = str(app.get("project_kind") or "unknown")
                is_app_project = (
                    project_kind in {"application", "developer_tool"}
                    or bool(artifacts)
                    or bool(available)
                )
                if not is_app_project:
                    result.update({
                        "status": "non_app_project_skipped",
                        "app": app.get("name"),
                        "project_kind": project_kind,
                        "reason": "The repository was not verified as an application/developer tool and has no verified installer.",
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
                    else:
                        # Dispatch all supported missing targets. No Apphoryn per-run quota.
                        mirror.dispatch_build(repository_url, git_ref, selected, str(app.get("slug") or app.get("id")), adapter_revision=adapter_revision)
                        result["build_status"] = "workflow_dispatched"
                        result["build_targets"] = selected
                        result["build_adapters"] = {target: supported[target] for target in selected}
                else:
                    result["build_status"] = "automatic_build_disabled"

                record = {
                    "repository_url": app.get("repository_url") or repository_url,
                    "github_repo_id": app.get("github_repo_id") or candidate.get("id"),
                    "source_updated_at": app.get("source_updated_at") or candidate.get("updated_at"),
                    "translation_status": app.get("translation_status") or "missing",
                    "translations": app.get("translations") or {},
                    "description_translations": app.get("description_translations") or {},
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

        return True

    def persist_checkpoint() -> int:
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
        return len(deduplicated)

    # Process retries and app-focused search results before advancing the global cursor.
    completed = process_entries(queue)
    retry_queue_size = persist_checkpoint()
    cursor_key = "last_repository_id"
    crawl_batches = 0

    # No total app/repository quota: continue checkpoint batches until the hosted
    # runner's time window ends, then resume from the last fully processed batch.
    while completed and time.monotonic() < deadline:
        state_before_batch = json.loads(json.dumps(crawler.state))
        discovered, current_report = crawler.scan(
            max_repositories=max(1, min(int(args.max_repositories), 10000)),
            max_pages=max(1, min(int(args.max_pages), 100)),
            cursor_key=cursor_key,
        )
        crawl_batches += 1
        batch_entries: list[dict[str, Any]] = []
        for candidate in discovered:
            url = str(candidate.get("html_url") or "")
            if url:
                enqueue({
                    "repository": url,
                    "candidate": candidate,
                    "force_refresh": cursor_key == "last_refresh_repository_id",
                    "reason": (
                        "Incremental GitHub public repository listing"
                        if cursor_key == "last_repository_id"
                        else "Scheduled upstream release refresh"
                    ),
                }, batch_entries)

        completed = process_entries(batch_entries)
        if not completed:
            # Commit the cursor only after every repository in the batch is handled.
            # Replaying the whole batch is safe because imports/build dispatches deduplicate.
            crawler.state = state_before_batch
            retry_queue_size = persist_checkpoint()
            break

        if cursor_key == "last_repository_id":
            scan_report = current_report
        else:
            refresh_report = current_report

        retry_queue_size = persist_checkpoint()
        if current_report.get("reached_current_end"):
            if cursor_key == "last_repository_id":
                cursor_key = "last_refresh_repository_id"
                continue

            crawler.state["last_refresh_repository_id"] = 0
            crawler.state["totals"]["refresh_passes_completed"] = int(
                crawler.state["totals"].get("refresh_passes_completed", 0)
            ) + 1
            retry_queue_size = persist_checkpoint()
            break

    report = {
        "forward_scan": scan_report,
        "refresh_scan": refresh_report,
        "crawl_batches": crawl_batches,
        "run_budget_seconds": max(60, int(args.run_budget_seconds)),
        "import_interval_seconds": import_pacer.interval_seconds,
        "existing_catalog_localization": localization_report,
        "run_time_window_reached": time.monotonic() >= deadline,
        "processed": len(results),
        "mirrored": sum(row.get("status") == "mirrored" for row in results),
        "source_only": sum(row.get("status") == "source_available" for row in results),
        "rejected": sum(row.get("status") == "rejected_by_policy" for row in results),
        "failed": sum(row.get("status") in {"failed_retry_scheduled", "retry_scheduled"} for row in results),
        "retry_queue_size": retry_queue_size,
    }
    Path(args.output).write_text(json.dumps({"report": report, "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
