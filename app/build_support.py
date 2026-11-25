from __future__ import annotations

import base64
import json
import re
import urllib.parse
from typing import Any

from .github_client import GitHubClient


DESKTOP_TARGETS = {"windows", "linux", "macos"}


def detect_supported_build_targets(
    owner: str, repository: str, git_ref: str, github: GitHubClient | None = None
) -> dict[str, str]:
    """Return only targets for which scripts/ci_build.py has a real adapter.

    The probe reads GitHub metadata and package manifests only. It never clones,
    installs dependencies, or executes third-party code in the trusted Scout job.
    """
    client = github or GitHubClient()
    safe_ref = urllib.parse.quote(git_ref, safe="")
    tree_response = client.request(
        f"/repos/{owner}/{repository}/git/trees/{safe_ref}?recursive=1"
    )
    tree = tree_response.get("tree", []) if isinstance(tree_response, dict) else []
    paths = [
        str(item.get("path") or "")
        for item in tree
        if isinstance(item, dict) and item.get("type") == "blob"
    ]

    supported: dict[str, str] = {}
    if any(path.rsplit("/", 1)[-1] == "gradlew" for path in paths):
        supported["android"] = "Gradle wrapper detected."

    if any(path.lower().endswith(".csproj") for path in paths):
        for target in DESKTOP_TARGETS:
            supported[target] = ".NET project detected."

    # A go.mod file alone does not make a repository an executable app.
    # Require a real package main file inside that exact module before scheduling builds.
    go_module_roots = {
        path.rsplit("/", 1)[0] if "/" in path else ""
        for path in paths if path.rsplit("/", 1)[-1] == "go.mod"
    }
    go_sources = [
        path for path in paths
        if path.lower().endswith(".go")
        and not path.lower().endswith("_test.go")
        and not any(part in {"vendor", ".git", "node_modules", "build"} for part in path.split("/"))
    ]
    checked_go_sources = 0
    for source_path in sorted(go_sources, key=lambda p: ("/cmd/" not in f"/{p}", "main.go" not in p.lower(), len(p))):
        source_dir = source_path.rsplit("/", 1)[0] if "/" in source_path else ""
        matching_roots = [
            root for root in go_module_roots
            if source_dir == root or (root and source_dir.startswith(root + "/")) or not root
        ]
        if not matching_roots:
            continue
        owning_root = max(matching_roots, key=len)
        encoded_path = urllib.parse.quote(source_path, safe="/")
        try:
            result = client.request(
                f"/repos/{owner}/{repository}/contents/{encoded_path}?ref={safe_ref}"
            )
            raw = base64.b64decode(str(result.get("content") or "").replace("\\n", ""))
            source_text = raw[:128 * 1024].decode("utf-8", errors="replace")
        except Exception:
            continue
        checked_go_sources += 1
        if re.search(r"(?m)^\\s*package\\s+main(?:\\s|$)", source_text):
            for target in DESKTOP_TARGETS:
                supported.setdefault(target, f"Go executable package detected in {owning_root or '.'}.")
            break
        if checked_go_sources >= 12:
            break

    package_files = [
        path for path in paths
        if path.rsplit("/", 1)[-1].lower() == "package.json"
    ][:8]
    for package_path in package_files:
        encoded_path = urllib.parse.quote(package_path, safe="/")
        try:
            result = client.request(
                f"/repos/{owner}/{repository}/contents/{encoded_path}?ref={safe_ref}"
            )
            raw = base64.b64decode(str(result.get("content") or "").replace("\n", ""))
            package = json.loads(raw.decode("utf-8"))
        except Exception:
            # A malformed or inaccessible optional package manifest must not
            # prevent other source adapters from being detected.
            continue
        if not isinstance(package, dict):
            continue
        deps: dict[str, Any] = {}
        for key in ("dependencies", "devDependencies", "optionalDependencies"):
            value = package.get(key)
            if isinstance(value, dict):
                deps.update(value)
        scripts = package.get("scripts") if isinstance(package.get("scripts"), dict) else {}
        if "electron" in deps and ("electron-builder" in deps or "dist" in scripts):
            for target in DESKTOP_TARGETS:
                supported.setdefault(target, "Electron project with a packaging script detected.")
            break

    return supported
