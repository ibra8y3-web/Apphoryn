from __future__ import annotations

import hmac
import os
import threading
import time
from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .config import settings
from .netutil import check_admin_token
from . import ai_router
from .agent_system import AGENTS, STORE, autopilot_phase, set_autopilot_schedule
from .resource_manager import resource_status, dispatch_security_scan
from .analytics import analytics
from .discovery_sources import DiscoverySources

router = APIRouter(prefix="/api/v1/admin/agents", tags=["AI workforce"])


def _require_admin(token: str | None, request: Request | None = None) -> None:
    check_admin_token(token, settings.admin_token, request)


class CreateAgentRun(BaseModel):
    goal: str = Field(min_length=8, max_length=12000)
    priority: int = Field(default=5, ge=1, le=10)
    allow_external_actions: bool = False
    allow_code_changes: bool = False
    max_attempts: int = Field(default=2, ge=1, le=3)


class CreateAgentSchedule(BaseModel):
    goal: str = Field(min_length=8, max_length=12000)
    interval_seconds: int = Field(ge=300, le=2592000)
    priority: int = Field(default=5, ge=1, le=10)


@router.get("/dashboard")
def dashboard(x_apphoryn_admin: str | None = Header(default=None), request: Request = None):
    _require_admin(x_apphoryn_admin, request)
    return {
        "agents": AGENTS,
        "agent_activity": STORE.agent_activity(),
        "memories": STORE.read_memories(15),
        "counts": STORE.counts(),
        "runs": STORE.list_runs(30),
        "tasks": STORE.list_tasks(limit=80),
        "workers": STORE.worker_rows(),
        "schedules": STORE.list_schedules(),
        "provider_metrics": STORE.provider_metrics(),
        "model_metrics": STORE.model_metrics(),
        "model_evaluations": STORE.model_evaluation_metrics(),
        "providers": ai_router.public_model_catalog(),
        "resources": resource_status(),
        "analytics": analytics.summary(),
        "automation_enabled": bool(STORE.get_system_state(
            "agent_autopilot_enabled",
            os.getenv("APPHORYN_AGENT_AUTOPILOT_ENABLED", "true").lower() in {"1", "true", "yes", "on"},
        )),
        "shared_catalog_health": STORE.get_system_state("shared_catalog_health", {"status": "starting"}),
        "automation_schedule": autopilot_phase(),
    }


@router.get("/verify")
def verify_admin(x_apphoryn_admin: str | None = Header(default=None), request: Request = None):
    """Lightweight password check used by the admin gate before anything is shown."""
    _require_admin(x_apphoryn_admin, request)
    return {"ok": True}


class AutomationSchedule(BaseModel):
    work_minutes: int = Field(ge=5, le=1440)
    rest_minutes: int = Field(ge=0, le=1440)


@router.post("/automation/schedule")
def save_automation_schedule(
    payload: AutomationSchedule,
    x_apphoryn_admin: str | None = Header(default=None),
    request: Request = None,
):
    _require_admin(x_apphoryn_admin, request)
    return set_autopilot_schedule(payload.work_minutes, payload.rest_minutes)


@router.post("/tasks")
def create_task(payload: CreateAgentRun, x_apphoryn_admin: str | None = Header(default=None), request: Request = None):
    _require_admin(x_apphoryn_admin, request)
    return STORE.enqueue(payload.goal, payload.priority, payload.allow_external_actions,
                         payload.max_attempts, payload.allow_code_changes)


@router.get("/tasks/{run_id}")
def get_task(run_id: str, x_apphoryn_admin: str | None = Header(default=None), request: Request = None):
    _require_admin(x_apphoryn_admin, request)
    run = STORE.get_run(run_id)
    if not run:
        raise HTTPException(404, "Agent run not found.")
    return {"run": run, "tasks": STORE.list_tasks(run_id)}


@router.post("/providers/refresh")
def refresh_providers(x_apphoryn_admin: str | None = Header(default=None), request: Request = None):
    _require_admin(x_apphoryn_admin, request)
    return ai_router.public_model_catalog(force=True)


@router.post("/providers/{provider}/test")
def test_provider(provider: str, x_apphoryn_admin: str | None = Header(default=None), request: Request = None):
    _require_admin(x_apphoryn_admin, request)
    return ai_router.test_provider(provider)

@router.post("/providers/benchmark")
def benchmark_providers(x_apphoryn_admin: str | None = Header(default=None), request: Request = None):
    _require_admin(x_apphoryn_admin, request)
    return ai_router.benchmark_models(max_models=3)


@router.post("/automation/enabled")
def set_automation_enabled(
    enabled: bool = Query(...),
    x_apphoryn_admin: str | None = Header(default=None),
    request: Request = None,
):
    _require_admin(x_apphoryn_admin, request)
    STORE.set_system_state("agent_autopilot_enabled", enabled)
    return {
        "enabled": enabled,
        "message": "Autonomous catalog review is enabled." if enabled else "Autonomous catalog review is paused.",
    }


@router.post("/schedules")
def create_schedule(payload: CreateAgentSchedule, x_apphoryn_admin: str | None = Header(default=None), request: Request = None):
    _require_admin(x_apphoryn_admin, request)
    return STORE.create_schedule(payload.goal, payload.interval_seconds, payload.priority)


@router.post("/schedules/{schedule_id}/enabled")
def set_schedule(schedule_id: str, enabled: bool, x_apphoryn_admin: str | None = Header(default=None), request: Request = None):
    _require_admin(x_apphoryn_admin, request)
    if not STORE.set_schedule_enabled(schedule_id, enabled):
        raise HTTPException(404, "Agent schedule not found.")
    return {"id": schedule_id, "enabled": enabled}


class GitHubScanRequest(BaseModel):
    repository_url: str = Field(min_length=20, max_length=300)
    git_ref: str = Field(default="HEAD", min_length=1, max_length=180)
    approval: bool = False


@router.get("/resources")
def resources(x_apphoryn_admin: str | None = Header(default=None), request: Request = None):
    _require_admin(x_apphoryn_admin, request)
    return resource_status()


@router.post("/resources/github-security-scan")
def github_security_scan(payload: GitHubScanRequest, x_apphoryn_admin: str | None = Header(default=None), request: Request = None):
    _require_admin(x_apphoryn_admin, request)
    try:
        return dispatch_security_scan(payload.repository_url, payload.git_ref, payload.approval)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.post("/analytics/sync")
def sync_analytics(x_apphoryn_admin: str | None = Header(default=None), request: Request = None):
    _require_admin(x_apphoryn_admin, request)
    return analytics.sync_once()

@router.post("/analytics/test")
def test_analytics_backend(x_apphoryn_admin: str | None = Header(default=None), request: Request = None):
    _require_admin(x_apphoryn_admin, request)
    return analytics.test_backend()


@router.get("/sources/search")
def search_official_catalogs(
    q: str = Query(..., min_length=2, max_length=180),
    limit: int = Query(10, ge=1, le=20),
    x_apphoryn_admin: str | None = Header(default=None),
    request: Request = None,
):
    _require_admin(x_apphoryn_admin, request)
    try:
        return DiscoverySources().search(q, limit)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
