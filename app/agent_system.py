from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from .config import ROOT, settings
from . import ai_router

AGENTS: list[dict[str, Any]] = [
    {"id": "niebex", "name": "Niebex", "title": "Central AI Brain", "description": "Plans work, selects specialists, coordinates hand-offs and reviews the run.", "tools": ["task_planner", "shared_memory", "provider_router"]},
    {"id": "discovery", "name": "AI Discovery Agent", "title": "Discovery", "description": "Finds real public repositories and identifies new software candidates across GitHub, F-Droid and Flathub.", "tools": ["github_discover", "multi_source_discover", "github_inspect"]},
    {"id": "research", "name": "AI Research Agent", "title": "Research", "description": "Researches repository facts, release history and supplied documentation.", "tools": ["github_inspect", "read_repo_file"]},
    {"id": "writing", "name": "AI Writing Agent", "title": "Writing", "description": "Writes bilingual, evidence-grounded descriptions and documentation.", "tools": ["shared_memory"]},
    {"id": "software_engineer", "name": "AI Software Engineer", "title": "Software Engineering", "description": "Reads source code, drafts bounded changes, creates a dedicated branch and opens a reviewable pull request only with separate code-change approval.", "tools": ["read_repo_file", "apply_code_patch"]},
    {"id": "build", "name": "AI Build Agent", "title": "Build", "description": "Checks build requirements and can dispatch the existing isolated build workflow after approval.", "tools": ["github_inspect", "dispatch_build"]},
    {"id": "import", "name": "AI Import Agent", "title": "Import", "description": "Checks provenance and can import actual release assets after approval.", "tools": ["github_inspect", "import_repository"]},
    {"id": "update", "name": "AI Update Agent", "title": "Updates", "description": "Compares upstream release metadata with Apphoryn's stored version.", "tools": ["github_inspect"]},
    {"id": "debugging", "name": "AI Debugging Agent", "title": "Debugging", "description": "Analyzes supplied logs, stack traces and build errors; can prepare a bounded repair pull request only with separate code-change approval.", "tools": ["read_repo_file", "apply_code_patch"]},
    {"id": "security", "name": "AI Security Analysis Agent", "title": "Security", "description": "Checks repository and local artifact metadata using Apphoryn's real validators.", "tools": ["github_inspect", "read_repo_file", "verify_artifact"]},
    {"id": "supervisor", "name": "AI Supervisor Agent", "title": "Supervisor", "description": "Audits outputs, evidence, missing fields and claims before run completion.", "tools": ["shared_memory", "quality_review"]},
]
AGENT_BY_ID = {item["id"]: item for item in AGENTS}
TERMINAL_RUN_STATES = {"completed", "failed", "cancelled"}
_REPO_RE = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", re.I)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(value: str | None, fallback: Any = None) -> Any:
    try:
        return json.loads(value) if value else fallback
    except (TypeError, json.JSONDecodeError):
        return fallback


class AgentStore:
    """Durable task queue, shared memory and provider telemetry on a separate SQLite DB."""

    def __init__(self, path: str | Path | None = None):
        self.path = str(path or os.getenv("APPHORYN_AGENT_DATABASE", str(ROOT / "storage" / "agent-workforce.sqlite3")))
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA busy_timeout = 30000")
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
            con.execute("PRAGMA journal_mode = WAL")
            con.executescript("""
                CREATE TABLE IF NOT EXISTS agent_runs (
                    id TEXT PRIMARY KEY, goal TEXT NOT NULL, status TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 5, allow_external_actions INTEGER NOT NULL DEFAULT 0,
                    allow_code_changes INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 2,
                    next_attempt_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    started_at TEXT, completed_at TEXT, last_error TEXT, result_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_agent_runs_queue ON agent_runs(status, priority DESC, created_at);
                CREATE TABLE IF NOT EXISTS agent_tasks (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
                    agent_id TEXT NOT NULL, title TEXT NOT NULL, instructions TEXT NOT NULL,
                    status TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 1, provider TEXT, model TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, started_at TEXT, completed_at TEXT,
                    result_json TEXT, tool_result_json TEXT, error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_agent_tasks_run ON agent_tasks(run_id, created_at);
                CREATE TABLE IF NOT EXISTS agent_memories (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, project_key TEXT, content TEXT NOT NULL,
                    source_run_id TEXT, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_memory_scope ON agent_memories(scope, created_at DESC);
                CREATE TABLE IF NOT EXISTS provider_events (
                    id TEXT PRIMARY KEY, provider TEXT NOT NULL, model TEXT, success INTEGER NOT NULL,
                    latency_ms INTEGER, http_status INTEGER, error TEXT, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_provider_events_provider ON provider_events(provider, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_provider_events_model ON provider_events(provider,model,created_at DESC);
                CREATE TABLE IF NOT EXISTS model_evaluations (
                    id TEXT PRIMARY KEY, provider TEXT NOT NULL, model TEXT NOT NULL,
                    task_type TEXT NOT NULL, score REAL NOT NULL, latency_ms INTEGER NOT NULL,
                    checks_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_model_evaluations_lookup ON model_evaluations(provider,model,task_type,created_at DESC);
                CREATE TABLE IF NOT EXISTS agent_workers (
                    worker_id TEXT PRIMARY KEY, status TEXT NOT NULL, current_run_id TEXT,
                    last_heartbeat_at TEXT NOT NULL, started_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_schedules (
                    id TEXT PRIMARY KEY, goal TEXT NOT NULL, interval_seconds INTEGER NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 5, enabled INTEGER NOT NULL DEFAULT 1,
                    next_run_at TEXT NOT NULL, last_run_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_schedules_due ON agent_schedules(enabled, next_run_at);
            """)
            run_columns = {row[1] for row in con.execute("PRAGMA table_info(agent_runs)").fetchall()}
            if "allow_code_changes" not in run_columns:
                con.execute("ALTER TABLE agent_runs ADD COLUMN allow_code_changes INTEGER NOT NULL DEFAULT 0")

    def enqueue(self, goal: str, priority: int = 5, allow_external_actions: bool = False,
                max_attempts: int = 2, allow_code_changes: bool = False) -> dict[str, Any]:
        run_id, now = str(uuid.uuid4()), utc_now()
        with self.connect() as con:
            existing = con.execute(
                """SELECT * FROM agent_runs
                   WHERE lower(trim(goal))=lower(trim(?)) AND status IN ('queued','running','retry')
                   ORDER BY created_at DESC LIMIT 1""",
                (goal.strip(),),
            ).fetchone()
            if existing is not None:
                return self._run_dict(existing) or {}
            con.execute(
                """INSERT INTO agent_runs(id,goal,status,priority,allow_external_actions,allow_code_changes,max_attempts,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (run_id, goal.strip(), "queued", max(1, min(int(priority), 10)), int(bool(allow_external_actions)),
                 int(bool(allow_code_changes)), max(1, min(int(max_attempts), 3)), now, now),
            )
        return self.get_run(run_id) or {}

    @staticmethod
    def _run_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item["allow_external_actions"] = bool(item["allow_external_actions"])
        item["allow_code_changes"] = bool(item.get("allow_code_changes", 0))
        item["result"] = _loads(item.pop("result_json"), None)
        return item

    def claim_run(self, worker_id: str) -> dict[str, Any] | None:
        now = utc_now()
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                """SELECT * FROM agent_runs WHERE status IN ('queued','retry')
                   AND (next_attempt_at IS NULL OR next_attempt_at<=?)
                   ORDER BY priority DESC,created_at ASC LIMIT 1""", (now,)
            ).fetchone()
            if row is None:
                con.execute(
                    """INSERT INTO agent_workers(worker_id,status,current_run_id,last_heartbeat_at,started_at)
                       VALUES(?,'idle',NULL,?,?) ON CONFLICT(worker_id) DO UPDATE SET
                       status='idle',current_run_id=NULL,last_heartbeat_at=excluded.last_heartbeat_at""",
                    (worker_id, now, now),
                )
                return None
            con.execute(
                """UPDATE agent_runs SET status='running',attempts=attempts+1,started_at=COALESCE(started_at,?),
                   updated_at=?,next_attempt_at=NULL WHERE id=?""", (now, now, row["id"])
            )
            con.execute(
                """INSERT INTO agent_workers(worker_id,status,current_run_id,last_heartbeat_at,started_at)
                   VALUES(?,'running',?,?,?) ON CONFLICT(worker_id) DO UPDATE SET status='running',
                   current_run_id=excluded.current_run_id,last_heartbeat_at=excluded.last_heartbeat_at""",
                (worker_id, row["id"], now, now),
            )
            return self._run_dict(con.execute("SELECT * FROM agent_runs WHERE id=?", (row["id"],)).fetchone())

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as con:
            return self._run_dict(con.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone())

    def update_run(self, run_id: str, status: str, *, error: str | None = None, result: Any = None,
                   retry_after_seconds: int | None = None) -> None:
        now = utc_now()
        next_at = (datetime.now(timezone.utc) + timedelta(seconds=retry_after_seconds)).isoformat() if retry_after_seconds else None
        completed = now if status in TERMINAL_RUN_STATES else None
        with self.connect() as con:
            con.execute(
                """UPDATE agent_runs SET status=?,last_error=?,result_json=COALESCE(?,result_json),
                   next_attempt_at=?,completed_at=COALESCE(?,completed_at),updated_at=? WHERE id=?""",
                (status, error, _json(result) if result is not None else None, next_at, completed, now, run_id),
            )

    def create_task(self, run_id: str, agent_id: str, title: str, instructions: str) -> str:
        task_id, now = str(uuid.uuid4()), utc_now()
        with self.connect() as con:
            con.execute(
                """INSERT INTO agent_tasks(id,run_id,agent_id,title,instructions,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,'queued',?,?)""",
                (task_id, run_id, agent_id, title[:180], instructions[:6000], now, now),
            )
        return task_id

    def update_task(self, task_id: str, status: str, *, provider: str | None = None, model: str | None = None,
                    result: Any = None, tool_result: Any = None, error: str | None = None) -> None:
        now = utc_now()
        with self.connect() as con:
            con.execute(
                """UPDATE agent_tasks SET status=?,provider=COALESCE(?,provider),model=COALESCE(?,model),
                   result_json=COALESCE(?,result_json),tool_result_json=COALESCE(?,tool_result_json),
                   error=?,started_at=CASE WHEN ?='running' THEN COALESCE(started_at,?) ELSE started_at END,
                   completed_at=CASE WHEN ? IN ('completed','failed','blocked') THEN ? ELSE completed_at END,
                   updated_at=? WHERE id=?""",
                (status, provider, model, _json(result) if result is not None else None,
                 _json(tool_result) if tool_result is not None else None, error, status, now, status, now, now, task_id),
            )

    def list_tasks(self, run_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as con:
            if run_id:
                rows = con.execute("SELECT * FROM agent_tasks WHERE run_id=? ORDER BY created_at ASC LIMIT ?",
                                   (run_id, max(1, min(limit, 200)))).fetchall()
            else:
                rows = con.execute("SELECT * FROM agent_tasks ORDER BY created_at DESC LIMIT ?",
                                   (max(1, min(limit, 200)),)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["result"] = _loads(item.pop("result_json"), None)
            item["tool_result"] = _loads(item.pop("tool_result_json"), None)
            result.append(item)
        return result

    def list_runs(self, limit: int = 25) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute("SELECT * FROM agent_runs ORDER BY created_at DESC LIMIT ?",
                               (max(1, min(limit, 100)),)).fetchall()
        return [self._run_dict(row) for row in rows if row is not None]

    def save_memory(self, scope: str, content: Any, source_run_id: str | None = None, project_key: str | None = None) -> None:
        with self.connect() as con:
            con.execute(
                "INSERT INTO agent_memories(id,scope,project_key,content,source_run_id,created_at) VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4()), scope, project_key, _json(content), source_run_id, utc_now()),
            )
            con.execute(
                """DELETE FROM agent_memories WHERE id IN
                   (SELECT id FROM agent_memories WHERE scope=? ORDER BY created_at DESC LIMIT -1 OFFSET 500)""",
                (scope,),
            )

    def read_memories(self, limit: int = 8) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute("SELECT scope,project_key,content,source_run_id,created_at FROM agent_memories ORDER BY created_at DESC LIMIT ?",
                               (max(1, min(limit, 20)),)).fetchall()
        return [{**dict(row), "content": _loads(row["content"], row["content"])} for row in rows]

    def record_provider_event(self, provider: str, model: str | None, success: bool,
                              latency_ms: int | None = None, http_status: int | None = None, error: str | None = None) -> None:
        with self.connect() as con:
            con.execute(
                "INSERT INTO provider_events(id,provider,model,success,latency_ms,http_status,error,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), provider, model, int(success), latency_ms, http_status, (error or "")[:500] or None, utc_now()),
            )

    def provider_metrics(self) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                """SELECT provider,COUNT(*) AS requests,SUM(success) AS successes,AVG(latency_ms) AS average_latency_ms,
                   MAX(created_at) AS last_request FROM provider_events GROUP BY provider ORDER BY provider"""
            ).fetchall()
        return [dict(row) for row in rows]

    def model_metrics(self) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                """SELECT e.provider,e.model,
                   COUNT(*) AS requests,SUM(e.success) AS successes,
                   AVG(e.latency_ms) AS average_latency_ms,
                   (SELECT AVG(m.score) FROM model_evaluations m
                    WHERE m.provider=e.provider AND m.model=e.model) AS benchmark_score,
                   MAX(e.created_at) AS last_request
                   FROM provider_events e WHERE e.model IS NOT NULL
                   GROUP BY e.provider,e.model ORDER BY e.provider,e.model"""
            ).fetchall()
        return [dict(row) for row in rows]

    def record_model_evaluation(self, provider: str, model: str, task_type: str,
                                score: float, latency_ms: int, checks: Any) -> None:
        with self.connect() as con:
            con.execute(
                """INSERT INTO model_evaluations
                   (id,provider,model,task_type,score,latency_ms,checks_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (str(uuid.uuid4()), provider, model, task_type[:80],
                 max(0.0, min(float(score), 1.0)), max(0, int(latency_ms)), _json(checks), utc_now()),
            )

    def model_evaluation_metrics(self) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                """SELECT provider,model,task_type,COUNT(*) AS benchmarks,
                   AVG(score) AS average_score,AVG(latency_ms) AS average_latency_ms,
                   MAX(created_at) AS last_benchmark
                   FROM model_evaluations GROUP BY provider,model,task_type"""
            ).fetchall()
        return [dict(row) for row in rows]

    def worker_rows(self) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute("SELECT * FROM agent_workers ORDER BY worker_id").fetchall()
        return [dict(row) for row in rows]

    def agent_activity(self) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                """SELECT t.agent_id,COUNT(*) AS task_count,MAX(t.updated_at) AS last_activity_at
                   FROM agent_tasks t GROUP BY t.agent_id ORDER BY t.agent_id"""
            ).fetchall()
            latest = con.execute(
                """SELECT t.agent_id,t.status FROM agent_tasks t
                   JOIN (SELECT agent_id,MAX(updated_at) AS latest_at FROM agent_tasks GROUP BY agent_id) x
                   ON t.agent_id=x.agent_id AND t.updated_at=x.latest_at"""
            ).fetchall()
        status_by_agent = {row["agent_id"]: row["status"] for row in latest}
        return [{**dict(row), "latest_status": status_by_agent.get(row["agent_id"], "idle")} for row in rows]

    def counts(self) -> dict[str, int]:
        with self.connect() as con:
            runs = con.execute("SELECT status,COUNT(*) n FROM agent_runs GROUP BY status").fetchall()
            tasks = con.execute("SELECT status,COUNT(*) n FROM agent_tasks GROUP BY status").fetchall()
            memory_count = con.execute("SELECT COUNT(*) FROM agent_memories").fetchone()[0]
        return {"runs_total": sum(int(r["n"]) for r in runs), "tasks_total": sum(int(r["n"]) for r in tasks),
                "memory_records": int(memory_count),
                **{f"runs_{r['status']}": int(r["n"]) for r in runs},
                **{f"tasks_{r['status']}": int(r["n"]) for r in tasks}}

    def recover_interrupted(self) -> int:
        now = utc_now()
        with self.connect() as con:
            rows = con.execute("SELECT id FROM agent_runs WHERE status='running'").fetchall()
            ids = [row["id"] for row in rows]
            if not ids:
                return 0
            con.executemany("UPDATE agent_runs SET status='queued',last_error='Recovered after worker restart',updated_at=? WHERE id=?",
                            [(now, run_id) for run_id in ids])
            con.executemany("UPDATE agent_tasks SET status='interrupted',error='Worker restarted during this task',updated_at=? WHERE run_id=? AND status='running'",
                            [(now, run_id) for run_id in ids])
            return len(ids)

    def heartbeat(self, worker_id: str, status: str, current_run_id: str | None = None) -> None:
        now = utc_now()
        with self.connect() as con:
            con.execute(
                """INSERT INTO agent_workers(worker_id,status,current_run_id,last_heartbeat_at,started_at)
                   VALUES(?,?,?,?,?) ON CONFLICT(worker_id) DO UPDATE SET status=excluded.status,
                   current_run_id=excluded.current_run_id,last_heartbeat_at=excluded.last_heartbeat_at""",
                (worker_id, status, current_run_id, now, now),
            )

    def create_schedule(self, goal: str, interval_seconds: int, priority: int = 5) -> dict[str, Any]:
        schedule_id, now = str(uuid.uuid4()), utc_now()
        interval_seconds = max(300, min(int(interval_seconds), 30 * 24 * 3600))
        next_at = (datetime.now(timezone.utc) + timedelta(seconds=interval_seconds)).isoformat()
        with self.connect() as con:
            con.execute(
                """INSERT INTO agent_schedules(id,goal,interval_seconds,priority,enabled,next_run_at,created_at,updated_at)
                   VALUES(?,?,?,?,1,?,?,?)""",
                (schedule_id, goal[:12000], interval_seconds, max(1, min(priority, 10)), next_at, now, now),
            )
        return {"id": schedule_id, "goal": goal, "interval_seconds": interval_seconds, "enabled": True, "next_run_at": next_at}

    def list_schedules(self) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute("SELECT * FROM agent_schedules ORDER BY created_at DESC").fetchall()
        return [{**dict(row), "enabled": bool(row["enabled"])} for row in rows]

    def set_schedule_enabled(self, schedule_id: str, enabled: bool) -> bool:
        with self.connect() as con:
            cursor = con.execute("UPDATE agent_schedules SET enabled=?,updated_at=? WHERE id=?",
                                 (int(enabled), utc_now(), schedule_id))
            return cursor.rowcount > 0

    def enqueue_due_schedules(self) -> int:
        now = datetime.now(timezone.utc)
        created = 0
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            rows = con.execute("SELECT * FROM agent_schedules WHERE enabled=1 AND next_run_at<=?",
                               (now.isoformat(),)).fetchall()
            for row in rows:
                run_id, stamp = str(uuid.uuid4()), utc_now()
                con.execute(
                    """INSERT INTO agent_runs(id,goal,status,priority,allow_external_actions,max_attempts,created_at,updated_at)
                       VALUES(?,?,'queued',?,0,2,?,?)""",
                    (run_id, row["goal"], row["priority"], stamp, stamp),
                )
                next_at = (now + timedelta(seconds=int(row["interval_seconds"]))).isoformat()
                con.execute("UPDATE agent_schedules SET next_run_at=?,last_run_id=?,updated_at=? WHERE id=?",
                            (next_at, run_id, stamp, row["id"]))
                created += 1
        return created


STORE = AgentStore()


def record_provider_event(provider: str, model: str | None, success: bool, latency_ms: int | None = None,
                          http_status: int | None = None, error: str | None = None) -> None:
    STORE.record_provider_event(provider, model, success, latency_ms, http_status, error)


def _safe_repo_parts(url: str) -> tuple[str, str]:
    parsed = urlparse(url.strip())
    if parsed.scheme != "https" or parsed.hostname != "github.com" or parsed.username or parsed.password:
        raise ValueError("Only canonical https://github.com/OWNER/REPOSITORY URLs are accepted.")
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) != 2 or any(part in {".", ".."} for part in parts):
        raise ValueError("The GitHub repository URL must contain exactly an owner and repository.")
    return parts[0], parts[1].removesuffix(".git")


def _run_tool(agent_id: str, request: dict[str, Any] | None, goal: str, allow_external_actions: bool,
              context: list[dict[str, Any]] | None = None, allow_code_changes: bool = False) -> dict[str, Any] | None:
    allowed = set(AGENT_BY_ID.get(agent_id, {}).get("tools", []))
    if request is None and agent_id == "discovery":
        request = {"name": "github_discover", "arguments": {"query": goal, "limit": 10}}
    if not request:
        return None
    tool_name = str(request.get("name") or "").strip()
    args = request.get("arguments") if isinstance(request.get("arguments"), dict) else {}
    if tool_name not in allowed:
        return {"tool": tool_name or "unknown", "executed": False, "reason": "This tool is not allowed for this employee."}
    try:
        if tool_name == "shared_memory":
            operation = str(args.get("operation") or "read").lower()
            if operation == "read":
                return {"tool": tool_name, "executed": True, "memories": STORE.read_memories(max(1, min(int(args.get("limit") or 8), 12)))}
            if operation == "save":
                content = args.get("content")
                if content is None:
                    raise ValueError("Memory content is required.")
                STORE.save_memory(str(args.get("scope") or "agent")[:60], content,
                                  str(args.get("source_run_id") or "") or None,
                                  str(args.get("project_key") or "") or None)
                return {"tool": tool_name, "executed": True, "saved": True}
            raise ValueError("Shared-memory operation must be read or save.")

        if tool_name == "quality_review":
            rows = context or []
            completed = [row for row in rows if isinstance(row, dict) and isinstance(row.get("result"), dict)]
            claims_without_tool_evidence = []
            unresolved = []
            for row in completed:
                result = row["result"]
                tool_evidence = result.get("verified_tool_result")
                if tool_evidence and not tool_evidence.get("executed"):
                    claims_without_tool_evidence.append(row.get("agent_id"))
                unresolved.extend(result.get("missing_evidence") or [])
            return {"tool": tool_name, "executed": True, "agents_reviewed": len(completed),
                    "agents_without_tool_evidence": claims_without_tool_evidence,
                    "unresolved_evidence": unresolved[:50],
                    "reviewed_agent_ids": [row.get("agent_id") for row in completed]}

        if tool_name == "multi_source_discover":
            from .discovery_sources import DiscoverySources
            query = str(args.get("query") or goal).strip()[:180]
            result = DiscoverySources().search(query, max(1, min(int(args.get("limit") or 10), 20)))
            return {"tool": tool_name, "executed": True, **result}

        if tool_name == "github_discover":
            from .services import CatalogService
            query = str(args.get("query") or goal).strip()[:180]
            rows = CatalogService().discover(query, max(1, min(int(args.get("limit") or 10), 20)))
            return {"tool": tool_name, "executed": True, "query": query, "count": len(rows), "items": rows[:20]}

        if tool_name == "github_inspect":
            from .github_client import GitHubClient
            repo_url = str(args.get("repository_url") or "")
            if not repo_url:
                match = _REPO_RE.search(goal)
                repo_url = match.group(0) if match else ""
            owner, repo = _safe_repo_parts(repo_url)
            client = GitHubClient()
            metadata = client.get_repository(owner, repo)
            release = client.get_latest_release(owner, repo)
            from .database import db
            known = db.get_app_by_repo(f"https://github.com/{owner}/{repo}")
            return {
                "tool": tool_name, "executed": True,
                "repository": {k: metadata.get(k) for k in ("full_name", "html_url", "description", "homepage",
                    "language", "license", "topics", "default_branch", "archived", "updated_at", "stargazers_count")},
                "latest_release": {"tag_name": release.get("tag_name"), "published_at": release.get("published_at"),
                    "html_url": release.get("html_url"),
                    "assets": [{"name": a.get("name"), "size": a.get("size")} for a in release.get("assets", [])[:30]]}
                    if release else None,
                "apphoryn_record": {"latest_version": known.get("latest_version"), "license_spdx": known.get("license_spdx")}
                    if known else None,
            }

        if tool_name == "read_repo_file":
            from .github_client import GitHubClient
            repo_url = str(args.get("repository_url") or "")
            if not repo_url:
                match = _REPO_RE.search(goal)
                repo_url = match.group(0) if match else ""
            owner, repo = _safe_repo_parts(repo_url)
            path = str(args.get("path") or "").strip().strip("/")
            if not path or any(part in {".", ".."} for part in path.split("/")):
                raise ValueError("A safe repository-relative file path is required.")
            ref_value = str(args.get("ref") or "").strip()
            ref_query = f"?ref={quote(ref_value, safe='')}" if ref_value else ""
            encoded_path = "/".join(quote(part, safe="") for part in path.split("/"))
            payload = GitHubClient().request(f"/repos/{owner}/{repo}/contents/{encoded_path}{ref_query}")
            if isinstance(payload, list) or payload.get("encoding") != "base64":
                raise ValueError("GitHub did not return a base64 file payload.")
            content = base64.b64decode(payload.get("content") or "").decode("utf-8", errors="replace")
            max_chars = max(1000, min(int(args.get("max_chars") or 30000), 40000))
            return {"tool": tool_name, "executed": True, "repository": f"{owner}/{repo}", "path": path,
                    "sha": payload.get("sha"), "content": content[:max_chars], "truncated": len(content) > max_chars}


        if tool_name == "apply_code_patch":
            if not allow_code_changes:
                return {"tool": tool_name, "executed": False,
                        "reason": "A separate explicit code-change approval was not granted for this task."}
            if os.getenv("APPHORYN_CODE_WRITES_ENABLED", "false").lower() not in {"1", "true", "yes", "on"}:
                return {"tool": tool_name, "executed": False,
                        "reason": "Code writes are disabled by server policy. Set APPHORYN_CODE_WRITES_ENABLED=true after reviewing the permissions."}
            from .github_client import GitHubClient
            repo_url = str(args.get("repository_url") or "")
            owner, repo = _safe_repo_parts(repo_url)
            files = args.get("files")
            if not isinstance(files, list) or not files or len(files) > 8:
                raise ValueError("Supply between one and eight complete text-file replacements.")
            allow_roots = {
                item.strip().strip("/") for item in
                os.getenv("APPHORYN_CODE_WRITE_ALLOWLIST", "app,tests,scripts,apps/web").split(",")
                if item.strip()
            }
            normalized = []
            total_bytes = 0
            for item in files:
                if not isinstance(item, dict):
                    raise ValueError("Each file change must be an object.")
                path = str(item.get("path") or "").strip()
                content = item.get("content")
                if (not path or path.startswith("/") or "\\" in path
                        or any(part in {"", ".", ".."} for part in path.split("/"))
                        or path.startswith(".github/")
                        or path.lower().startswith(".env")
                        or any(token in path.casefold() for token in ("secret", "credential", "private_key"))
                        or path.lower().endswith((".pem", ".key", ".p12", ".pfx", ".jks"))):
                    raise ValueError(f"Protected or unsafe repository path: {path[:160]}")
                if not any(path == root or path.startswith(root + "/") for root in allow_roots):
                    raise ValueError(f"Path is outside the configured code-write allowlist: {path[:160]}")
                if not isinstance(content, str) or not content.strip() or len(content.encode("utf-8")) > 100_000:
                    raise ValueError(f"File content is empty or exceeds 100 KB: {path[:160]}")
                total_bytes += len(content.encode("utf-8"))
                normalized.append({"path": path, "content": content, "summary": str(item.get("summary") or "")[:240]})
            if len({item["path"] for item in normalized}) != len(normalized):
                raise ValueError("A path may appear only once in a code-change request.")
            if total_bytes > 300_000:
                raise ValueError("Total code-change payload exceeds 300 KB.")
            client = GitHubClient(settings.github_token)
            if not settings.github_token:
                raise RuntimeError("APPHORYN_GITHUB_TOKEN is required for approved code-change pull requests.")
            metadata = client.get_repository(owner, repo)
            if metadata.get("archived") or metadata.get("disabled"):
                raise ValueError("Archived or disabled repositories cannot receive code-change pull requests.")
            base_ref = str(args.get("base_ref") or metadata.get("default_branch") or "main")
            if not re.fullmatch(r"[A-Za-z0-9._/-]{1,180}", base_ref) or ".." in base_ref.split("/"):
                raise ValueError("Invalid target base branch.")
            ref_path = "/repos/" + owner + "/" + repo + "/git/ref/heads/" + quote(base_ref, safe="/")
            base_data = client.request(ref_path)
            base_sha = str((base_data.get("object") or {}).get("sha") or "")
            if not base_sha:
                raise RuntimeError("GitHub did not return the base branch commit.")
            branch_name = f"apphoryn-ai/{uuid.uuid4().hex[:12]}"
            client.mutate("POST", f"/repos/{owner}/{repo}/git/refs",
                          {"ref": "refs/heads/" + branch_name, "sha": base_sha})
            committed = []
            for item in normalized:
                encoded = "/".join(quote(part, safe="") for part in item["path"].split("/"))
                endpoint = f"/repos/{owner}/{repo}/contents/{encoded}"
                existing_sha = None
                try:
                    current = client.request(endpoint, {"ref": branch_name})
                    if isinstance(current, dict):
                        existing_sha = current.get("sha")
                except Exception as exc:
                    if "HTTP 404" not in str(exc):
                        raise
                payload = {
                    "message": "Apphoryn AI: " + (item["summary"] or "propose reviewed source change")[:120],
                    "content": base64.b64encode(item["content"].encode("utf-8")).decode("ascii"),
                    "branch": branch_name,
                }
                if existing_sha:
                    payload["sha"] = existing_sha
                result = client.mutate("PUT", endpoint, payload)
                committed.append({
                    "path": item["path"],
                    "commit_sha": ((result.get("commit") or {}).get("sha")),
                    "blob_sha": ((result.get("content") or {}).get("sha")),
                })
            title = str(args.get("pull_request_title") or "Apphoryn AI proposed source improvements").strip()[:180]
            body = str(args.get("pull_request_body") or "Proposed by Apphoryn's AI Software Engineer.\\n\\nReview all changes, run tests, and merge manually. This automation never writes to the default branch.")[:6000]
            pull = client.mutate("POST", f"/repos/{owner}/{repo}/pulls", {
                "title": title, "head": branch_name, "base": base_ref, "body": body,
                "maintainer_can_modify": False,
            })
            return {
                "tool": tool_name, "executed": True, "repository": f"{owner}/{repo}",
                "base_branch": base_ref, "proposal_branch": branch_name,
                "pull_request_number": pull.get("number"), "pull_request_url": pull.get("html_url"),
                "files_committed": committed,
                "default_branch_modified": False,
                "review_required_before_merge": True,
            }

        if tool_name == "import_repository":
            if not allow_external_actions:
                return {"tool": tool_name, "executed": False, "reason": "Explicit approval for external import actions was not granted."}
            from .services import CatalogService
            repo_url = str(args.get("repository_url") or "")
            _safe_repo_parts(repo_url)
            imported = CatalogService().import_repository(repo_url, mirror_release_assets=True)
            record, artifacts = imported.get("app") or {}, imported.get("artifacts") or []
            mirror_result = None
            if settings.github_token and settings.github_owner and settings.github_repository:
                from .github_release_mirror import GitHubReleaseMirror
                mirror = GitHubReleaseMirror(
                    settings.github_token,
                    f"{settings.github_owner}/{settings.github_repository}",
                    settings.github_ref,
                )
                mirror_result = mirror.publish_app(record, artifacts)
            return {"tool": tool_name, "executed": True,
                    "mirror": mirror_result,
                    "app": {"name": record.get("name"), "repository_url": record.get("repository_url"),
                            "latest_version": record.get("latest_version"), "license_spdx": record.get("license_spdx")},
                    "artifact_count": len(artifacts),
                    "artifacts": [{"filename": a.get("filename"), "platform": a.get("platform"),
                        "sha256": a.get("sha256"), "validation_status": a.get("validation_status")} for a in artifacts],
                    "rejected_assets": imported.get("rejected_assets", [])}

        if tool_name == "dispatch_build":
            if not allow_external_actions:
                return {"tool": tool_name, "executed": False, "reason": "Explicit approval for remote build dispatch was not granted."}
            from .services import BuildService
            repo_url = str(args.get("repository_url") or "")
            _safe_repo_parts(repo_url)
            targets = args.get("targets") if isinstance(args.get("targets"), list) else ["linux"]
            targets = [str(value).lower() for value in targets][:4]
            dispatch = BuildService().dispatch(repo_url, str(args.get("git_ref") or "main")[:180], targets)
            return {"tool": tool_name, "executed": True, "dispatch": dispatch}

        if tool_name == "verify_artifact":
            from .artifact_validation import validate_artifact
            path = Path(str(args.get("path") or "")).resolve()
            storage = settings.storage_path.resolve()
            if not path.is_relative_to(storage) or not path.is_file():
                raise ValueError("Only existing files inside APPHORYN_STORAGE can be verified.")
            return {"tool": tool_name, "executed": True, "path": str(path),
                    "verification": validate_artifact(path, require_apk_signature=True)}
        return {"tool": tool_name, "executed": False, "reason": "Unknown tool."}
    except Exception as exc:
        return {"tool": tool_name, "executed": False, "error": f"{type(exc).__name__}: {str(exc)[:600]}"}


def _agent_call(agent_id: str, goal: str, instructions: str, context: list[dict[str, Any]],
                memories: list[dict[str, Any]], allow_external_actions: bool,
                allow_code_changes: bool = False) -> tuple[dict[str, Any], dict[str, str], dict[str, Any] | None]:
    agent = AGENT_BY_ID[agent_id]
    system = (
        f"You are {agent['name']} ({agent['title']}) inside Apphoryn. {agent['description']} "
        "Use only evidence in the task, shared context and tool results. Never invent facts, sources, test results, "
        "file edits, imports, builds, signatures, quota values, costs or actions. Do not claim code was changed or deployed. "
        "Return one JSON object with keys summary, findings(array), recommendations(array), risks(array), "
        "missing_evidence(array), confidence(low|medium|high), tool_request(object or null). "
        f"Your allowed tools are: {', '.join(agent['tools'])}. Request a tool with {{name, arguments}}; "
        "the orchestrator validates it. Import/build actions require allow_external_actions=true. "
        "Code edits require allow_code_changes=true AND APPHORYN_CODE_WRITES_ENABLED=true; even then only a new branch and pull request may be created, never a direct default-branch push. "
        "The code-write tool rejects workflows, secrets, credentials and paths outside its allowlist."
    )
    payload = {"goal": goal, "assignment": instructions, "agent": {"id": agent_id, "name": agent["name"]},
               "previous_agent_results": context[-8:], "shared_memory": memories,
               "allow_external_actions": allow_external_actions}
    draft, route = ai_router.generate_json([
        {"role": "system", "content": system}, {"role": "user", "content": _json(payload)}
    ])
    request = draft.get("tool_request") if isinstance(draft.get("tool_request"), dict) else None
    tool_result = _run_tool(agent_id, request, goal, allow_external_actions, context, allow_code_changes)
    if agent_id == "supervisor":
        audit = _run_tool("supervisor", {"name": "quality_review", "arguments": {}},
                          goal, allow_external_actions, context, allow_code_changes)
        tool_result = {"agent_requested_tool_result": tool_result, "supervisor_audit": audit}
    if tool_result is not None:
        evidence = {"initial_analysis": draft, "tool_execution_result": tool_result,
                    "instruction": "Produce the final JSON analysis. The tool output is the only proof the operation occurred."}
        final, final_route = ai_router.generate_json([
            {"role": "system", "content": system}, {"role": "user", "content": _json(payload)},
            {"role": "user", "content": _json(evidence)}
        ])
        final["verified_tool_result"] = tool_result
        return final, final_route, tool_result
    draft.pop("tool_request", None)
    draft["verified_tool_result"] = None
    return draft, route, None


def execute_run(run: dict[str, Any]) -> None:
    run_id, goal = str(run["id"]), str(run["goal"])
    allow_actions = bool(run.get("allow_external_actions"))
    allow_code_changes = bool(run.get("allow_code_changes"))
    memories = STORE.read_memories(8)
    existing_tasks = STORE.list_tasks(run_id, limit=200)

    # Reuse a completed Niebex plan when a worker is restarted; otherwise create a new planner task.
    saved_planner = next((
        task for task in reversed(existing_tasks)
        if task["agent_id"] == "niebex" and task["status"] == "completed"
        and isinstance(task.get("result"), dict)
        and isinstance(task["result"].get("plan"), list)
    ), None)
    if saved_planner:
        saved_result = saved_planner["result"]
        plan = {"summary": saved_result.get("summary", ""), "tasks": saved_result["plan"]}
        planner_route = {"provider": saved_planner.get("provider"), "model": saved_planner.get("model")}
        assignments = saved_result["plan"]
    else:
        planner_task = STORE.create_task(
            run_id, "niebex", "Plan and coordinate request",
            "Select specialists and define verifiable deliverables."
        )
        STORE.update_task(planner_task, "running")
        planner_system = (
            "You are Niebex, Apphoryn's central AI brain. Create a concise ordered work plan. Choose only these "
            "specialist IDs: discovery, research, writing, software_engineer, build, import, update, debugging, security. "
            "Select only relevant agents, maximum 8. The Supervisor is added by the system at the end. "
            "Never promise unimplemented code edits or claim an action is complete without tool evidence. "
            "Return JSON: {summary:string,tasks:[{agent_id:string,title:string,instructions:string}]}."
        )
        try:
            plan, planner_route = ai_router.generate_json([
                {"role": "system", "content": planner_system},
                {"role": "user", "content": _json({"goal": goal, "agents": [
                    {"id": a["id"], "title": a["title"], "description": a["description"], "tools": a["tools"]}
                    for a in AGENTS if a["id"] != "niebex"
                ], "shared_memory": memories, "allow_external_actions": allow_actions})}
            ])
        except Exception as exc:
            STORE.update_task(planner_task, "failed", error=f"{type(exc).__name__}: {str(exc)[:700]}")
            raise

        planned = plan.get("tasks") if isinstance(plan.get("tasks"), list) else []
        assignments: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in planned:
            if not isinstance(item, dict):
                continue
            agent_id = str(item.get("agent_id") or "").strip()
            if agent_id not in AGENT_BY_ID or agent_id in {"niebex", "supervisor"} or agent_id in seen:
                continue
            seen.add(agent_id)
            assignments.append({
                "agent_id": agent_id,
                "title": str(item.get("title") or AGENT_BY_ID[agent_id]["title"])[:180],
                "instructions": str(item.get("instructions") or goal)[:6000],
            })
            if len(assignments) >= 8:
                break
        if not assignments:
            assignments = [
                {"agent_id": "research", "title": "Research the request", "instructions": goal},
                {"agent_id": "writing", "title": "Prepare a structured result", "instructions": goal},
            ]
        assignments.append({
            "agent_id": "supervisor",
            "title": "Review evidence and final result",
            "instructions": "Audit preceding agent outputs and flag unsupported claims and unresolved requirements.",
        })
        STORE.update_task(
            planner_task, "completed",
            provider=planner_route.get("provider"), model=planner_route.get("model"),
            result={"summary": str(plan.get("summary") or "")[:1200], "plan": assignments},
        )

    # Preserve completed specialist outputs across process restarts and continue at the first unfinished agent.
    completed_by_agent: dict[str, dict[str, Any]] = {}
    for task in existing_tasks:
        if task["agent_id"] not in {"niebex"} and task["status"] == "completed" and isinstance(task.get("result"), dict):
            completed_by_agent[task["agent_id"]] = task

    context: list[dict[str, Any]] = []
    for assignment in assignments:
        agent_id = assignment["agent_id"]
        completed_task = completed_by_agent.get(agent_id)
        if completed_task:
            context.append({
                "agent_id": agent_id,
                "title": completed_task["title"],
                "provider": completed_task.get("provider"),
                "model": completed_task.get("model"),
                "result": completed_task["result"],
            })
            continue

        task_id = STORE.create_task(run_id, agent_id, assignment["title"], assignment["instructions"])
        STORE.update_task(task_id, "running")
        try:
            result, route, tool_result = _agent_call(
                agent_id, goal, assignment["instructions"], context, memories, allow_actions, allow_code_changes
            )
            STORE.update_task(
                task_id, "completed", provider=route.get("provider"), model=route.get("model"),
                result=result, tool_result=tool_result,
            )
            context.append({
                "agent_id": agent_id, "title": assignment["title"],
                "provider": route.get("provider"), "model": route.get("model"), "result": result,
            })
            STORE.heartbeat(threading.current_thread().name, "running", run_id)
        except Exception as exc:
            STORE.update_task(task_id, "failed", error=f"{type(exc).__name__}: {str(exc)[:700]}")
            raise

    final_result = {
        "plan_summary": str(plan.get("summary") or "")[:1200],
        "agent_results": context,
        "completed_agent_count": len(context),
        "external_actions_approved": allow_actions,
        "resumed_from_saved_tasks": bool(existing_tasks),
    }
    STORE.update_run(run_id, "completed", result=final_result)
    repo_match = _REPO_RE.search(goal)
    project_key = f"{repo_match.group(1)}/{repo_match.group(2)}" if repo_match else None
    STORE.save_memory("run", {"goal": goal[:4000], "result": final_result}, run_id, project_key)


_stop_event = threading.Event()
_worker_lock = threading.Lock()
_analytics_sync_lock = threading.Lock()
_last_analytics_sync = 0.0
_workers: list[threading.Thread] = []


def _worker_loop(worker_id: str) -> None:
    global _last_analytics_sync
    while not _stop_event.is_set():
        try:
            if time.monotonic() - _last_analytics_sync >= 30 and _analytics_sync_lock.acquire(blocking=False):
                try:
                    from .analytics import analytics
                    analytics.sync_once(limit=100)
                    _last_analytics_sync = time.monotonic()
                finally:
                    _analytics_sync_lock.release()
            STORE.enqueue_due_schedules()
            run = STORE.claim_run(worker_id)
            if run is None:
                _stop_event.wait(2.0)
                continue
            try:
                execute_run(run)
                STORE.heartbeat(worker_id, "idle", None)
            except Exception as exc:
                message = f"{type(exc).__name__}: {str(exc)[:900]}"
                if int(run.get("attempts") or 1) < int(run.get("max_attempts") or 2):
                    STORE.update_run(run["id"], "retry", error=message, retry_after_seconds=30)
                else:
                    STORE.update_run(run["id"], "failed", error=message)
                STORE.heartbeat(worker_id, "error", None)
        except Exception:
            STORE.heartbeat(worker_id, "error", None)
            _stop_event.wait(5.0)


def start_agent_workers() -> None:
    with _worker_lock:
        if any(worker.is_alive() for worker in _workers):
            return
        STORE.recover_interrupted()
        _stop_event.clear()
        count = max(1, min(int(os.getenv("APPHORYN_AGENT_WORKERS", "2")), 4))
        for index in range(count):
            worker_id = f"agent-worker-{index + 1}"
            thread = threading.Thread(target=_worker_loop, args=(worker_id,), name=worker_id, daemon=True)
            _workers.append(thread)
            thread.start()


def stop_agent_workers() -> None:
    _stop_event.set()
    for worker in list(_workers):
        if worker.is_alive():
            worker.join(timeout=3.0)
    with _worker_lock:
        _workers.clear()
