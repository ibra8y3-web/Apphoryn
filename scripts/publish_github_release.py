#!/usr/bin/env python3
"""Publish isolated build outputs as assets in Apphoryn-owned GitHub Releases."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.github_release_mirror import GitHubMirrorError, GitHubReleaseMirror


def has_build_outputs(directory: str | Path) -> bool:
    root = Path(directory)
    return root.is_dir() and any(
        item.is_file() and not item.is_symlink()
        for item in root.rglob("*")
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--ref", required=True)
    args = parser.parse_args()
    if not has_build_outputs(args.directory):
        print("No verified build outputs were produced for this request; nothing to publish.")
        return
    token = os.getenv("GITHUB_TOKEN", "")
    target_repository = os.getenv("GITHUB_REPOSITORY", "")
    branch = os.getenv("APPHORYN_GITHUB_REF", "main")
    if not token or not target_repository:
        raise SystemExit("GITHUB_TOKEN and GITHUB_REPOSITORY are required; no package was published.")
    try:
        mirror = GitHubReleaseMirror(token, target_repository, branch)
        result = mirror.publish_built_artifacts(args.repository, args.ref, Path(args.directory))
    except (GitHubMirrorError, OSError, ValueError) as exc:
        raise SystemExit(f"Apphoryn build publication failed: {exc}") from exc
    print(f"Published {len(result['published'])} validated build package(s).")
    print(result["release_url"])
    for artifact in result["published"]:
        print(f"{artifact['platform']} {artifact['filename']} sha256={artifact['sha256']}")


if __name__ == "__main__":
    main()
