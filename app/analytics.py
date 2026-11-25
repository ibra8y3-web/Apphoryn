from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AnalyticsStore:
    """Privacy-minimizing local event ledger with retryable PostgreSQL/D1 replication."""

    ALLOWED_EVENTS = {
        "app_view", "download_requested", "download_completed",
        "search", "catalog_view", "favorite_added", "favorite_removed",
    }

    def __init__(self, path: str | Path | None = None):
        default = Path(__file__).resolve().parents[1] / "storage" / "analytics.sqlite3"
        self.path = str(path or os.getenv("APPHORYN_ANALYTICS_DATABASE", str(default)))
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=30000")
        con.execute("PRAGMA foreign_keys=ON")
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def initialize(self) -> None:
        with self.connect() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript("""
                CREATE TABLE IF NOT EXISTS analytics_events (
                    id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    app_slug TEXT,
                    artifact_id TEXT,
                    platform TEXT,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    synced_at TEXT,
                    last_sync_attempt_at TEXT,
                    sync_attempts INTEGER NOT NULL DEFAULT 0,
                    last_sync_error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_analytics_events_time
                    ON analytics_events(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_analytics_events_type_time
                    ON analytics_events(event_type, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_analytics_events_app_type
                    ON analytics_events(app_slug, event_type, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_analytics_events_pending
                    ON analytics_events(synced_at, last_sync_attempt_at);
            """)

    @staticmethod
    def _safe_details(details: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(details, dict):
            return {}
        # Never persist IP addresses, auth headers, cookies, tokens, arbitrary request bodies or raw URLs.
        allowed = {"query_sha256", "query_length", "source", "result_count", "client_platform"}
        result: dict[str, Any] = {}
        for key in allowed:
            value = details.get(key)
            if isinstance(value, (str, int, float, bool)) and len(str(value)) <= 160:
                result[key] = value
        return result

    def record_event(self, event_type: str, *, app_slug: str | None = None,
                     artifact_id: str | None = None, platform: str | None = None,
                     details: dict[str, Any] | None = None) -> str | None:
        if event_type not in self.ALLOWED_EVENTS:
            return None
        event_id, now = str(uuid.uuid4()), _utc_now()
        with self.connect() as con:
            con.execute(
                """INSERT INTO analytics_events
                   (id,event_type,app_slug,artifact_id,platform,details_json,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (event_id, event_type, (app_slug or "")[:180] or None,
                 (artifact_id or "")[:180] or None, (platform or "")[:40] or None,
                 json.dumps(self._safe_details(details), separators=(",", ":")), now),
            )
        return event_id

    def record_search(self, query: str, result_count: int, source: str = "catalog") -> str | None:
        term = query.strip()
        if not term:
            return None
        digest = hashlib.sha256(term.casefold().encode("utf-8")).hexdigest()
        return self.record_event(
            "search", details={"query_sha256": digest, "query_length": len(term),
                               "source": source[:40], "result_count": max(0, int(result_count))},
        )

    def summary(self) -> dict[str, Any]:
        with self.connect() as con:
            totals = con.execute(
                "SELECT event_type,COUNT(*) AS total FROM analytics_events GROUP BY event_type"
            ).fetchall()
            apps = con.execute(
                """SELECT app_slug,
                   SUM(CASE WHEN event_type='app_view' THEN 1 ELSE 0 END) AS views,
                   SUM(CASE WHEN event_type='download_requested' THEN 1 ELSE 0 END) AS downloads,
                   SUM(CASE WHEN event_type='download_completed' THEN 1 ELSE 0 END) AS completed_downloads,
                   SUM(CASE WHEN event_type='favorite_added' THEN 1 ELSE 0 END) AS favorites_added
                   FROM analytics_events WHERE app_slug IS NOT NULL
                   GROUP BY app_slug ORDER BY downloads DESC,views DESC LIMIT 50"""
            ).fetchall()
            pending = con.execute(
                "SELECT COUNT(*) FROM analytics_events WHERE synced_at IS NULL"
            ).fetchone()[0]
            last = con.execute(
                "SELECT MAX(created_at) FROM analytics_events"
            ).fetchone()[0]
            errors = con.execute(
                """SELECT last_sync_error FROM analytics_events
                   WHERE synced_at IS NULL AND last_sync_error IS NOT NULL
                   ORDER BY last_sync_attempt_at DESC LIMIT 1"""
            ).fetchone()
        counts = {row["event_type"]: int(row["total"]) for row in totals}
        return {
            "totals": counts,
            "views": counts.get("app_view", 0),
            "download_requests": counts.get("download_requested", 0),
            "completed_downloads": counts.get("download_completed", 0),
            "favorites_added": counts.get("favorite_added", 0),
            "favorites_removed": counts.get("favorite_removed", 0),
            "searches": counts.get("search", 0),
            "per_app": [dict(row) for row in apps],
            "pending_replication": int(pending),
            "last_event_at": last,
            "last_replication_error": errors["last_sync_error"] if errors else None,
            "backend": self.backend_status(),
        }

    @staticmethod
    def _backend_config() -> tuple[str, dict[str, str]]:
        pg = os.getenv("APPHORYN_ANALYTICS_POSTGRES_DSN", "").strip()
        d1 = {
            "account_id": os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip(),
            "database_id": os.getenv("CLOUDFLARE_D1_DATABASE_ID", "").strip(),
            "token": os.getenv("CLOUDFLARE_D1_API_TOKEN", "").strip(),
        }
        if pg:
            return "postgres", {"dsn": pg}
        if all(d1.values()):
            return "cloudflare_d1", d1
        return "local_sqlite", {}

    def backend_status(self) -> dict[str, Any]:
        provider, config = self._backend_config()
        if provider == "postgres":
            return {"provider": provider, "configured": True, "replication": "enabled",
                    "note": "PostgreSQL-compatible endpoint configured; actual connectivity is proven by the first successful sync."}
        if provider == "cloudflare_d1":
            return {"provider": provider, "configured": True, "replication": "enabled",
                    "note": "Cloudflare D1 credentials configured; actual connectivity is proven by the first successful sync."}
        return {"provider": provider, "configured": False, "replication": "local_only",
                "note": "Events are safely stored in persistent local SQLite. Configure a PostgreSQL DSN or Cloudflare D1 credentials to replicate them remotely."}

    def _pending_batch(self, limit: int = 100) -> list[dict[str, Any]]:
        now = _utc_now()
        retry_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            rows = con.execute(
                """SELECT * FROM analytics_events
                   WHERE synced_at IS NULL
                     AND (last_sync_attempt_at IS NULL OR last_sync_attempt_at < ?)
                   ORDER BY created_at ASC LIMIT ?""",
                (retry_cutoff, max(1, min(limit, 500))),
            ).fetchall()
            ids = [row["id"] for row in rows]
            con.executemany(
                """UPDATE analytics_events SET last_sync_attempt_at=?,sync_attempts=sync_attempts+1
                   WHERE id=?""",
                [(now, event_id) for event_id in ids],
            )
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json") or "{}")
            result.append(item)
        return result

    def _mark_synced(self, event_ids: list[str]) -> None:
        if not event_ids:
            return
        now = _utc_now()
        with self.connect() as con:
            con.executemany(
                "UPDATE analytics_events SET synced_at=?,last_sync_error=NULL WHERE id=?",
                [(now, event_id) for event_id in event_ids],
            )

    def _mark_sync_failed(self, event_ids: list[str], error: str) -> None:
        if not event_ids:
            return
        with self.connect() as con:
            con.executemany(
                "UPDATE analytics_events SET last_sync_error=? WHERE id=?",
                [(error[:500], event_id) for event_id in event_ids],
            )

    @staticmethod
    def _sync_postgres(dsn: str, rows: list[dict[str, Any]]) -> None:
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("Install psycopg[binary] to replicate analytics to Neon/Supabase PostgreSQL.") from exc
        with psycopg.connect(dsn, connect_timeout=5) as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS apphoryn_analytics_events (
                        id TEXT PRIMARY KEY,
                        event_type TEXT NOT NULL,
                        app_slug TEXT,
                        artifact_id TEXT,
                        platform TEXT,
                        details JSONB NOT NULL DEFAULT '{}'::jsonb,
                        created_at TIMESTAMPTZ NOT NULL
                    )
                """)
                cursor.executemany(
                    """INSERT INTO apphoryn_analytics_events
                       (id,event_type,app_slug,artifact_id,platform,details,created_at)
                       VALUES(%s,%s,%s,%s,%s,%s::jsonb,%s::timestamptz)
                       ON CONFLICT(id) DO NOTHING""",
                    [(r["id"], r["event_type"], r["app_slug"], r["artifact_id"], r["platform"],
                      json.dumps(r["details"]), r["created_at"]) for r in rows],
                )

    @staticmethod
    def _d1_request(account_id: str, database_id: str, token: str,
                    sql: str, params: list[Any] | None = None) -> None:
        url = (
            "https://api.cloudflare.com/client/v4/accounts/"
            + urllib.parse.quote(account_id, safe="")
            + "/d1/database/" + urllib.parse.quote(database_id, safe="") + "/query"
        )
        body = {"sql": sql}
        if params is not None:
            body["params"] = params
        request = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                     "Accept": "application/json", "User-Agent": "Apphoryn-Analytics/1.0"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                payload = json.loads(response.read(2 * 1024 * 1024).decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Cloudflare D1 request failed: {type(exc).__name__}") from exc
        if not payload.get("success"):
            errors = payload.get("errors") or []
            raise RuntimeError("Cloudflare D1 rejected the query: " + str(errors[:2])[:300])

    @classmethod
    def _sync_d1(cls, config: dict[str, str], rows: list[dict[str, Any]]) -> None:
        cls._d1_request(config["account_id"], config["database_id"], config["token"], """
            CREATE TABLE IF NOT EXISTS apphoryn_analytics_events (
                id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                app_slug TEXT,
                artifact_id TEXT,
                platform TEXT,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
        """)
        for row in rows:
            cls._d1_request(
                config["account_id"], config["database_id"], config["token"],
                """INSERT OR IGNORE INTO apphoryn_analytics_events
                   (id,event_type,app_slug,artifact_id,platform,details_json,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                [row["id"], row["event_type"], row["app_slug"], row["artifact_id"], row["platform"],
                 json.dumps(row["details"], separators=(",", ":")), row["created_at"]],
            )

    def sync_once(self, limit: int = 100) -> dict[str, Any]:
        provider, config = self._backend_config()
        if provider == "local_sqlite":
            return {"provider": provider, "synced": 0, "status": "local_only"}
        rows = self._pending_batch(limit)
        if not rows:
            return {"provider": provider, "synced": 0, "status": "up_to_date"}
        ids = [row["id"] for row in rows]
        try:
            if provider == "postgres":
                self._sync_postgres(config["dsn"], rows)
            else:
                self._sync_d1(config, rows)
            self._mark_synced(ids)
            return {"provider": provider, "synced": len(ids), "status": "ok"}
        except Exception as exc:
            self._mark_sync_failed(ids, f"{type(exc).__name__}: {str(exc)[:400]}")
            return {"provider": provider, "synced": 0, "status": "retry_pending",
                    "error": f"{type(exc).__name__}: {str(exc)[:300]}"}


analytics = AnalyticsStore()
