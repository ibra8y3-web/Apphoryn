from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from .config import ROOT, settings
from .github_client import GitHubClient


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_repository(value: str) -> tuple[str, str, str]:
    parsed = urlparse(str(value or "").strip())
    if parsed.scheme != "https" or parsed.hostname not in {"github.com", "www.github.com"}:
        raise ValueError("Only public GitHub repository URLs can be archived.")
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) != 2 or any(part in {".", ".."} for part in parts):
        raise ValueError("A repository URL must identify exactly one owner and repository.")
    owner, name = parts
    name = name.removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", name):
        raise ValueError("The repository URL contains an invalid owner or repository name.")
    full_name = f"{owner}/{name}"
    return full_name, owner, name


def classify_application(
    metadata: dict[str, Any],
    paths: set[str],
    readme: str = "",
) -> tuple[str, list[str]]:
    """Conservative, evidence-based project classifier. It never treats a search hit as an app."""
    lower_paths = {path.lower().lstrip("./") for path in paths}
    topics = {str(item).lower().replace("_", "-") for item in (metadata.get("topics") or [])}
    description = str(metadata.get("description") or "").lower()
    text = (description + " " + " ".join(sorted(topics)) + " " + readme[:16000]).lower()
    evidence: list[str] = []

    def has(*names: str) -> bool:
        return any(name.lower() in lower_paths for name in names)

    def has_filename(*names: str) -> bool:
        endings = tuple("/" + name.lower().lstrip("/") for name in names)
        exact = {name.lower().lstrip("/") for name in names}
        return any(path.endswith(endings) or path in exact for path in lower_paths)

    def prefix(*names: str) -> bool:
        return any(any(path.startswith(name.lower().rstrip("/") + "/") for path in lower_paths) for name in names)

    manifests = {
        "android": has_filename("AndroidManifest.xml") and any(path.endswith(("build.gradle", "build.gradle.kts")) for path in lower_paths),
        "flutter": has("pubspec.yaml") and has("lib/main.dart"),
        "react_native": has("package.json") and (has("app.json") or prefix("android", "ios")) and any("react-native" in text for _ in (0,)),
        "electron": has("package.json") and (has("electron-builder.yml", "electron-builder.yaml") or any(path.endswith(("main.cjs", "main.js", "electron.js")) for path in lower_paths) or "electron" in text),
        "tauri": (has("src-tauri/tauri.conf.json", "src-tauri/tauri.conf.json5") or (has("tauri.conf.json") and prefix("src-tauri"))) and has("src-tauri/cargo.toml", "cargo.toml"),
        "godot": has("project.godot"),
        "unity": prefix("assets", "projectsettings") and has("projectsettings/projectversion.txt"),
        "dotnet": any(path.endswith(".sln") or path.endswith(".csproj") for path in lower_paths) and has_filename("Program.cs"),
        "rust_cli": has("cargo.toml") and (has("src/main.rs") or prefix("src/bin")),
        "go_cli": has("go.mod") and (has("main.go") or prefix("cmd")),
        "python_app": (has("pyproject.toml") or has("setup.py")) and (
            has_filename("__main__.py", "main.py") or prefix("src")
        ) and any(term in text for term in ("application", "desktop app", "command line", "command-line", "cli tool", "gui", "terminal app")),
        "web_app": has("package.json") and (
            has("index.html") or has("src/main.ts", "src/main.tsx", "src/main.js", "src/main.jsx", "src/app.tsx")
        ),
        "static_web_app": has("index.html") and (has("manifest.json") or has("service-worker.js", "sw.js")) and any(
            term in text for term in ("web app", "web application", "progressive web app", "pwa")
        ),
    }
    for family, matched in manifests.items():
        if matched:
            evidence.append(f"manifest:{family}")

    app_topics = {
        "android-app", "desktop-app", "web-app", "mobile-app", "ios-app",
        "windows-app", "linux-app", "macos-app", "productivity-app",
        "utility-app", "open-source-app", "command-line-tool", "cli-app",
        "game", "game-engine", "electron-app", "flutter-app",
    }
    if topics & app_topics:
        evidence.append("topic:" + ",".join(sorted(topics & app_topics)))

    install_language = ("installation" in text or "download" in text or "getting started" in text)
    user_facing_language = any(term in text for term in (
        "desktop application", "mobile application", "web application",
        "end-user application", "graphical user interface", "cross-platform app",
        "install this app", "install the app", "application for",
    ))
    if install_language and user_facing_language:
        evidence.append("readme:user-facing-installation")

    excluded_topics = {"library", "framework", "sdk", "bindings", "boilerplate", "starter-template"}
    excluded = bool(topics & excluded_topics) and not evidence
    if excluded:
        return "not_application", ["excluded-topic:" + ",".join(sorted(topics & excluded_topics))]
    if any(item.startswith("manifest:") for item in evidence):
        return "application", evidence
    if any(item.startswith("topic:") for item in evidence) and (install_language or user_facing_language):
        return "application", evidence
    if "readme:user-facing-installation" in evidence:
        return "application", evidence
    return "not_application", evidence or ["no-application-manifest-or-user-facing-evidence"]


class RepositoryArchive:
    """Independent metadata archive for discovered apps and all upstream release/tag versions.

    Binary artifacts are deliberately not downloaded here. They still pass through
    Apphoryn's existing license, package, digest, signature, and publishing pipeline.
    """

    PAGE_SIZE = 100

    def __init__(self, path: str | Path | None = None):
        self.path = str(path or (settings.database_path.parent / "repository-archive.sqlite3"))
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self):
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        return con

    def _initialize(self) -> None:
        with self._connect() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript("""
                CREATE TABLE IF NOT EXISTS archive_repositories (
                    id TEXT PRIMARY KEY,
                    github_repo_id TEXT,
                    full_name TEXT NOT NULL,
                    repository_url TEXT NOT NULL UNIQUE,
                    owner TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    homepage_url TEXT,
                    default_branch TEXT NOT NULL DEFAULT 'main',
                    primary_language TEXT,
                    license_spdx TEXT,
                    topics_json TEXT NOT NULL DEFAULT '[]',
                    stars INTEGER NOT NULL DEFAULT 0,
                    forks INTEGER NOT NULL DEFAULT 0,
                    upstream_created_at TEXT,
                    upstream_updated_at TEXT,
                    upstream_pushed_at TEXT,
                    classification_status TEXT NOT NULL DEFAULT 'pending',
                    classification_evidence_json TEXT NOT NULL DEFAULT '[]',
                    release_page INTEGER NOT NULL DEFAULT 1,
                    tag_page INTEGER NOT NULL DEFAULT 1,
                    releases_done INTEGER NOT NULL DEFAULT 0,
                    tags_done INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL DEFAULT 'queued',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_epoch REAL NOT NULL DEFAULT 0,
                    last_error TEXT,
                    version_count INTEGER NOT NULL DEFAULT 0,
                    last_seen_at TEXT NOT NULL,
                    last_synced_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_archive_github_repo_id
                  ON archive_repositories(github_repo_id) WHERE github_repo_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_archive_queue
                  ON archive_repositories(state, next_attempt_epoch, updated_at);
                CREATE INDEX IF NOT EXISTS idx_archive_classification
                  ON archive_repositories(classification_status, updated_at);
                CREATE TABLE IF NOT EXISTS archive_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    repository_id TEXT NOT NULL REFERENCES archive_repositories(id) ON DELETE CASCADE,
                    version_kind TEXT NOT NULL,
                    version_key TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    target_commitish TEXT,
                    commit_sha TEXT,
                    published_at TEXT,
                    created_at_upstream TEXT,
                    html_url TEXT,
                    source_zip_url TEXT,
                    source_tar_url TEXT,
                    prerelease INTEGER NOT NULL DEFAULT 0,
                    draft INTEGER NOT NULL DEFAULT 0,
                    assets_json TEXT NOT NULL DEFAULT '[]',
                    raw_metadata_json TEXT NOT NULL DEFAULT '{}',
                    recorded_at TEXT NOT NULL,
                    UNIQUE(repository_id, version_kind, version_key)
                );
                CREATE INDEX IF NOT EXISTS idx_archive_versions_repo
                  ON archive_versions(repository_id, published_at DESC, id DESC);
            """)

    @staticmethod
    def _repository_id(candidate: dict[str, Any], full_name: str, repository_url: str) -> tuple[str, str | None]:
        raw_id = candidate.get("id") or candidate.get("github_repo_id")
        if raw_id is not None and str(raw_id).isdigit():
            return str(raw_id), str(raw_id)
        return hashlib.sha256(repository_url.rstrip("/").lower().encode("utf-8")).hexdigest(), None

    def enqueue_batch(self, candidates: list[dict[str, Any]]) -> dict[str, int]:
        inserted = 0
        refreshed = 0
        ignored = 0
        for candidate in candidates:
            if not isinstance(candidate, dict):
                ignored += 1
                continue
            if candidate.get("archived") or candidate.get("disabled") or candidate.get("fork"):
                ignored += 1
                continue
            raw_url = candidate.get("html_url") or candidate.get("repository_url")
            try:
                full_name, owner, name = _canonical_repository(str(raw_url or ""))
            except ValueError:
                ignored += 1
                continue
            repository_url = f"https://github.com/{full_name}"
            archive_id, github_repo_id = self._repository_id(candidate, full_name, repository_url)
            topics = candidate.get("topics") if isinstance(candidate.get("topics"), list) else []
            license_info = candidate.get("license") if isinstance(candidate.get("license"), dict) else {}
            license_spdx = candidate.get("license_spdx") or license_info.get("spdx_id")
            now = _now()
            with self._connect() as con:
                existing = con.execute(
                    "SELECT id, upstream_updated_at FROM archive_repositories WHERE id=? OR repository_url=? LIMIT 1",
                    (archive_id, repository_url),
                ).fetchone()
                if existing is None:
                    con.execute("""
                        INSERT INTO archive_repositories
                        (id,github_repo_id,full_name,repository_url,owner,name,description,homepage_url,
                         default_branch,primary_language,license_spdx,topics_json,stars,forks,
                         upstream_created_at,upstream_updated_at,upstream_pushed_at,last_seen_at,created_at,updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (
                        archive_id, github_repo_id, full_name, repository_url, owner,
                        str(candidate.get("name") or name)[:180],
                        str(candidate.get("description") or "")[:3000],
                        candidate.get("homepage") or candidate.get("homepage_url"),
                        str(candidate.get("default_branch") or "main")[:180],
                        candidate.get("language"), str(license_spdx or "")[:100] or None,
                        json.dumps(topics[:100], ensure_ascii=False),
                        max(0, int(candidate.get("stargazers_count") or candidate.get("stars") or 0)),
                        max(0, int(candidate.get("forks_count") or 0)),
                        candidate.get("created_at"), candidate.get("updated_at"),
                        candidate.get("pushed_at"), now, now, now,
                    ))
                    inserted += 1
                else:
                    changed = bool(
                        candidate.get("updated_at")
                        and str(candidate.get("updated_at")) > str(existing["upstream_updated_at"] or "")
                    )
                    con.execute("""
                        UPDATE archive_repositories SET
                          github_repo_id=COALESCE(?,github_repo_id),full_name=?,owner=?,name=?,
                          description=?,homepage_url=COALESCE(?,homepage_url),
                          default_branch=?,primary_language=COALESCE(?,primary_language),
                          license_spdx=COALESCE(?,license_spdx),topics_json=?,stars=?,forks=?,
                          upstream_created_at=COALESCE(?,upstream_created_at),
                          upstream_pushed_at=COALESCE(?,upstream_pushed_at),
                          upstream_updated_at=COALESCE(?,upstream_updated_at),
                          last_seen_at=?,state=CASE WHEN ? THEN 'queued' ELSE state END,
                          classification_status=CASE WHEN ? THEN 'pending' ELSE classification_status END,
                          release_page=CASE WHEN ? THEN 1 ELSE release_page END,
                          tag_page=CASE WHEN ? THEN 1 ELSE tag_page END,
                          releases_done=CASE WHEN ? THEN 0 ELSE releases_done END,
                          tags_done=CASE WHEN ? THEN 0 ELSE tags_done END,
                          last_error=CASE WHEN ? THEN NULL ELSE last_error END,
                          updated_at=?
                        WHERE id=?
                    """, (
                        github_repo_id, full_name, owner, str(candidate.get("name") or name)[:180],
                        str(candidate.get("description") or "")[:3000],
                        candidate.get("homepage") or candidate.get("homepage_url"),
                        str(candidate.get("default_branch") or "main")[:180],
                        candidate.get("language"), str(license_spdx or "")[:100] or None,
                        json.dumps(topics[:100], ensure_ascii=False),
                        max(0, int(candidate.get("stargazers_count") or candidate.get("stars") or 0)),
                        max(0, int(candidate.get("forks_count") or 0)),
                        candidate.get("created_at"), candidate.get("pushed_at"),
                        candidate.get("updated_at"), now, int(changed), int(changed),
                        int(changed), int(changed), int(changed), int(changed), int(changed),
                        now, existing["id"],
                    ))
                    refreshed += int(changed)
            if existing is None:
                continue
        return {"queued": inserted, "refreshed": refreshed, "ignored": ignored}

    def seed_catalog(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        candidates = []
        for row in rows:
            if isinstance(row, dict) and row.get("repository_url"):
                candidates.append({
                    "id": row.get("github_repo_id"),
                    "name": row.get("name"),
                    "html_url": row.get("repository_url"),
                    "description": row.get("description") or row.get("summary") or "",
                    "topics": row.get("topics") or [],
                    "license_spdx": row.get("license_spdx"),
                    "default_branch": row.get("default_branch") or "main",
                    "updated_at": row.get("source_updated_at"),
                    "homepage": row.get("homepage_url"),
                    "language": None,
                })
        return self.enqueue_batch(candidates)

    def process_next(self, github: GitHubClient) -> dict[str, Any] | None:
        now_epoch = time.time()
        with self._connect() as con:
            row = con.execute("""
                SELECT * FROM archive_repositories
                WHERE state IN ('queued','archiving','retry')
                  AND next_attempt_epoch <= ?
                ORDER BY CASE WHEN classification_status='pending' THEN 0 ELSE 1 END,
                         last_synced_at IS NOT NULL, updated_at
                LIMIT 1
            """, (now_epoch,)).fetchone()
        if row is None:
            return None

        repository_id = str(row["id"])
        owner, repo_name = str(row["owner"]), str(row["name"])
        try:
            if row["classification_status"] == "pending":
                branch = quote(str(row["default_branch"] or "main"), safe="")
                tree = github.request(f"/repos/{owner}/{repo_name}/git/trees/{branch}", {"recursive": "1"})
                tree_rows = tree.get("tree", []) if isinstance(tree, dict) else []
                paths = {
                    str(item.get("path") or "") for item in tree_rows
                    if isinstance(item, dict) and item.get("type") == "blob" and item.get("path")
                }
                readme_text = ""
                try:
                    readme = github.request(f"/repos/{owner}/{repo_name}/readme")
                    encoded = str(readme.get("content") or "")
                    if encoded:
                        readme_text = base64.b64decode(encoded).decode("utf-8", errors="replace")[:16000]
                except Exception:
                    pass
                status, evidence = classify_application(dict(row), paths, readme_text)
                with self._connect() as con:
                    con.execute("""
                        UPDATE archive_repositories SET classification_status=?,
                          classification_evidence_json=?,state=?,last_error=NULL,
                          updated_at=?,last_synced_at=?
                        WHERE id=?
                    """, (
                        status, json.dumps(evidence, ensure_ascii=False),
                        "archiving" if status == "application" else "complete",
                        _now(), _now(), repository_id,
                    ))
                return {"repository_id": repository_id, "step": "classified", "classification": status, "evidence": evidence}

            if row["classification_status"] != "application":
                return {"repository_id": repository_id, "step": "skipped_non_application"}

            if not row["releases_done"]:
                page_number = int(row["release_page"])
                items = github.request(
                    f"/repos/{owner}/{repo_name}/releases",
                    {"per_page": str(self.PAGE_SIZE), "page": str(page_number)},
                )
                if not isinstance(items, list):
                    raise RuntimeError("GitHub releases endpoint returned an invalid response.")
                self._store_releases(repository_id, items)
                done = len(items) < self.PAGE_SIZE
                with self._connect() as con:
                    con.execute("""
                        UPDATE archive_repositories SET release_page=?,releases_done=?,
                          version_count=(SELECT COUNT(*) FROM archive_versions WHERE repository_id=?),
                          state='archiving',last_synced_at=?,updated_at=?
                        WHERE id=?
                    """, (page_number if done else page_number + 1, int(done), repository_id, _now(), _now(), repository_id))
                return {"repository_id": repository_id, "step": "release_page", "page": page_number, "versions": len(items), "complete": done}

            if not row["tags_done"]:
                page_number = int(row["tag_page"])
                items = github.request(
                    f"/repos/{owner}/{repo_name}/tags",
                    {"per_page": str(self.PAGE_SIZE), "page": str(page_number)},
                )
                if not isinstance(items, list):
                    raise RuntimeError("GitHub tags endpoint returned an invalid response.")
                self._store_tags(repository_id, items)
                done = len(items) < self.PAGE_SIZE
                with self._connect() as con:
                    con.execute("""
                        UPDATE archive_repositories SET tag_page=?,tags_done=?,
                          version_count=(SELECT COUNT(*) FROM archive_versions WHERE repository_id=?),
                          state=?,last_synced_at=?,updated_at=?
                        WHERE id=?
                    """, (page_number if done else page_number + 1, int(done), repository_id,
                          "complete" if done else "archiving", _now(), _now(), repository_id))
                return {"repository_id": repository_id, "step": "tag_page", "page": page_number, "versions": len(items), "complete": done}

            with self._connect() as con:
                con.execute("UPDATE archive_repositories SET state='complete',last_synced_at=?,updated_at=? WHERE id=?",
                            (_now(), _now(), repository_id))
            return {"repository_id": repository_id, "step": "complete"}
        except Exception as exc:
            with self._connect() as con:
                con.execute("""
                    UPDATE archive_repositories SET state='retry',attempts=attempts+1,
                      next_attempt_epoch=?,last_error=?,updated_at=? WHERE id=?
                """, (time.time() + 300, f"{type(exc).__name__}: {str(exc)[:500]}", _now(), repository_id))
            return {"repository_id": repository_id, "step": "retry_scheduled", "error": f"{type(exc).__name__}: {str(exc)[:180]}"}

    def _store_releases(self, repository_id: str, rows: list[dict[str, Any]]) -> None:
        with self._connect() as con:
            for item in rows:
                if not isinstance(item, dict) or not item.get("tag_name"):
                    continue
                tag = str(item["tag_name"])[:300]
                assets = []
                for asset in item.get("assets") or []:
                    if not isinstance(asset, dict):
                        continue
                    assets.append({
                        "name": str(asset.get("name") or "")[:300],
                        "size": max(0, int(asset.get("size") or 0)),
                        "content_type": str(asset.get("content_type") or "")[:120],
                        "digest": asset.get("digest"),
                        "download_url": asset.get("browser_download_url"),
                    })
                con.execute("""
                    INSERT INTO archive_versions
                    (repository_id,version_kind,version_key,name,target_commitish,published_at,
                     html_url,source_zip_url,source_tar_url,prerelease,draft,assets_json,
                     raw_metadata_json,recorded_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(repository_id,version_kind,version_key) DO UPDATE SET
                      name=excluded.name,target_commitish=excluded.target_commitish,
                      published_at=excluded.published_at,html_url=excluded.html_url,
                      source_zip_url=excluded.source_zip_url,source_tar_url=excluded.source_tar_url,
                      prerelease=excluded.prerelease,draft=excluded.draft,
                      assets_json=excluded.assets_json,raw_metadata_json=excluded.raw_metadata_json
                """, (
                    repository_id, "release", tag, str(item.get("name") or tag)[:300],
                    str(item.get("target_commitish") or "")[:300] or None,
                    item.get("published_at") or item.get("created_at"), item.get("html_url"),
                    item.get("zipball_url"), item.get("tarball_url"),
                    int(bool(item.get("prerelease"))), int(bool(item.get("draft"))),
                    json.dumps(assets, ensure_ascii=False),
                    json.dumps({"tag_name": tag, "body": str(item.get("body") or "")[:3000]}, ensure_ascii=False),
                    _now(),
                ))

    def _store_tags(self, repository_id: str, rows: list[dict[str, Any]]) -> None:
        with self._connect() as con:
            for item in rows:
                if not isinstance(item, dict) or not item.get("name"):
                    continue
                name = str(item["name"])[:300]
                commit = item.get("commit") if isinstance(item.get("commit"), dict) else {}
                con.execute("""
                    INSERT INTO archive_versions
                    (repository_id,version_kind,version_key,name,commit_sha,
                     raw_metadata_json,recorded_at)
                    VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT(repository_id,version_kind,version_key) DO UPDATE SET
                      name=excluded.name,commit_sha=excluded.commit_sha,
                      raw_metadata_json=excluded.raw_metadata_json
                """, (
                    repository_id, "tag", name, name, str(commit.get("sha") or "")[:80] or None,
                    json.dumps({"commit_url": commit.get("url")}, ensure_ascii=False), _now(),
                ))

    def stats(self) -> dict[str, Any]:
        with self._connect() as con:
            row = con.execute("""
                SELECT COUNT(*) AS repositories,
                  SUM(CASE WHEN classification_status='application' THEN 1 ELSE 0 END) AS applications,
                  SUM(CASE WHEN classification_status='pending' THEN 1 ELSE 0 END) AS awaiting_classification,
                  SUM(CASE WHEN state IN ('queued','archiving','retry') THEN 1 ELSE 0 END) AS pending,
                  SUM(CASE WHEN state='complete' AND classification_status='application' THEN 1 ELSE 0 END) AS completed
                FROM archive_repositories
            """).fetchone()
            versions = con.execute("""
                SELECT COUNT(*) AS total,
                  SUM(CASE WHEN version_kind='release' THEN 1 ELSE 0 END) AS releases,
                  SUM(CASE WHEN version_kind='tag' THEN 1 ELSE 0 END) AS tags
                FROM archive_versions
            """).fetchone()
            latest = con.execute("SELECT MAX(last_seen_at) AS value FROM archive_repositories").fetchone()
        return {
            "repositories_seen": int(row["repositories"] or 0),
            "applications_archived": int(row["applications"] or 0),
            "awaiting_classification": int(row["awaiting_classification"] or 0),
            "queue_pending": int(row["pending"] or 0),
            "repositories_completed": int(row["completed"] or 0),
            "versions_total": int(versions["total"] or 0),
            "release_versions": int(versions["releases"] or 0),
            "git_tags": int(versions["tags"] or 0),
            "last_seen_at": latest["value"],
            "storage": "SQLite archive; metadata and provenance only",
        }

    def list_repositories(self, query: str = "", limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        offset = max(0, int(offset))
        sql = """SELECT id,github_repo_id,full_name,repository_url,owner,name,description,
                 homepage_url,default_branch,primary_language,license_spdx,topics_json,stars,forks,
                 upstream_created_at,upstream_updated_at,upstream_pushed_at,
                 classification_status,classification_evidence_json,state,version_count,
                 last_seen_at,last_synced_at,last_error
                 FROM archive_repositories WHERE classification_status='application'"""
        args: list[Any] = []
        if query.strip():
            sql += " AND (name LIKE ? OR full_name LIKE ? OR description LIKE ? OR primary_language LIKE ?)"
            args.extend([f"%{query.strip()[:120]}%"] * 4)
        sql += " ORDER BY upstream_updated_at DESC,stars DESC LIMIT ? OFFSET ?"
        args.extend([limit, offset])
        with self._connect() as con:
            rows = con.execute(sql, args).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["topics"] = json.loads(item.pop("topics_json") or "[]")
            item["classification_evidence"] = json.loads(item.pop("classification_evidence_json") or "[]")
            result.append(item)
        return result

    def list_versions(self, repository_id: str, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        offset = max(0, int(offset))
        with self._connect() as con:
            rows = con.execute("""
                SELECT version_kind,version_key,name,target_commitish,commit_sha,published_at,
                       created_at_upstream,html_url,source_zip_url,source_tar_url,
                       prerelease,draft,assets_json,raw_metadata_json,recorded_at
                FROM archive_versions WHERE repository_id=?
                ORDER BY COALESCE(published_at,created_at_upstream,recorded_at) DESC,
                         version_kind,version_key
                LIMIT ? OFFSET ?
            """, (repository_id, limit, offset)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["assets"] = json.loads(item.pop("assets_json") or "[]")
            item["metadata"] = json.loads(item.pop("raw_metadata_json") or "{}")
            item["prerelease"] = bool(item["prerelease"])
            item["draft"] = bool(item["draft"])
            result.append(item)
        return result

    def get_repository(self, repository_id: str) -> dict[str, Any] | None:
        with self._connect() as con:
            row = con.execute("SELECT * FROM archive_repositories WHERE id=?", (repository_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["topics"] = json.loads(item.pop("topics_json") or "[]")
        item["classification_evidence"] = json.loads(item.pop("classification_evidence_json") or "[]")
        return item


class RepositoryArchiveWorker:
    def __init__(self, archive: RepositoryArchive | None = None):
        self.archive = archive or RepositoryArchive()
        self.github = GitHubClient()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self._seed_existing_catalog()
        self.thread = threading.Thread(target=self._run, name="apphoryn-repository-archive", daemon=True)
        self.thread.start()

    def _seed_existing_catalog(self) -> None:
        path = ROOT / "apps" / "web" / "catalog" / "index.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            rows = payload.get("apps", []) if isinstance(payload, dict) else []
            self.archive.seed_catalog([row for row in rows if isinstance(row, dict)])
        except (OSError, json.JSONDecodeError):
            pass

    def _run(self) -> None:
        while not self.stop_event.is_set():
            result = self.archive.process_next(self.github)
            # Keep unauthenticated public API traffic below GitHub's hourly core limit.
            delay = 2 if settings.github_token else 65
            self.stop_event.wait(delay if result else max(15, delay))

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5)


repository_archive = RepositoryArchive()
_archive_worker = RepositoryArchiveWorker(repository_archive)


def start_repository_archive_worker() -> None:
    _archive_worker.start()


def stop_repository_archive_worker() -> None:
    _archive_worker.stop()
