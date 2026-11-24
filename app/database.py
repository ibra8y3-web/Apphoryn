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
                    github_repo_id TEXT,
                    source_updated_at TEXT,
                    project_kind TEXT NOT NULL DEFAULT 'unknown',
                    build_recommended INTEGER NOT NULL DEFAULT 0,
                    classification_source TEXT NOT NULL DEFAULT 'heuristic',
                    classification_reason TEXT NOT NULL DEFAULT '',
                    homepage_url TEXT,
                    owner TEXT NOT NULL,
                    default_branch TEXT NOT NULL DEFAULT 'main',
                    source_commit_sha TEXT,
                    license_text TEXT NOT NULL DEFAULT '',
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
                    validation_status TEXT NOT NULL DEFAULT 'validated',
                    signature_status TEXT NOT NULL DEFAULT 'not_checked',
                    signer_certificate_sha256 TEXT,
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
                CREATE INDEX IF NOT EXISTS idx_artifact_app_sha ON artifacts(app_id, sha256);
                CREATE INDEX IF NOT EXISTS idx_jobs_created ON build_jobs(created_at DESC);
            """)
            app_columns = {row[1] for row in con.execute("PRAGMA table_info(apps)").fetchall()}
            for column, statement in {
                "translations_json": "ALTER TABLE apps ADD COLUMN translations_json TEXT NOT NULL DEFAULT '{}'",
                "default_branch": "ALTER TABLE apps ADD COLUMN default_branch TEXT NOT NULL DEFAULT 'main'",
                "license_text": "ALTER TABLE apps ADD COLUMN license_text TEXT NOT NULL DEFAULT ''",
                "source_commit_sha": "ALTER TABLE apps ADD COLUMN source_commit_sha TEXT",
                "github_repo_id": "ALTER TABLE apps ADD COLUMN github_repo_id TEXT",
                "source_updated_at": "ALTER TABLE apps ADD COLUMN source_updated_at TEXT",
                "project_kind": "ALTER TABLE apps ADD COLUMN project_kind TEXT NOT NULL DEFAULT 'unknown'",
                "build_recommended": "ALTER TABLE apps ADD COLUMN build_recommended INTEGER NOT NULL DEFAULT 0",
                "classification_source": "ALTER TABLE apps ADD COLUMN classification_source TEXT NOT NULL DEFAULT 'heuristic'",
                "classification_reason": "ALTER TABLE apps ADD COLUMN classification_reason TEXT NOT NULL DEFAULT ''",
            }.items():
                if column not in app_columns:
                    con.execute(statement)
            con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_apps_github_repo_id ON apps(github_repo_id) WHERE github_repo_id IS NOT NULL")
            artifact_columns = {row[1] for row in con.execute("PRAGMA table_info(artifacts)").fetchall()}
            for column, statement in {
                "validation_status": "ALTER TABLE artifacts ADD COLUMN validation_status TEXT NOT NULL DEFAULT 'validated'",
                "signature_status": "ALTER TABLE artifacts ADD COLUMN signature_status TEXT NOT NULL DEFAULT 'not_checked'",
                "signer_certificate_sha256": "ALTER TABLE artifacts ADD COLUMN signer_certificate_sha256 TEXT",
            }.items():
                if column not in artifact_columns:
                    con.execute(statement)

            # Older releases only prevented duplicates when filename/version also
            # matched. Collapse legacy byte-identical rows before enforcing the
            # invariant at the database layer, so concurrent imports cannot race.
            con.execute("""
                DELETE FROM artifacts
                WHERE rowid NOT IN (
                    SELECT MIN(rowid)
                    FROM artifacts
                    GROUP BY app_id, lower(sha256)
                )
            """)
            con.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_artifact_app_sha_unique
                ON artifacts(app_id, lower(sha256))
            """)

    @staticmethod
    def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        for key in ("topics_json", "translations_json", "targets_json"):
            if key in result:
                result[key.removesuffix("_json")] = json.loads(result.pop(key) or ("{}" if key == "translations_json" else "[]"))
        for key in ("license_eligible", "build_recommended", "upstream_digest_verified"):
            if key in result:
                result[key] = bool(result[key])
        return result

    def upsert_app(self, record: dict[str, Any]) -> dict[str, Any]:
        repo_id = str(record.get("github_repo_id") or "").strip() or None
        with self.connect() as con:
            existing = con.execute(
                """SELECT id, created_at FROM apps
                   WHERE repository_url=? OR (? IS NOT NULL AND github_repo_id=?)
                   ORDER BY CASE WHEN github_repo_id=? THEN 0 ELSE 1 END LIMIT 1""",
                (record["repository_url"], repo_id, repo_id, repo_id),
            ).fetchone()
            app_id = existing["id"] if existing else record["id"]
            created_at = existing["created_at"] if existing else record["created_at"]
            values = (
                record["slug"], record["name"], record["summary"], record["description"],
                record["repository_url"], record.get("homepage_url"), record["owner"],
                record.get("default_branch", "main"), record.get("source_commit_sha"),
                repo_id, record.get("source_updated_at"),
                record.get("project_kind", "unknown"), int(record.get("build_recommended", False)),
                record.get("classification_source", "heuristic"), record.get("classification_reason", ""),
                record.get("license_text", ""), record.get("license_spdx"),
                int(record["license_eligible"]), record["category"],
                json.dumps(record.get("topics", [])),
                json.dumps(record.get("translations", {}), ensure_ascii=False),
                record.get("latest_version"), record.get("release_url"), record["updated_at"],
            )
            if existing:
                con.execute(
                    """UPDATE apps SET
                       slug=?,name=?,summary=?,description=?,repository_url=?,homepage_url=?,owner=?,
                       default_branch=?,source_commit_sha=?,github_repo_id=COALESCE(?,github_repo_id),
                       source_updated_at=COALESCE(?,source_updated_at),project_kind=?,build_recommended=?,
                       classification_source=?,classification_reason=?,license_text=?,license_spdx=?,
                       license_eligible=?,category=?,topics_json=?,translations_json=?,latest_version=?,
                       release_url=?,updated_at=?
                       WHERE id=?""",
                    (*values, app_id),
                )
            else:
                con.execute(
                    """INSERT INTO apps
                       (id,slug,name,summary,description,repository_url,homepage_url,owner,default_branch,
                        source_commit_sha,github_repo_id,source_updated_at,project_kind,build_recommended,
                        classification_source,classification_reason,license_text,license_spdx,license_eligible,
                        category,topics_json,translations_json,latest_version,release_url,
                        created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (app_id, record["slug"], record["name"], record["summary"], record["description"],
                     record["repository_url"], record.get("homepage_url"), record["owner"],
                     record.get("default_branch", "main"), record.get("source_commit_sha"), repo_id,
                     record.get("source_updated_at"), record.get("project_kind", "unknown"),
                     int(record.get("build_recommended", False)), record.get("classification_source", "heuristic"),
                     record.get("classification_reason", ""), record.get("license_text", ""),
                     record.get("license_spdx"), int(record["license_eligible"]), record["category"],
                     json.dumps(record.get("topics", [])),
                     json.dumps(record.get("translations", {}), ensure_ascii=False),
                     record.get("latest_version"), record.get("release_url"), created_at, record["updated_at"]),
                )
            row = con.execute("SELECT * FROM apps WHERE id=?", (app_id,)).fetchone()
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
        """Store one verified binary per app/content hash, regardless of release tag or filename."""
        with self.connect() as con:
            existing = con.execute(
                "SELECT * FROM artifacts WHERE app_id=? AND lower(sha256)=lower(?) ORDER BY created_at DESC LIMIT 1",
                (item["app_id"], item["sha256"]),
            ).fetchone()
            if existing is not None:
                return self._dict(existing) or {}

            con.execute("""INSERT OR IGNORE INTO artifacts
                (id,app_id,filename,platform,package_type,version,size_bytes,sha256,upstream_digest_verified,validation_status,signature_status,signer_certificate_sha256,source_url,storage_path,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (item["id"],item["app_id"],item["filename"],item["platform"],item["package_type"],item.get("version"),
                 item["size_bytes"],item["sha256"],int(item.get("upstream_digest_verified",False)),
                 item.get("validation_status","validated"),item.get("signature_status","not_checked"),
                 item.get("signer_certificate_sha256"),item.get("source_url"),item["storage_path"],item["created_at"]))
            row = con.execute("SELECT * FROM artifacts WHERE id=?", (item["id"],)).fetchone()
            if row is None:
                row = con.execute(
                    "SELECT * FROM artifacts WHERE app_id=? AND lower(sha256)=lower(?) LIMIT 1",
                    (item["app_id"], item["sha256"]),
                ).fetchone()
            return self._dict(row) or {}

    def get_artifact_by_sha256(self, app_id: str, sha256: str) -> dict[str, Any] | None:
        with self.connect() as con:
            row = con.execute(
                "SELECT * FROM artifacts WHERE app_id=? AND lower(sha256)=lower(?) ORDER BY created_at DESC LIMIT 1",
                (app_id, sha256),
            ).fetchone()
            return self._dict(row)

    def list_artifacts(self, app_id: str) -> list[dict[str, Any]]:
        with self.connect() as con:
            return [self._dict(row) or {} for row in con.execute("SELECT * FROM artifacts WHERE app_id=? ORDER BY created_at DESC", (app_id,)).fetchall()]

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        with self.connect() as con:
            return self._dict(con.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone())

    def add_job(self, job: dict[str, Any]) -> None:
        with self.connect() as con:
            con.execute("""INSERT INTO build_jobs (id,app_id,repository_url,git_ref,targets_json,status,workflow_url,message,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (job["id"],job["app_id"],job["repository_url"],job["git_ref"],json.dumps(job["targets"]),
                 job["status"],job.get("workflow_url"),job.get("message"),job["created_at"],job["updated_at"]))

    def update_job(self, job_id: str, status: str, message: str | None = None, workflow_url: str | None = None) -> None:
        with self.connect() as con:
            con.execute("UPDATE build_jobs SET status=?,message=?,workflow_url=COALESCE(?,workflow_url),updated_at=? WHERE id=?",
                        (status,message,workflow_url,utc_now(),job_id))

    def list_jobs(self, limit: int = 30) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute("SELECT * FROM build_jobs ORDER BY created_at DESC LIMIT ?", (max(1,min(limit,100)),)).fetchall()
            return [self._dict(row) or {} for row in rows]


db = Database()
