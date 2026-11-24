from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import settings


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path | str = settings.database_path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as con:
            con.execute("PRAGMA journal_mode = WAL")
            con.executescript("""
                CREATE TABLE IF NOT EXISTS apps (
                    id TEXT PRIMARY KEY,
                    slug TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    repository_url TEXT NOT NULL UNIQUE,
                    homepage_url TEXT,
                    owner TEXT NOT NULL,
                    license_spdx TEXT,
                    license_eligible INTEGER NOT NULL DEFAULT 0,
                    category TEXT NOT NULL DEFAULT 'other',
                    topics_json TEXT NOT NULL DEFAULT '[]',
                    translations_json TEXT NOT NULL DEFAULT '{}',
                    latest_version TEXT,
                    release_url TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY,
                    app_id TEXT NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
                    filename TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    package_type TEXT NOT NULL,
                    version TEXT,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    upstream_digest_verified INTEGER NOT NULL DEFAULT 0,
                    source_url TEXT,
                    storage_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(app_id, filename, version, sha256)
                );
                CREATE TABLE IF NOT EXISTS build_jobs (
                    id TEXT PRIMARY KEY,
                    app_id TEXT NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
                    repository_url TEXT NOT NULL,
                    git_ref TEXT NOT NULL,
                    targets_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    workflow_url TEXT,
                    message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_apps_category ON apps(category);
                CREATE INDEX IF NOT EXISTS idx_artifacts_app ON artifacts(app_id);
                CREATE INDEX IF NOT EXISTS idx_jobs_created ON build_jobs(created_at DESC);
            """)
            columns = {row[1] for row in con.execute("PRAGMA table_info(apps)").fetchall()}
            if "translations_json" not in columns:
                con.execute("ALTER TABLE apps ADD COLUMN translations_json TEXT NOT NULL DEFAULT '{}' ")

    @staticmethod
    def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        for key in ("topics_json", "translations_json", "targets_json"):
            if key in result:
                result[key.removesuffix("_json")] = json.loads(result.pop(key) or ("{}" if key == "translations_json" else "[]"))
        for key in ("license_eligible", "upstream_digest_verified"):
            if key in result:
                result[key] = bool(result[key])
        return result

    def upsert_app(self, record: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as con:
            existing = con.execute("SELECT id, created_at FROM apps WHERE repository_url=?", (record["repository_url"],)).fetchone()
            app_id = existing["id"] if existing else record["id"]
            created_at = existing["created_at"] if existing else record["created_at"]
            con.execute("""INSERT INTO apps
                (id,slug,name,summary,description,repository_url,homepage_url,owner,license_spdx,license_eligible,category,topics_json,translations_json,latest_version,release_url,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(repository_url) DO UPDATE SET
                    slug=excluded.slug,name=excluded.name,summary=excluded.summary,description=excluded.description,
                    homepage_url=excluded.homepage_url,owner=excluded.owner,license_spdx=excluded.license_spdx,
                    license_eligible=excluded.license_eligible,category=excluded.category,topics_json=excluded.topics_json,
                    translations_json=excluded.translations_json,latest_version=excluded.latest_version,release_url=excluded.release_url,updated_at=excluded.updated_at""",
                (app_id,record["slug"],record["name"],record["summary"],record["description"],record["repository_url"],record.get("homepage_url"),record["owner"],record.get("license_spdx"),int(record["license_eligible"]),record["category"],json.dumps(record.get("topics", [])),json.dumps(record.get("translations", {}), ensure_ascii=False),record.get("latest_version"),record.get("release_url"),created_at,record["updated_at"]))
            row = con.execute("SELECT * FROM apps WHERE repository_url=?", (record["repository_url"],)).fetchone()
            return self._dict(row) or {}

    def get_app(self, app_id: str) -> dict[str, Any] | None:
        with self.connect() as con:
            return self._dict(con.execute("SELECT * FROM apps WHERE id=?", (app_id,)).fetchone())

    def get_app_by_repo(self, repository_url: str) -> dict[str, Any] | None:
        with self.connect() as con:
            return self._dict(con.execute("SELECT * FROM apps WHERE repository_url=?", (repository_url,)).fetchone())

    def list_apps(self, category: str | None = None, query: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        sql = "SELECT * FROM apps WHERE 1=1"
        args: list[Any] = []
        if category and category != "all":
            sql += " AND category=?"; args.append(category)
        if query:
            sql += " AND (name LIKE ? OR summary LIKE ? OR description LIKE ?)"
            args.extend([f"%{query}%"] * 3)
        sql += " ORDER BY updated_at DESC LIMIT ?"; args.append(max(1, min(limit, 200)))
        with self.connect() as con:
            return [self._dict(row) or {} for row in con.execute(sql, args).fetchall()]

    def add_artifact(self, item: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as con:
            con.execute("""INSERT OR IGNORE INTO artifacts
                (id,app_id,filename,platform,package_type,version,size_bytes,sha256,upstream_digest_verified,source_url,storage_path,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (item["id"],item["app_id"],item["filename"],item["platform"],item["package_type"],item.get("version"),item["size_bytes"],item["sha256"],int(item.get("upstream_digest_verified",False)),item.get("source_url"),item["storage_path"],item["created_at"]))
            row = con.execute("SELECT * FROM artifacts WHERE id=?", (item["id"],)).fetchone()
            if row is None:
                row = con.execute("SELECT * FROM artifacts WHERE app_id=? AND filename=? AND version IS ? AND sha256=?", (item["app_id"],item["filename"],item.get("version"),item["sha256"])).fetchone()
            return self._dict(row) or {}

    def list_artifacts(self, app_id: str) -> list[dict[str, Any]]:
        with self.connect() as con:
            return [self._dict(row) or {} for row in con.execute("SELECT * FROM artifacts WHERE app_id=? ORDER BY created_at DESC", (app_id,)).fetchall()]

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        with self.connect() as con:
            return self._dict(con.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone())

    def add_job(self, job: dict[str, Any]) -> None:
        with self.connect() as con:
            con.execute("""INSERT INTO build_jobs (id,app_id,repository_url,git_ref,targets_json,status,workflow_url,message,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)""", (job["id"],job["app_id"],job["repository_url"],job["git_ref"],json.dumps(job["targets"]),job["status"],job.get("workflow_url"),job.get("message"),job["created_at"],job["updated_at"]))

    def update_job(self, job_id: str, status: str, message: str | None = None, workflow_url: str | None = None) -> None:
        with self.connect() as con:
            con.execute("UPDATE build_jobs SET status=?,message=?,workflow_url=COALESCE(?,workflow_url),updated_at=? WHERE id=?", (status,message,workflow_url,utc_now(),job_id))

    def list_jobs(self, limit: int = 30) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute("SELECT * FROM build_jobs ORDER BY created_at DESC LIMIT ?", (max(1,min(limit,100)),)).fetchall()
            return [self._dict(row) or {} for row in rows]


db = Database()
