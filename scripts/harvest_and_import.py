#!/usr/bin/env python3
"""Discover software, import verified release packages, and dispatch safe builds for supported apps.

This worker is intended for scheduled GitHub Actions execution. It never executes
third-party source code in the trusted Scout job. Source builds run in the isolated
build-community-app workflow without Apphoryn secrets.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

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
from app.build_support import detect_supported_build_targets
from app.github_client import GitHubClient
from app.github_release_mirror import GitHubMirrorError, GitHubReleaseMirror
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


def configured_queries(primary: str) -> list[str]:
    configured = [line.strip() for line in os.getenv("APPHORYN_DISCOVERY_QUERIES", "").splitlines() if line.strip()]
    result = []
    for query in [primary, *configured]:
        if query and query not in result:
            result.append(query)
    return result or ["topic:opensource"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", default=os.getenv("APPHORYN_QUERY", "topic:opensource"))
    parser.add_argument("--limit", type=int, default=10, help="Maximum search results per query (1-10).")
    parser.add_argument("--seed-file", default=str(ROOT / "catalog-seed-repositories.json"))
    parser.add_argument("--output", default="discovery-candidates.json")
    parser.add_argument("--skip-seeds", action="store_true")
    args = parser.parse_args()

    limit = max(1, min(args.limit, 10))
    max_candidates = max(1, min(int(os.getenv("APPHORYN_MAX_DISCOVERY_PER_RUN", "30")), 100))
    catalog = CatalogService()

    # Search several app-focused topic families without a stars threshold. Recently
    # updated small and specialized apps remain eligible; popularity is not a filter.
    candidates_by_repo: dict[str, dict[str, object]] = {}
    queries = configured_queries(args.query)
    for query in queries:
        try:
            found = catalog.discover(query, limit)
        except Exception as exc:
            print(json.dumps({"query": query, "status": "search_failed", "reason": str(exc)[:300]}, ensure_ascii=False), flush=True)
            continue
        for item in found:
            repository = str(item.get("repository_url") or "").rstrip("/").lower()
            if repository and repository not in candidates_by_repo:
                candidates_by_repo[repository] = {**item, "discovery_query": query}
        if len(candidates_by_repo) >= max_candidates:
            break
    candidates = list(candidates_by_repo.values())[:max_candidates]
    Path(args.output).write_text(json.dumps(candidates, ensure_ascii=False, indent=2), encoding="utf-8")

    token = os.getenv("GITHUB_TOKEN") or os.getenv("APPHORYN_GITHUB_TOKEN") or settings.github_token
    target_repo = os.getenv("GITHUB_REPOSITORY", "")
    if not token or not target_repo:
        raise SystemExit(
            "Real software mirroring requires GITHUB_TOKEN and GITHUB_REPOSITORY. "
            "No app was advertised or marked imported. Enable Actions Contents: write."
        )
    mirror = GitHubReleaseMirror(token, target_repo, os.getenv("APPHORYN_GITHUB_REF", "main"))

    queue: list[dict[str, str]] = []
    if not args.skip_seeds:
        queue.extend(load_seed_repositories(Path(args.seed_file)))
    seen = {row["repository"].rstrip("/").lower() for row in queue}
    for item in candidates:
        repo_url = str(item.get("repository_url") or "")
        key = repo_url.rstrip("/").lower()
        if repo_url and key not in seen:
            queue.append({"repository": repo_url, "reason": f"Discovered by {item.get('discovery_query') or 'GitHub repository search'}."})
            seen.add(key)

    configured_targets = os.getenv("APPHORYN_AUTO_BUILD_TARGETS", "android,windows,linux,macos").strip()
    auto_targets = [] if configured_targets.lower() in {"off", "none", "disabled"} else sorted({
        part.strip().lower() for part in configured_targets.split(",") if part.strip()
    })
    allowed_targets = {"android", "windows", "linux", "macos"}
    if auto_targets and not set(auto_targets).issubset(allowed_targets):
        raise SystemExit("APPHORYN_AUTO_BUILD_TARGETS contains unsupported targets.")
    build_budget = max(0, min(int(os.getenv("APPHORYN_MAX_AUTO_BUILDS_PER_RUN", "3")), 20))

    results: list[dict[str, object]] = []
    for entry in queue:
        repo_url = entry["repository"]
        result: dict[str, object] = {
            "repository": repo_url,
            "seed_reason": entry.get("reason", ""),
            "status": "started",
        }
        try:
            imported = catalog.import_repository(repo_url, mirror_release_assets=True)
            app = imported["app"]
            verified_artifacts = imported.get("artifacts", [])
            result.update({
                "app": app.get("name"),
                "version": app.get("latest_version"),
                "license": app.get("license_spdx"),
                "project_kind": app.get("project_kind"),
                "build_recommended": bool(app.get("build_recommended")),
                "ai_provider": app.get("ai_provider"),
                "ai_model": app.get("ai_model"),
                "translation_status": app.get("translation_status", "missing"),
                "verified_release_packages": len(verified_artifacts),
                "rejected_release_assets": imported.get("rejected_assets", []),
            })

            if verified_artifacts:
                mirror_result = mirror.publish_app(app, verified_artifacts)
                result["status"] = "mirrored"
                result["apphoryn_release"] = mirror_result.get("release_url")
                result["mirrored_files"] = [item["filename"] for item in mirror_result["mirrored_artifacts"]]
            else:
                source_result = mirror.publish_app(app, [])
                result["status"] = "source_available"
                result["project_status"] = source_result.get("project_status", "source_available")

            if auto_targets:
                already_available = mirror.get_available_platforms(repo_url)
                missing_targets = sorted(set(auto_targets) - already_available)
                is_buildable_app = (
                    app.get("project_kind") in {"application", "developer_tool"}
                    and bool(app.get("build_recommended"))
                )
                if not missing_targets:
                    result["build_status"] = "all_requested_platforms_available"
                elif not is_buildable_app:
                    result["build_status"] = "not_dispatched"
                    result["build_reason"] = "AI/source evidence did not classify this repository as a standalone app or executable developer tool."
                elif build_budget <= 0:
                    result["build_status"] = "deferred_by_run_budget"
                    result["build_targets"] = missing_targets
                else:
                    git_ref = str(app.get("source_commit_sha") or app.get("default_branch") or "main")
                    parts = github_target_parts(repo_url.removeprefix("https://github.com/"))
                    supported: dict[str, str] = {}
                    if parts:
                        supported = detect_supported_build_targets(parts[0], parts[1], git_ref, GitHubClient(token))
                    build_targets = sorted(set(missing_targets) & set(supported))
                    result["unsupported_build_targets"] = sorted(set(missing_targets) - set(build_targets))
                    if not build_targets:
                        result["build_status"] = "no_supported_build_adapter"
                        result["build_reason"] = "No matching Gradle, .NET, Go, or Electron packaging adapter was detected in the repository."
                    elif mirror.has_recent_build_attempt(repo_url, git_ref, targets=build_targets):
                        result["build_status"] = "already_dispatched_for_targets"
                        result["build_ref"] = git_ref
                        result["build_targets"] = build_targets
                    else:
                        mirror.dispatch_build(repo_url, git_ref, build_targets, str(app.get("slug") or app.get("id")))
                        build_budget -= 1
                        result["build_ref"] = git_ref
                        result["build_targets"] = build_targets
                        result["build_adapters"] = {target: supported[target] for target in build_targets}
                        result["build_status"] = "workflow_dispatched"
            else:
                result["build_status"] = "automatic_build_disabled"
        except Exception as exc:
            result.update({"status": "failed", "reason": str(exc)[:700]})
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)

    Path("harvest-results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    mirrored_count = sum(item.get("status") == "mirrored" for item in results)
    build_count = sum(item.get("build_status") == "workflow_dispatched" for item in results)
    print(
        f"Scout finished: {len(results)} repositories inspected; "
        f"{mirrored_count} with verified release packages; {build_count} isolated build(s) dispatched."
    )


if __name__ == "__main__":
    main()
