from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from .config import ROOT, settings
from .database import db, utc_now
from .domain import license_allows_redistribution
from .github_client import GitHubClient, GitHubError
from .services import CatalogService


DEFAULT_QUERIES = (
    "topic:android-app",
    "topic:desktop-app",
    "topic:windows-app",
    "topic:linux-app",
    "topic:macos-app",
    "topic:productivity-app",
    "topic:utility-app",
    "topic:electron",
    "topic:flutter",
    "topic:command-line-tool",
)


def _default_state() -> dict[str, Any]:
    return {
        "phase": "starting",
        "started_at": None,
        "last_started_at": None,
        "last_completed_at": None,
        "next_scan_at": None,
        "current_query": None,
        "last_reason": "service_start",
        "last_error": None,
        "query_cursor": 0,
        "searches_completed": 0,
        "total_discovered": 0,
        "total_imported": 0,
        "total_packages": 0,
        "last_import_at": None,
        "last_import_epoch": 0.0,
        "events": [],
        "candidates": [],
    }


class AutonomousScout:
    """Persistent, resumable discovery/import worker owned by the Apphoryn API process."""

    def __init__(self) -> None:
        self.path = settings.storage_path / "autonomous-scout.json"
        self.github = GitHubClient()
        self.catalog = CatalogService(github=self.github, database=db)
        self.lock = threading.RLock()
        self.wake_event = threading.Event()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.last_scan_monotonic = 0.0
        self.state = self._load_state()

    def _load_state(self) -> dict[str, Any]:
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                state = _default_state()
                state.update(loaded)
                return state
        except (OSError, json.JSONDecodeError):
            pass
        return _default_state()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(self.state, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(self.path)

    def _event(self, event_type: str, message: str, **details: Any) -> None:
        event = {"at": utc_now(), "type": event_type, "message": message[:500]}
        for key in ("repository_url", "name", "license_spdx", "packages", "query"):
            if key in details and details[key] is not None:
                event[key] = details[key]
        self.state.setdefault("events", []).insert(0, event)
        self.state["events"] = self.state["events"][:40]

    def start(self) -> None:
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.stop_event.clear()
            self.state["started_at"] = self.state.get("started_at") or utc_now()
            self.thread = threading.Thread(
                target=self._run,
                name="apphoryn-autonomous-scout",
                daemon=True,
            )
            self.thread.start()
        self.wake("service_start")

    def stop(self) -> None:
        self.stop_event.set()
        self.wake_event.set()
        thread = self.thread
        if thread and thread.is_alive():
            thread.join(timeout=5)

    def wake(self, reason: str = "storefront_open") -> dict[str, Any]:
        with self.lock:
            self.state["last_reason"] = str(reason or "manual_wake")[:80]
            if self.state.get("phase") not in {"searching", "importing"}:
                self.state["phase"] = "queued"
            self._save()
            snapshot = self.status()
        self.wake_event.set()
        return snapshot

    def status(self) -> dict[str, Any]:
        with self.lock:
            public = dict(self.state)
            public.pop("last_import_epoch", None)
            public["worker_alive"] = bool(self.thread and self.thread.is_alive())
            public["github_write_token_configured"] = bool(settings.github_token)
            public["import_interval_seconds"] = (
                0 if settings.github_token else 300
            )
            public["events"] = list(self.state.get("events") or [])[:20]
            public["candidates"] = list(self.state.get("candidates") or [])[:30]
            return public

    def _queries(self) -> tuple[str, ...]:
        configured = tuple(
            line.strip()
            for line in os.getenv("APPHORYN_AUTONOMOUS_SCOUT_QUERIES", "").splitlines()
            if line.strip()
        )
        return configured or DEFAULT_QUERIES

    def _known_manifest_repositories(self) -> set[str]:
        path = ROOT / "apps" / "web" / "catalog" / "index.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            rows = payload.get("apps", []) if isinstance(payload, dict) else []
            return {
                str(row.get("repository_url") or "").rstrip("/").lower()
                for row in rows
                if isinstance(row, dict) and row.get("repository_url")
            }
        except (OSError, json.JSONDecodeError):
            return set()

    def _upsert_candidate(self, candidate: dict[str, Any]) -> None:
        rows = self.state.setdefault("candidates", [])
        url = str(candidate.get("repository_url") or "").rstrip("/").lower()
        if not url:
            return
        for index, old in enumerate(rows):
            if str(old.get("repository_url") or "").rstrip("/").lower() == url:
                rows[index] = {**old, **candidate}
                return
        rows.insert(0, candidate)
        del rows[100:]

    def _scan_once(self, reason: str) -> None:
        queries = self._queries()
        cursor = int(self.state.get("query_cursor") or 0) % len(queries)
        query = queries[cursor]
        self.state["query_cursor"] = (cursor + 1) % len(queries)
        self.state["phase"] = "searching"
        self.state["current_query"] = query
        self.state["last_started_at"] = utc_now()
        self.state["last_reason"] = reason
        self.state["last_error"] = None
        self._save()

        result = self.github.search_repositories(query=query, page=1, per_page=50)
        repositories = result.get("items") if isinstance(result, dict) else []
        repositories = repositories if isinstance(repositories, list) else []
        known_manifest = self._known_manifest_repositories()
        imported_this_scan = False
        new_candidates = 0
        eligible_candidates: list[dict[str, Any]] = []

        for repo in repositories:
            if not isinstance(repo, dict):
                continue
            full_name = str(repo.get("full_name") or "").strip()
            repository_url = str(repo.get("html_url") or "").strip().rstrip("/")
            if not full_name or not repository_url.startswith("https://github.com/"):
                continue
            license_info = repo.get("license") or {}
            spdx = str(license_info.get("spdx_id") or "").strip()
            eligible = license_allows_redistribution(spdx)
            archived = bool(repo.get("archived") or repo.get("disabled"))
            fork = bool(repo.get("fork"))
            existing = db.get_app_by_repo(repository_url)
            in_manifest = repository_url.lower() in known_manifest
            previous_candidate = next((row for row in self.state.get("candidates", []) if str(row.get("repository_url") or "").rstrip("/").lower() == repository_url.lower()), {})
            retry_after = float(previous_candidate.get("retry_after_epoch") or 0)
            existing_updated = str((existing or {}).get("source_updated_at") or "")
            upstream_updated = str(repo.get("updated_at") or "")
            if archived or fork:
                status = "skipped_inactive_or_fork"
            elif not eligible:
                status = "blocked_license"
            elif retry_after > time.time():
                status = "retry_pending"
            elif in_manifest or (existing and (not upstream_updated or existing_updated == upstream_updated)):
                status = "already_catalogued"
            else:
                status = "update_available" if existing else "discovered"
                new_candidates += 1
            candidate = {
                "name": str(repo.get("name") or full_name.rsplit("/", 1)[-1])[:180],
                "full_name": full_name[:240],
                "description": str(repo.get("description") or "")[:500],
                "repository_url": repository_url,
                "icon_url": (repo.get("owner") or {}).get("avatar_url"),
                "license_spdx": spdx or None,
                "license_eligible": eligible,
                "stars": int(repo.get("stargazers_count") or 0),
                "updated_at": repo.get("updated_at"),
                "topics": list(repo.get("topics") or [])[:20],
                "status": status,
                "query": query,
                "seen_at": utc_now(),
            }
            self._upsert_candidate(candidate)
            if status in {"discovered", "update_available"}:
                eligible_candidates.append(candidate)

        self.state["searches_completed"] = int(self.state.get("searches_completed") or 0) + 1
        self.state["total_discovered"] = int(self.state.get("total_discovered") or 0) + new_candidates
        self._event(
            "search_completed",
            f"GitHub search completed: {len(repositories)} results, {new_candidates} new eligible candidates.",
            query=query,
        )

        # Unauthenticated GitHub API access is limited to 60 core requests/hour.
        # The slower import cadence is removed automatically when a write token is configured.
        import_interval = 0 if settings.github_token else 300
        last_import_epoch = float(self.state.get("last_import_epoch") or 0)
        may_import = time.time() - last_import_epoch >= import_interval
        if may_import:
            for candidate in eligible_candidates:
                repository_url = str(candidate["repository_url"])
                self.state["phase"] = "importing"
                self.state["current_query"] = query
                self._save()
                try:
                    imported = self.catalog.import_repository(
                        repository_url,
                        mirror_release_assets=True,
                    )
                    app_record = imported.get("app") or {}
                    artifacts = imported.get("artifacts") or []
                    self.state["total_imported"] = int(self.state.get("total_imported") or 0) + 1
                    self.state["total_packages"] = int(self.state.get("total_packages") or 0) + len(artifacts)
                    self.state["last_import_at"] = utc_now()
                    self.state["last_import_epoch"] = time.time()
                    candidate["status"] = "imported" if artifacts else "source_added_no_verified_package"
                    candidate["name"] = str(app_record.get("name") or candidate["name"])
                    self._upsert_candidate(candidate)
                    self._event(
                        "import_completed",
                        (
                            f"Imported {candidate['name']} with {len(artifacts)} verified package(s)."
                            if artifacts else
                            f"Registered {candidate['name']} as a source project; no verified installer was available in its latest release."
                        ),
                        repository_url=repository_url,
                        name=candidate["name"],
                        license_spdx=app_record.get("license_spdx"),
                        packages=len(artifacts),
                    )
                    imported_this_scan = True
                    break
                except ValueError as exc:
                    candidate["status"] = "blocked_or_incomplete_source"
                    candidate["reason"] = str(exc)[:300]
                    candidate["retry_after_epoch"] = time.time() + 86400
                    self._upsert_candidate(candidate)
                    self._event(
                        "import_skipped",
                        f"{candidate['name']}: {str(exc)[:300]}",
                        repository_url=repository_url,
                        name=candidate["name"],
                    )
                except Exception as exc:
                    candidate["status"] = "retry_pending"
                    candidate["reason"] = f"{type(exc).__name__}: {str(exc)[:240]}"
                    candidate["retry_after_epoch"] = time.time() + 1800
                    self._upsert_candidate(candidate)
                    self.state["last_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
                    self._event(
                        "import_retry_pending",
                        f"{candidate['name']}: import will be retried after the upstream error clears.",
                        repository_url=repository_url,
                        name=candidate["name"],
                    )
                    continue

        self.state["last_completed_at"] = utc_now()
        self.state["next_scan_at"] = utc_now()
        self.state["phase"] = "idle"
        self.state["current_query"] = None
        if not imported_this_scan and eligible_candidates and not may_import:
            self._event(
                "import_waiting_for_upstream_quota",
                "Search results are saved. Import resumes according to GitHub's unauthenticated API quota.",
            )
        self._save()

    def _run(self) -> None:
        while not self.stop_event.is_set():
            # One search per minute is below GitHub's unauthenticated search limit.
            signaled = self.wake_event.wait(timeout=60)
            self.wake_event.clear()
            if self.stop_event.is_set():
                break
            elapsed = time.monotonic() - self.last_scan_monotonic
            if self.last_scan_monotonic and elapsed < 12:
                self.stop_event.wait(12 - elapsed)
                if self.stop_event.is_set():
                    break
            reason = str(self.state.get("last_reason") or ("scheduled_cycle" if not signaled else "storefront_open"))
            self.last_scan_monotonic = time.monotonic()
            try:
                with self.lock:
                    self._scan_once(reason)
            except GitHubError as exc:
                with self.lock:
                    self.state["phase"] = "rate_limited" if "HTTP 403" in str(exc) or "HTTP 429" in str(exc) else "error"
                    self.state["last_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
                    self.state["last_completed_at"] = utc_now()
                    self._event("search_failed", "GitHub search could not complete; the worker will retry automatically.")
                    self._save()
                self.stop_event.wait(60)
            except Exception as exc:
                with self.lock:
                    self.state["phase"] = "error"
                    self.state["last_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
                    self.state["last_completed_at"] = utc_now()
                    self._event("worker_error", f"Scout cycle failed: {type(exc).__name__}.")
                    self._save()
                self.stop_event.wait(30)


_scout = AutonomousScout()


def start_autonomous_scout() -> None:
    _scout.start()


def stop_autonomous_scout() -> None:
    _scout.stop()


def wake_autonomous_scout(reason: str = "storefront_open") -> dict[str, Any]:
    return _scout.wake(reason)


def autonomous_scout_status() -> dict[str, Any]:
    return _scout.status()
