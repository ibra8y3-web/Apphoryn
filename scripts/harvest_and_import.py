#!/usr/bin/env python3
"""Discover licensed software, fetch real release packages, verify, then mirror them.

Only Apphoryn-owned GitHub Release URLs are published to store users. This script
never executes third-party source code; it dispatches source builds to another,
isolated workflow when no verified release package is available.
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", default="topic:opensource stars:>100")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--seed-file", default=str(ROOT / "catalog-seed-repositories.json"))
    parser.add_argument("--output", default="discovery-candidates.json")
    parser.add_argument("--skip-seeds", action="store_true")
    args = parser.parse_args()

    limit = max(1, min(args.limit, 10))
    catalog = CatalogService()
    candidates = catalog.discover(args.query, limit)
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
        if repo_url and repo_url.rstrip("/").lower() not in seen:
            queue.append({"repository": repo_url, "reason": "Discovered by GitHub repository search."})
            seen.add(repo_url.rstrip("/").lower())

    configured_targets = os.getenv("APPHORYN_AUTO_BUILD_TARGETS", "android,windows,linux,macos").strip()
    auto_targets = [] if configured_targets.lower() in {"off", "none", "disabled"} else sorted({part.strip() for part in configured_targets.split(",") if part.strip()})
    allowed_targets = {"android", "windows", "linux", "macos"}
    if auto_targets and not set(auto_targets).issubset(allowed_targets):
        raise SystemExit("APPHORYN_AUTO_BUILD_TARGETS contains unsupported targets.")
    results: list[dict[str, object]] = []
    for entry in queue:
        repo_url = entry["repository"]
        result: dict[str, object] = {"repository": repo_url, "seed_reason": entry.get("reason", ""), "status": "started"}
        try:
            imported = catalog.import_repository(repo_url, mirror_release_assets=True)
            app = imported["app"]
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
                # Projects without a validated installer remain unlisted until a build succeeds.
                mirror.publish_app(app, [])
                result["status"] = "source_only_not_published"
                if auto_targets:
                    git_ref = str(app.get("source_commit_sha") or app.get("default_branch") or "main")
                    if mirror.has_recent_build_attempt(repo_url, git_ref):
                        result["build_status"] = "already_dispatched_or_built"
                        result["build_ref"] = git_ref
                    else:
                        mirror.dispatch_build(repo_url, git_ref, auto_targets, str(app.get("slug") or app.get("id")))
                        result["build_ref"] = git_ref
                        result["build_targets"] = auto_targets
                        result["build_status"] = "workflow_dispatched"
                else:
                    result["reason"] = "No structurally valid signed release packages were found; no installable app was published."
        except Exception as exc:
            result.update({"status": "failed", "reason": str(exc)[:700]})
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)

    Path("harvest-results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    mirrored_count = sum(item.get("status") == "mirrored" for item in results)
    print(f"Scout finished: {len(results)} repositories inspected; {mirrored_count} app(s) mirrored with verified package files.")


if __name__ == "__main__":
    main()
