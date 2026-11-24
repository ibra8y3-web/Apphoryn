#!/usr/bin/env python3
"""Discover real GitHub projects and optionally import eligible releases into Apphoryn.

This script never executes source code. Optional builds are merely dispatched to the
separate, isolated build workflow after the repository has passed the license gate.
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.services import CatalogService


def post_json(base: str, path: str, token: str, body: dict) -> dict:
    request = urllib.request.Request(
        base.rstrip("/") + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Apphoryn-Admin": token, "User-Agent": "Apphoryn-Scout/0.1"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", default="topic:opensource stars:>100")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--output", default="discovery-candidates.json")
    args = parser.parse_args()
    limit = max(1, min(args.limit, 10))
    candidates = CatalogService().discover(args.query, limit)
    Path(args.output).write_text(json.dumps(candidates, ensure_ascii=False, indent=2), encoding="utf-8")

    base = os.getenv("APPHORYN_API_BASE_URL", "").rstrip("/")
    admin_token = os.getenv("APPHORYN_ADMIN_TOKEN", "")
    targets = sorted({target.strip() for target in os.getenv("APPHORYN_AUTO_BUILD_TARGETS", "").split(",") if target.strip()})
    allowed_targets = {"android", "windows", "linux", "macos"}
    if targets and not set(targets).issubset(allowed_targets):
        raise SystemExit("APPHORYN_AUTO_BUILD_TARGETS contains an unsupported platform.")
    if base and not base.startswith("https://"):
        raise SystemExit("APPHORYN_API_BASE_URL must use HTTPS.")

    results = []
    if not base or not admin_token:
        print("Candidate discovery completed. Store URL/admin secret are not configured; no apps were imported.")
    else:
        for item in candidates:
            result = {"repository": item.get("repository_url"), "status": "skipped"}
            if not item.get("license_eligible") or item.get("archived"):
                result["reason"] = "License is not allowlisted or repository is archived."
                results.append(result)
                continue
            try:
                imported = post_json(base, "/api/v1/admin/import", admin_token, {"repository_url": item["repository_url"], "mirror_release_assets": True})
                app = imported.get("app", {})
                artifacts = imported.get("artifacts", [])
                result.update({"status": "imported", "app_id": app.get("id"), "artifact_count": len(artifacts)})
                if not artifacts and targets and app.get("id"):
                    build = post_json(base, "/api/v1/admin/build", admin_token, {
                        "repository_url": item["repository_url"], "git_ref": item.get("default_branch") or "main", "targets": targets,
                    })
                    result["build_job_id"] = build.get("id")
                    result["build_status"] = build.get("status")
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError) as exc:
                result.update({"status": "failed", "reason": str(exc)[:300]})
            results.append(result)
            print(json.dumps(result, ensure_ascii=False))
    Path("harvest-results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Discovered {len(candidates)} candidate(s); import attempts: {sum(r['status'] in {'imported','failed'} for r in results)}.")


if __name__ == "__main__":
    main()
