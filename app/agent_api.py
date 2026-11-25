from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from .config import settings
from . import ai_router
from .agent_system import AGENTS, STORE
from .resource_manager import resource_status, dispatch_security_scan
from .analytics import analytics

router = APIRouter(prefix="/api/v1/admin/agents", tags=["AI workforce"])


def _require_admin(token: str | None) -> None:
    import hmac
    if not settings.admin_token:
        raise HTTPException(503, "Agent administration is disabled until APPHORYN_ADMIN_TOKEN is configured.")
    if not token or not hmac.compare_digest(token, settings.admin_token):
        raise HTTPException(401, "A valid Apphoryn admin token is required.")


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
def dashboard(x_apphoryn_admin: str | None = Header(default=None)):
    _require_admin(x_apphoryn_admin)
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
    }


@router.post("/tasks")
def create_task(payload: CreateAgentRun, x_apphoryn_admin: str | None = Header(default=None)):
    _require_admin(x_apphoryn_admin)
    return STORE.enqueue(payload.goal, payload.priority, payload.allow_external_actions,
                         payload.max_attempts, payload.allow_code_changes)


@router.get("/tasks/{run_id}")
def get_task(run_id: str, x_apphoryn_admin: str | None = Header(default=None)):
    _require_admin(x_apphoryn_admin)
    run = STORE.get_run(run_id)
    if not run:
        raise HTTPException(404, "Agent run not found.")
    return {"run": run, "tasks": STORE.list_tasks(run_id)}


@router.post("/providers/refresh")
def refresh_providers(x_apphoryn_admin: str | None = Header(default=None)):
    _require_admin(x_apphoryn_admin)
    return ai_router.public_model_catalog(force=True)


@router.post("/providers/{provider}/test")
def test_provider(provider: str, x_apphoryn_admin: str | None = Header(default=None)):
    _require_admin(x_apphoryn_admin)
    return ai_router.test_provider(provider)

@router.post("/providers/benchmark")
def benchmark_providers(x_apphoryn_admin: str | None = Header(default=None)):
    _require_admin(x_apphoryn_admin)
    return ai_router.benchmark_models(max_models=3)


@router.post("/schedules")
def create_schedule(payload: CreateAgentSchedule, x_apphoryn_admin: str | None = Header(default=None)):
    _require_admin(x_apphoryn_admin)
    return STORE.create_schedule(payload.goal, payload.interval_seconds, payload.priority)


@router.post("/schedules/{schedule_id}/enabled")
def set_schedule(schedule_id: str, enabled: bool, x_apphoryn_admin: str | None = Header(default=None)):
    _require_admin(x_apphoryn_admin)
    if not STORE.set_schedule_enabled(schedule_id, enabled):
        raise HTTPException(404, "Agent schedule not found.")
    return {"id": schedule_id, "enabled": enabled}


class GitHubScanRequest(BaseModel):
    repository_url: str = Field(min_length=20, max_length=300)
    git_ref: str = Field(default="HEAD", min_length=1, max_length=180)
    approval: bool = False


@router.get("/resources")
def resources(x_apphoryn_admin: str | None = Header(default=None)):
    _require_admin(x_apphoryn_admin)
    return resource_status()


@router.post("/resources/github-security-scan")
def github_security_scan(payload: GitHubScanRequest, x_apphoryn_admin: str | None = Header(default=None)):
    _require_admin(x_apphoryn_admin)
    try:
        return dispatch_security_scan(payload.repository_url, payload.git_ref, payload.approval)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.post("/analytics/sync")
def sync_analytics(x_apphoryn_admin: str | None = Header(default=None)):
    _require_admin(x_apphoryn_admin)
    return analytics.sync_once()
