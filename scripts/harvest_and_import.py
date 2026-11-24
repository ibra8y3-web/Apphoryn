#!/usr/bin/env python3
"""Incrementally crawl public GitHub repositories and import eligible real releases.

The crawl cursor is persisted after a complete batch. Repository IDs, stable
catalogue upserts and content hashes make retries/renames idempotent.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
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
    # Seed records do not include timestamps; once indexed, the crawler remains
    # responsible for detecting subsequent upstream changes.
    return not upstream_updated or upstream_updated <= indexed_updated


def main() -> None:
    parser = argparse.ArgumentParser(description="Apphoryn global GitHub Scout and migration worker.")
    parser.add_argument("--global-scan", action="store_true", help="Continue the saved chronological GitHub crawl.")
    parser.add_argument("--query", default="", help="Optional additional GitHub search query.")
    parser.add_argument("--max-repositories", type=int, default=200)
    parser.add_argument("--max-search-requests", type=int, default=25)
    parser.add_argument("--state-file", default=str(ROOT / "data" / "scout-state.json"))
    parser.add_argument("--seed-file", default=str(ROOT / "catalog-seed-repositories.json"))
    parser.add_argument("--catalog-file", default=str(ROOT / "apps" / "web" / "catalog" / "index.json"))
    parser.add_argument("--output", default="discovery-candidates.json")
    parser.add_argument("--skip-seeds", action="store_true")
    args = parser.parse_args()

    token = os.getenv("GITHUB_TOKEN") or os.getenv("APPHORYN_GITHUB_TOKEN") or settings.github_token
    target_repo = os.getenv("GITHUB_REPOSITORY", "")
    if not token or not target_repo:
        raise SystemExit(
            "Real migration requires GITHUB_TOKEN and GITHUB_REPOSITORY. "
            "No app is advertised or marked imported without the Apphoryn mirror."
        )

    github = GitHubClient(token)
    catalog = CatalogService(github=github)
    mirror = GitHubReleaseMirror(token, target_repo, os.getenv("APPHORYN_GITHUB_REF", "main"))
    known = load_catalog_index(Path(args.catalog_file))
    known_by_id = {str(item.get("github_repo_id")): item for item in known if item.get("github_repo_id")}
    known_by_url = {_repo_key(str(item.get("repository_url") or "")): item for item in known if item.get("repository_url")}

    seeds = [] if args.skip_seeds else load_seed_repositories(Path(args.seed_file))
    queue: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for seed in seeds:
        key = _repo_key(seed["repository"])
        if key and key not in seen_urls:
            queue.append({"repository": seed["repository"], "reason": seed.get("reason", "Maintained seed")})
            seen_urls.add(key)

    crawl_report: dict[str, Any] = {"repositories": 0, "search_requests": 0, "pending_partitions": 0}
    crawler = None
    if args.global_scan:
        crawler = RepositoryCrawler(github, args.state_file)
        discovered, crawl_report = crawler.scan(args.max_repositories, args.max_search_requests)
        for candidate in discovered:
            url = str(candidate.get("html_url") or "")
            key = _repo_key(url)
            if key and key not in seen_urls:
                queue.append({
                    "repository": url,
                    "reason": "Incremental global GitHub repository crawl",
                    "candidate": candidate,
                })
                seen_urls.add(key)
    elif args.query:
        for candidate in catalog.discover(args.query, max(1, min(args.max_repositories, 100))):
            url = str(candidate.get("repository_url") or "")
            key = _repo_key(url)
            if key and key not in seen_urls:
                queue.append({"repository": url, "reason": "Explicit GitHub search query", "candidate": candidate})
                seen_urls.add(key)

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
    for entry in queue:
        repo_url = entry["repository"]
        candidate = entry.get("candidate") or {}
        result: dict[str, Any] = {"repository": repo_url, "reason": entry.get("reason", ""), "status": "started"}
        try:
            if known_current(candidate, known_by_id, known_by_url):
                result["status"] = "already_current"
                results.append(result)
                print(json.dumps(result, ensure_ascii=False), flush=True)
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
            else:
                mirror.publish_app(app, [])
                result["status"] = "source_only_not_published"
                if auto_targets:
                    git_ref = str(app.get("source_commit_sha") or app.get("default_branch") or "main")
                    if mirror.has_recent_build_attempt(repo_url, git_ref):
                        result["build_status"] = "already_dispatched_or_built"
                    else:
                        mirror.dispatch_build(repo_url, git_ref, auto_targets, str(app.get("slug") or app.get("id")))
                        result["build_status"] = "workflow_dispatched"
                        result["build_targets"] = auto_targets
                else:
                    result["reason"] = "No verified installer found; source-only entries remain hidden from the public store."
            key = _repo_key(str(app.get("repository_url") or repo_url))
            known_by_url[key] = {
                "repository_url": app.get("repository_url") or repo_url,
                "github_repo_id": app.get("github_repo_id"),
                "source_updated_at": app.get("source_updated_at"),
            }
            if app.get("github_repo_id"):
                known_by_id[str(app["github_repo_id"])] = known_by_url[key]
        except ValueError as exc:
            # Missing/unsupported licenses, archived projects and unavailable
            # license texts are permanent policy rejections, not retry loops.
            result.update({"status": "rejected_by_policy", "reason": str(exc)[:700]})
        except Exception as exc:
            result.update({"status": "failed", "reason": str(exc)[:700]})
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)

    if crawler:
        crawler.save()
        Path("scout-report.json").write_text(json.dumps(crawl_report, ensure_ascii=False, indent=2), encoding="utf-8")

    Path("harvest-results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    mirrored_count = sum(item.get("status") == "mirrored" for item in results)
    current_count = sum(item.get("status") == "already_current" for item in results)
    rejected_count = sum(item.get("status") == "rejected_by_policy" for item in results)
    failed_count = sum(item.get("status") == "failed" for item in results)
    print(
        "Scout batch finished: "
        f"{crawl_report.get('repositories', 0)} repositories discovered; "
        f"{len(results)} migration candidates processed; {mirrored_count} mirrored; "
        f"{current_count} unchanged; {rejected_count} policy-rejected; {failed_count} failed; "
        f"{crawl_report.get('pending_partitions', 0)} partitions remain."
    )


if __name__ == "__main__":
    main()
