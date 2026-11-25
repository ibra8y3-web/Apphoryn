from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from .config import settings
from . import ai_router
from .agent_system import AGENTS, STORE

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
        "counts": STORE.counts(),
        "runs": STORE.list_runs(30),
        "tasks": STORE.list_tasks(limit=80),
        "workers": STORE.worker_rows(),
        "schedules": STORE.list_schedules(),
        "provider_metrics": STORE.provider_metrics(),
        "providers": ai_router.public_model_catalog(),
    }


@router.post("/tasks")
def create_task(payload: CreateAgentRun, x_apphoryn_admin: str | None = Header(default=None)):
    _require_admin(x_apphoryn_admin)
    return STORE.enqueue(payload.goal, payload.priority, payload.allow_external_actions, payload.max_attempts)


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
