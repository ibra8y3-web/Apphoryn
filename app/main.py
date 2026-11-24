from __future__ import annotations

import hmac
import os
import tempfile
import uuid
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import ROOT, settings
from .database import db
from .services import BuildService, CatalogService, store_uploaded_artifact

app = FastAPI(title="Apphoryn API", version="0.1.0", description="Discover, build, verify and distribute open-source software.")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"])
catalog = CatalogService()
builder = BuildService(catalog=catalog)


class ImportRequest(BaseModel):
    repository_url: str
    mirror_release_assets: bool = True


class BuildRequest(BaseModel):
    repository_url: str
    git_ref: str = "main"
    targets: list[str] = Field(default_factory=lambda: ["linux"])


def require_admin(token: str | None) -> None:
    if not settings.admin_token:
        raise HTTPException(503, "Admin actions are disabled until APPHORYN_ADMIN_TOKEN is configured.")
    if not token or not hmac.compare_digest(token, settings.admin_token):
        raise HTTPException(401, "A valid Apphoryn admin token is required.")


@app.get("/api/v1/health")
def health():
    return {"status": "ok", "name": settings.app_name, "version": "0.1.0", "catalog_count": len(db.list_apps(limit=200))}


@app.get("/api/v1/catalog/apps")
def list_apps(category: str | None = None, q: str | None = None, limit: int = Query(80, ge=1, le=200)):
    items = db.list_apps(category, q, limit)
    for item in items:
        item["artifacts"] = db.list_artifacts(item["id"])
    return {"items": items, "count": len(items)}


@app.get("/api/v1/catalog/apps/{slug}")
def app_detail(slug: str):
    match = next((item for item in db.list_apps(limit=200) if item["slug"] == slug), None)
    if not match:
        raise HTTPException(404, "Application not found.")
    match["artifacts"] = db.list_artifacts(match["id"])
    return match


@app.get("/api/v1/discovery/search")
def discovery_search(q: str = Query(..., min_length=2, max_length=120), limit: int = Query(20, ge=1, le=50)):
    try:
        return {"items": catalog.discover(q, limit), "query": q}
    except Exception as exc:
        raise HTTPException(502, f"GitHub discovery failed: {exc}") from exc


@app.post("/api/v1/admin/import")
def import_repository(payload: ImportRequest, x_apphoryn_admin: str | None = Header(default=None)):
    require_admin(x_apphoryn_admin)
    try:
        return catalog.import_repository(payload.repository_url, payload.mirror_release_assets)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"Import failed: {exc}") from exc


@app.post("/api/v1/admin/build")
def request_build(payload: BuildRequest, x_apphoryn_admin: str | None = Header(default=None)):
    require_admin(x_apphoryn_admin)
    try:
        return builder.dispatch(payload.repository_url, payload.git_ref, payload.targets)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"Build dispatch failed: {exc}") from exc


@app.get("/api/v1/admin/jobs")
def jobs(x_apphoryn_admin: str | None = Header(default=None)):
    require_admin(x_apphoryn_admin)
    return {"items": db.list_jobs()}


@app.post("/api/v1/admin/artifacts/upload")
async def upload_built_artifact(
    request: Request,
    x_apphoryn_token: str | None = Header(default=None),
    x_apphoryn_app_id: str | None = Header(default=None),
    x_apphoryn_filename: str | None = Header(default=None),
    x_apphoryn_platform: str | None = Header(default=None),
    x_apphoryn_version: str | None = Header(default="unknown"),
    x_apphoryn_source_repo: str | None = Header(default=None),
    x_apphoryn_git_ref: str | None = Header(default="main"),
    x_apphoryn_sha256: str | None = Header(default=None),
    x_apphoryn_job_id: str | None = Header(default=None),
):
    if not settings.ingest_token or not x_apphoryn_token or not hmac.compare_digest(x_apphoryn_token, settings.ingest_token):
        raise HTTPException(401, "Invalid artifact ingest token.")
    required = [x_apphoryn_app_id, x_apphoryn_filename, x_apphoryn_platform, x_apphoryn_source_repo, x_apphoryn_sha256]
    if not all(required):
        raise HTTPException(400, "Missing artifact metadata headers.")
    temp_path = Path(tempfile.gettempdir()) / f"apphoryn-{uuid.uuid4().hex}.upload"
    size = 0
    try:
        with temp_path.open("wb") as output:
            async for chunk in request.stream():
                size += len(chunk)
                if size > settings.max_artifact_bytes:
                    raise HTTPException(413, "Artifact exceeds configured size limit.")
                output.write(chunk)
        item = store_uploaded_artifact(
            app_id=x_apphoryn_app_id,
            filename=x_apphoryn_filename,
            platform=x_apphoryn_platform,
            version=x_apphoryn_version or "unknown",
            source_url=x_apphoryn_source_repo,
            git_ref=x_apphoryn_git_ref or "main",
            data_path=temp_path,
            expected_sha256=x_apphoryn_sha256,
        )
        if x_apphoryn_job_id:
            db.update_job(x_apphoryn_job_id, "artifact_published", f"Published {item['filename']}.")
        return item
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, f"Artifact ingest failed: {exc}") from exc
    finally:
        temp_path.unlink(missing_ok=True)


@app.get("/api/v1/downloads/{artifact_id}")
def download_artifact(artifact_id: str):
    artifact = db.get_artifact(artifact_id)
    if not artifact:
        raise HTTPException(404, "Artifact not found.")
    path = Path(artifact["storage_path"]).resolve()
    storage_root = settings.storage_path.resolve()
    if storage_root not in path.parents or not path.is_file():
        raise HTTPException(404, "Artifact file is unavailable.")
    return FileResponse(path, filename=artifact["filename"], media_type="application/octet-stream")


# Mount the responsive storefront after API routes so it cannot shadow them.
web_dir = ROOT / "apps" / "web"
app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="storefront")
