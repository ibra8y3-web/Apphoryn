#!/usr/bin/env python3
"""Global GitHub repository migration worker with resumable, duplicate-safe state."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def github_target_parts(value: str) -> tuple[str, str] | None:
    parts = value.strip().split("/", 1)
    if len(parts) != 2 or not all(parts):
        return None
    return parts[0], parts[1]


if not os.environ.get("APPHORYN_GITHUB_TOKEN") and os.environ.get("GITHUB_TOKEN"):
    os.environ["APPHORYN_GITHUB_TOKEN"] = os.environ["GITHUB_TOKEN"]
if target := github_target_parts(os.environ.get("GITHUB_REPOSITORY", "")):
    os.environ.setdefault("APPHORYN_GITHUB_OWNER", target[0])
    os.environ.setdefault("APPHORYN_GITHUB_REPOSITORY", target[1])

from app.config import settings
from app.github_client import GitHubClient
from app.github_release_mirror import GitHubReleaseMirror
from app.repository_crawler import RepositoryCrawler
from app.services import CatalogService


RETRY_DELAYS_SECONDS = (6 * 3600, 24 * 3600, 3 * 86400, 7 * 86400, 30 * 86400)


def load_seed_repositories(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit("Seed repository file must contain a JSON array.")
    result = []
    for row in data:
        if isinstance(row, dict) and isinstance(row.get("repository"), str):
            result.append({"repository": row["repository"], "reason": str(row.get("reason") or "")})
    return result


def load_catalog_index(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    records = payload.get("apps", []) if isinstance(payload, dict) else []
    return [item for item in records if isinstance(item, dict)] if isinstance(records, list) else []


def load_retry_queue(path: Path, now: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], []
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Scout retry queue cannot be read safely: {exc}") from exc
    if not isinstance(payload, list):
        raise RuntimeError("Scout retry queue must contain a JSON array.")
    due, waiting = [], []
    for row in payload:
        if not isinstance(row, dict) or not row.get("repository"):
            continue
        (due if float(row.get("next_attempt_at") or 0) <= now else waiting).append(row)
    return due, waiting


def save_retry_queue(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _repo_key(value: str) -> str:
    return value.strip().rstrip("/").casefold().removesuffix(".git")


def known_current(candidate: dict[str, Any], known_by_id: dict[str, dict], known_by_url: dict[str, dict]) -> bool:
    repo_id = str(candidate.get("id") or "")
    url = str(candidate.get("html_url") or candidate.get("repository_url") or "")
    existing = known_by_id.get(repo_id) if repo_id else None
    existing = existing or known_by_url.get(_repo_key(url))
    if not existing or not existing.get("source_updated_at"):
        return False
    upstream_updated = str(candidate.get("updated_at") or "")
    indexed_updated = str(existing.get("source_updated_at") or "")
    return not upstream_updated or upstream_updated <= indexed_updated


def should_dispatch_build_fallback(
    verified_artifacts: list[dict[str, Any]],
    auto_targets: list[str],
    build_recommended: bool = False,
) -> bool:
    """Build only likely standalone apps/tools when no verified installer exists.

    A GitHub Release may contain only source archives or documentation. In that
    case the AI/heuristic project classifier decides whether a native build is
    useful; libraries and documentation remain in the source workbench.
    """
    return bool(auto_targets) and not verified_artifacts and bool(build_recommended)


def should_revisit_source_project(existing: dict[str, Any] | None, auto_targets: list[str]) -> bool:
    """Revisit a known buildable source-only project when native builds are enabled."""
    record = existing or {}
    return (
        bool(auto_targets)
        and not bool(record.get("artifacts"))
        and bool(record.get("build_recommended", False))
    )


def retry_record(entry: dict[str, Any], now: float, reason: str) -> dict[str, Any]:
    attempts = int(entry.get("attempts") or 0) + 1
    delay = RETRY_DELAYS_SECONDS[min(attempts - 1, len(RETRY_DELAYS_SECONDS) - 1)]
    return {
        "repository": entry["repository"],
        "candidate": entry.get("candidate") or {},
        "reason": reason[:500],
        "attempts": attempts,
        "next_attempt_at": now + delay,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Apphoryn global public-repository crawler and migration worker.")
    parser.add_argument("--global-scan", action="store_true", help="Continue the saved public repository ID cursor.")
    parser.add_argument("--query", default="", help="Optional additional GitHub search query.")
    parser.add_argument("--max-repositories", type=int, default=200)
    parser.add_argument("--max-pages", type=int, default=25)
    parser.add_argument("--state-file", default=str(ROOT / "data" / "scout-state.json"))
    parser.add_argument("--retry-file", default=str(ROOT / "data" / "scout-retry.json"))
    parser.add_argument("--seed-file", default=str(ROOT / "catalog-seed-repositories.json"))
    parser.add_argument("--catalog-file", default=str(ROOT / "apps" / "web" / "catalog" / "index.json"))
    parser.add_argument("--output", default="discovery-candidates.json")
    parser.add_argument("--skip-seeds", action="store_true")
    args = parser.parse_args()

    token = os.getenv("GITHUB_TOKEN") or os.getenv("APPHORYN_GITHUB_TOKEN") or settings.github_token
    target_repo = os.getenv("GITHUB_REPOSITORY", "")
    if not token or not target_repo:
        raise SystemExit("Real migration requires GITHUB_TOKEN and GITHUB_REPOSITORY.")

    github = GitHubClient(token)
    catalog = CatalogService(github=github)
    mirror = GitHubReleaseMirror(token, target_repo, os.getenv("APPHORYN_GITHUB_REF", "main"))
    now = time.time()
    known = load_catalog_index(Path(args.catalog_file))
    known_by_id = {str(item.get("github_repo_id")): item for item in known if item.get("github_repo_id")}
    known_by_url = {_repo_key(str(item.get("repository_url") or "")): item for item in known if item.get("repository_url")}

    due_retries, waiting_retries = load_retry_queue(Path(args.retry_file), now)
    seeds = [] if args.skip_seeds else load_seed_repositories(Path(args.seed_file))
    queue: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    def enqueue(entry: dict[str, Any]) -> None:
        key = _repo_key(str(entry.get("repository") or ""))
        if key and key not in seen_keys:
            queue.append(entry)
            seen_keys.add(key)

    for item in due_retries:
        enqueue({**item, "reason": item.get("reason") or "Scheduled retry", "force_refresh": True})
    for seed in seeds:
        enqueue({"repository": seed["repository"], "reason": seed.get("reason", "Maintained seed")})

    crawl_report: dict[str, Any] = {
        "repositories": 0, "repository_pages": 0, "last_repository_id": 0,
        "last_refresh_repository_id": 0, "scan_phase": "new",
    }
    crawler = None
    if args.global_scan:
        crawler = RepositoryCrawler(github, args.state_file)
        discovered, new_report = crawler.scan(
            args.max_repositories, args.max_pages, cursor_key="last_repository_id"
        )
        crawl_report.update(new_report)
        crawl_report["scan_phase"] = "new"
        for candidate in discovered:
            url = str(candidate.get("html_url") or "")
            enqueue({
                "repository": url,
                "reason": "Incremental GitHub public-repository listing",
                "candidate": candidate,
            })

        # Once the forward scan reaches the current tail, run a second
        # independent cursor from the beginning. It revisits old repositories
        # to discover new releases without storing every source-only project
        # in the public catalogue or generating empty releases.
        if new_report.get("reached_current_end"):
            refresh_rows, refresh_report = crawler.scan(
                args.max_repositories, args.max_pages, cursor_key="last_refresh_repository_id"
            )
            crawl_report["refresh"] = refresh_report
            crawl_report["scan_phase"] = "refresh"
            for candidate in refresh_rows:
                url = str(candidate.get("html_url") or "")
                enqueue({
                    "repository": url,
                    "reason": "Scheduled full-catalogue upstream release recheck",
                    "candidate": candidate,
                    "force_refresh": True,
                })
            if refresh_report.get("reached_current_end"):
                crawler.state["last_refresh_repository_id"] = 0
                crawler.state["totals"]["refresh_passes_completed"] = int(
                    crawler.state["totals"].get("refresh_passes_completed", 0)
                ) + 1
    elif args.query:
        for candidate in catalog.discover(args.query, max(1, min(args.max_repositories, 100))):
            url = str(candidate.get("repository_url") or "")
            enqueue({"repository": url, "reason": "Explicit GitHub repository search", "candidate": candidate})

    Path(args.output).write_text(json.dumps([{
        "repository": row["repository"], "reason": row.get("reason", "")
    } for row in queue], ensure_ascii=False, indent=2), encoding="utf-8")

    configured = os.getenv("APPHORYN_AUTO_BUILD_TARGETS", "off").strip()
    auto_targets = [] if configured.lower() in {"off", "none", "disabled", ""} else sorted({
        part.strip() for part in configured.split(",") if part.strip()
    })
    allowed_targets = {"android", "windows", "linux", "macos"}
    if auto_targets and not set(auto_targets).issubset(allowed_targets):
        raise SystemExit("APPHORYN_AUTO_BUILD_TARGETS contains unsupported targets.")

    results: list[dict[str, Any]] = []
    scheduled_retries = list(waiting_retries)
    for entry in queue:
        repo_url = entry["repository"]
        candidate = entry.get("candidate") or {}
        result: dict[str, Any] = {"repository": repo_url, "reason": entry.get("reason", ""), "status": "started"}
        try:
            if candidate.get("fork") is True:
                result.update({"status": "duplicate_fork_skipped", "reason": "Fork repositories are scanned but not imported as duplicate app listings."})
                results.append(result)
                continue
            if candidate.get("archived") is True or candidate.get("disabled") is True:
                result.update({"status": "inactive_repository_skipped", "reason": "Archived or disabled upstream repositories are not imported."})
                results.append(result)
                continue
            existing_record = (
                known_by_id.get(str(candidate.get("id") or ""))
                or known_by_url.get(_repo_key(repo_url))
            )
            if (
                not entry.get("force_refresh")
                and known_current(candidate, known_by_id, known_by_url)
                and not should_revisit_source_project(existing_record, auto_targets)
            ):
                result["status"] = "already_current"
                results.append(result)
                continue

            imported = catalog.import_repository(repo_url, mirror_release_assets=True)
            app = imported["app"]
            app["github_repo_id"] = app.get("github_repo_id") or str(candidate.get("id") or "") or None
            app["source_updated_at"] = app.get("source_updated_at") or candidate.get("updated_at")
            verified_artifacts = imported.get("artifacts", [])
            result.update({
                "app": app.get("name"),
                "version": app.get("latest_version"),
                "license": app.get("license_spdx"),
                "verified_release_packages": len(verified_artifacts),
                "rejected_release_assets": imported.get("rejected_assets", []),
            })
            if verified_artifacts:
                mirror_result = mirror.publish_app(app, verified_artifacts)
                result["status"] = "mirrored"
                result["apphoryn_release"] = mirror_result.get("release_url")
                result["mirrored_files"] = [item["filename"] for item in mirror_result["mirrored_artifacts"]]
                key = _repo_key(str(app.get("repository_url") or repo_url))
                known_record = {
                    "repository_url": app.get("repository_url") or repo_url,
                    "github_repo_id": app.get("github_repo_id"),
                    "source_updated_at": app.get("source_updated_at"),
                    "scout_checked_at": mirror_result.get("scout_checked_at"),
                }
                known_by_url[key] = known_record
                if app.get("github_repo_id"):
                    known_by_id[str(app["github_repo_id"])] = known_record
            else:
                # Keep source-only projects in the project/workbench layer without
                # presenting them as installable apps in the public storefront.
                result["project"] = mirror.register_project(app)
                existing = known_by_id.get(str(app.get("github_repo_id") or "")) or known_by_url.get(_repo_key(repo_url))
                if existing:
                    mirror.mark_app_checked(str(app.get("repository_url") or repo_url), app.get("github_repo_id"))
                    result["status"] = "checked_no_new_installer"
                else:
                    if imported.get("release_found"):
                        scheduled_retries.append(retry_record(entry, now, "Release exists but no supported verified installer was accepted."))
                        result["status"] = "awaiting_verified_installer"
                    else:
                        result["status"] = "source_only_scanned"
                # If an upstream release exists but contains no usable installer, build
                # from source rather than repeatedly waiting for a package that is absent.
                if should_dispatch_build_fallback(verified_artifacts, auto_targets, bool(app.get("build_recommended", False))):
                    git_ref = str(app.get("source_commit_sha") or app.get("default_branch") or "main")
                    if not mirror.has_recent_build_attempt(repo_url, git_ref):
                        mirror.dispatch_build(repo_url, git_ref, auto_targets, str(app.get("slug") or app.get("id")))
                        result["build_status"] = "workflow_dispatched"
                        result["build_targets"] = auto_targets
                    else:
                        result["build_status"] = "already_queued_or_published"
                result["reason"] = "Project source metadata is retained in Apphoryn; only verified installable packages appear in the store."
        except ValueError as exc:
            result.update({"status": "rejected_by_policy", "reason": str(exc)[:700]})
        except Exception as exc:
            result.update({"status": "failed", "reason": str(exc)[:700]})
            scheduled_retries.append(retry_record(entry, now, str(exc)))
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)

    # Keep one retry record per immutable upstream ID or canonical URL.
    retry_deduplicated: dict[str, dict[str, Any]] = {}
    for item in scheduled_retries:
        candidate = item.get("candidate") or {}
        key = str(candidate.get("id") or _repo_key(str(item.get("repository") or "")))
        if key:
            previous = retry_deduplicated.get(key)
            if previous is None or float(item.get("next_attempt_at") or 0) < float(previous.get("next_attempt_at") or 0):
                retry_deduplicated[key] = item
    save_retry_queue(Path(args.retry_file), list(retry_deduplicated.values()))

    if crawler:
        crawler.save()
        Path("scout-report.json").write_text(json.dumps(crawl_report, ensure_ascii=False, indent=2), encoding="utf-8")

    Path("harvest-results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    mirrored_count = sum(item.get("status") == "mirrored" for item in results)
    current_count = sum(item.get("status") in {"already_current", "checked_no_new_installer"} for item in results)
    rejected_count = sum(item.get("status") in {"rejected_by_policy", "duplicate_fork_skipped", "inactive_repository_skipped"} for item in results)
    failed_count = sum(item.get("status") == "failed" for item in results)
    print(
        "Scout run finished: "
        f"{crawl_report.get('repositories', 0)} public repositories scanned in the {crawl_report.get('scan_phase', 'discovery')} phase; "
        f"{len(results)} migration tasks processed; {mirrored_count} apps with verified installers mirrored; "
        f"{current_count} current; {rejected_count} skipped by policy/deduplication; "
        f"{failed_count} failed; {len(retry_deduplicated)} retry records retained; "
        f"last repository ID={crawl_report.get('last_repository_id', 0)}."
    )


if __name__ == "__main__":
    main()
