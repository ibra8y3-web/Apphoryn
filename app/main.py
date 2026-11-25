from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import tempfile
import uuid
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query, Request
from starlette.background import BackgroundTask
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Literal

from .config import ROOT, settings
from . import ai_router
from .agent_api import router as agent_router
from .agent_system import start_agent_workers, stop_agent_workers
from .database import db
from .services import BuildService, CatalogService, store_uploaded_artifact
from .github_release_mirror import GitHubReleaseMirror
from .analytics import analytics

app = FastAPI(title="Apphoryn API", version="0.2.0-alpha", description="Discover, build, verify and distribute open-source software.")
cors_origins = [origin.strip() for origin in os.getenv("APPHORYN_CORS_ORIGINS", "").split(",") if origin.strip()]
if cors_origins:
    app.add_middleware(CORSMiddleware, allow_origins=cors_origins, allow_methods=["GET", "POST"], allow_headers=["*"])
catalog = CatalogService()
builder = BuildService(catalog=catalog)
app.include_router(agent_router)
MIRROR_CATALOG = ROOT / "apps" / "web" / "catalog" / "index.json"


class PublicAnalyticsEvent(BaseModel):
    event_type: Literal["app_view", "catalog_view", "search", "favorite_added", "favorite_removed", "download_requested"]
    app_slug: str | None = Field(default=None, max_length=180)
    artifact_id: str | None = Field(default=None, max_length=180)
    details: dict[str, str | int | float | bool] = Field(default_factory=dict)


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


def read_mirror_catalog() -> list[dict]:
    try:
        payload = json.loads(MIRROR_CATALOG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = payload.get("apps", []) if isinstance(payload, dict) else []
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict) and isinstance(item.get("artifacts"), list)]


def catalog_items(category: str | None = None, query: str | None = None, limit: int = 100) -> list[dict]:
    by_repo: dict[str, dict] = {}
    for item in db.list_apps(limit=200) + read_mirror_catalog():
        key = str(item.get("repository_url") or item.get("slug") or item.get("id") or "")
        if not key:
            continue
        previous = by_repo.get(key)
        if previous is None:
            by_repo[key] = {**item, "artifacts": list(item.get("artifacts") or [])}
            continue
        merged = list(previous.get("artifacts") or [])
        known = {str(artifact.get("sha256") or artifact.get("id")) for artifact in merged}
        for artifact in item.get("artifacts") or []:
            digest = str(artifact.get("sha256") or artifact.get("id"))
            if digest and digest not in known:
                merged.append(artifact)
                known.add(digest)
        by_repo[key] = {**previous, **item, "artifacts": merged}
    rows = list(by_repo.values())
    # The public store lists only applications that have at least one verified installable file.
    rows = [item for item in rows if item.get("artifacts")]
    if category and category != "all":
        rows = [item for item in rows if item.get("category") == category]
    if query:
        term = query.casefold()
        rows = [item for item in rows if term in " ".join(str(item.get(field) or "") for field in ("name", "summary", "description", "owner", "category")).casefold()]
    rows.sort(key=lambda item: str(item.get("name", "")).casefold())
    return rows[:max(1, min(limit, 200))]


def find_mirrored_artifact(artifact_id: str) -> dict | None:
    for app_record in read_mirror_catalog():
        for artifact in app_record.get("artifacts") or []:
            if artifact_id in {str(artifact.get("id") or ""), str(artifact.get("sha256") or "")}:
                return artifact
    return None


@app.get("/api/v1/health")
def health():
    count = sum(1 for item in read_mirror_catalog() if item.get("artifacts")) or len(db.list_apps(limit=200))
    return {"status": "ok", "name": settings.app_name, "version": "0.2.0-alpha", "catalog_count": count}


@app.get("/api/v1/catalog/apps")
def list_apps(category: str | None = None, q: str | None = None, limit: int = Query(80, ge=1, le=200)):
    items = catalog_items(category, q, limit)
    analytics.record_event("catalog_view")
    if q:
        analytics.record_search(q, len(items), source="catalog")
    return {"items": items, "count": len(items)}


@app.get("/api/v1/catalog/apps/{slug}")
def app_detail(slug: str):
    match = next((item for item in catalog_items(limit=200) if item.get("slug") == slug or item.get("id") == slug), None)
    if not match:
        raise HTTPException(404, "Application not found.")
    analytics.record_event("app_view", app_slug=str(match.get("slug") or slug))
    return match


_analytics_rate_limit: dict[str, tuple[int, int]] = {}


def _allow_analytics_request(request: Request) -> bool:
    # The source IP is hashed only in process memory for a short rate-limit window.
    host = request.client.host if request.client else "unknown"
    key = hashlib.sha256(host.encode("utf-8", errors="ignore")).hexdigest()
    minute = int(time.time() // 60)
    window, count = _analytics_rate_limit.get(key, (minute, 0))
    if window != minute:
        window, count = minute, 0
    if count >= 90:
        return False
    _analytics_rate_limit[key] = (window, count + 1)
    if len(_analytics_rate_limit) > 10000:
        for old_key, (old_window, _) in list(_analytics_rate_limit.items()):
            if old_window < minute - 1:
                _analytics_rate_limit.pop(old_key, None)
    return True


@app.post("/api/v1/analytics/events")
def record_public_analytics_event(payload: PublicAnalyticsEvent, request: Request):
    if not _allow_analytics_request(request):
        raise HTTPException(429, "Analytics event rate limit exceeded.")
    app_slug = (payload.app_slug or "").strip()
    if payload.event_type in {"app_view", "favorite_added", "favorite_removed", "download_requested"}:
        if not app_slug:
            raise HTTPException(422, "app_slug is required for this event.")
        match = next((item for item in catalog_items(limit=200) if item.get("slug") == app_slug), None)
        if not match:
            raise HTTPException(404, "Application not found.")
        artifact_id = (payload.artifact_id or "").strip()
        if payload.event_type == "download_requested":
            if not artifact_id or not any(
                artifact_id in {str(artifact.get("id") or ""), str(artifact.get("sha256") or "")}
                for artifact in match.get("artifacts") or []
            ):
                raise HTTPException(422, "A verified artifact belonging to this app is required.")
        analytics.record_event(payload.event_type, app_slug=app_slug,
                               artifact_id=artifact_id or None, details=payload.details)
        return {"accepted": True}
    if payload.event_type == "search":
        query = str(payload.details.get("query") or "").strip()[:180]
        if len(query) >= 2:
            analytics.record_search(query, 0, source="storefront")
        return {"accepted": True}
    analytics.record_event("catalog_view", details=payload.details)
    return {"accepted": True}


@app.get("/api/v1/discovery/search")
def discovery_search(q: str = Query(..., min_length=2, max_length=120), limit: int = Query(20, ge=1, le=50)):
    try:
        items = catalog.discover(q, limit)
        analytics.record_search(q, len(items), source="github")
        return {"items": items, "query": q}
    except Exception as exc:
        raise HTTPException(502, f"GitHub discovery failed: {exc}") from exc


@app.post("/api/v1/admin/import")
def import_repository(payload: ImportRequest, x_apphoryn_admin: str | None = Header(default=None)):
    require_admin(x_apphoryn_admin)
    try:
        result = catalog.import_repository(payload.repository_url, payload.mirror_release_assets)
        if settings.github_token and settings.github_owner and settings.github_repository:
            mirror = GitHubReleaseMirror(settings.github_token, f"{settings.github_owner}/{settings.github_repository}", settings.github_ref)
            result["mirror"] = mirror.publish_app(result["app"], result["artifacts"])
        return result
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


_RELEASE_ASSET_HOSTS = {
    "github.com", "objects.githubusercontent.com",
    "release-assets.githubusercontent.com", "github-releases.githubusercontent.com",
}


class _ApphorynReleaseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlparse(newurl)
        if parsed.scheme != "https" or parsed.hostname not in _RELEASE_ASSET_HOSTS:
            raise urllib.error.HTTPError(
                newurl, code, "Release redirect target is not an approved HTTPS asset host.", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _is_apphoryn_release_url(url: str, owner: str | None = None, repository: str | None = None) -> bool:
    parsed = urllib.parse.urlparse(url)
    owner = (settings.github_owner if owner is None else owner).strip()
    repository = (settings.github_repository if repository is None else repository).strip()
    expected_path = f"/{owner}/{repository}/releases/download/"
    return (
        bool(owner and repository)
        and parsed.scheme == "https"
        and parsed.hostname == "github.com"
        and parsed.path.startswith(expected_path)
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )


@app.get("/api/v1/downloads/{artifact_id}")
def download_artifact(artifact_id: str):
    """Serve Apphoryn-owned package bytes; never send the client to GitHub or an upstream site."""
    artifact = db.get_artifact(artifact_id)
    if artifact:
        path = Path(artifact["storage_path"]).resolve()
        storage_root = settings.storage_path.resolve()
        if storage_root in path.parents and path.is_file():
            app_record = db.get_app(str(artifact.get("app_id") or "")) or {}
            app_slug = str(app_record.get("slug") or "")
            analytics.record_event("download_requested", app_slug=app_slug,
                                   artifact_id=artifact_id, platform=str(artifact.get("platform") or ""))
            return FileResponse(
                path, filename=artifact["filename"], media_type="application/octet-stream",
                headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"},
                background=BackgroundTask(
                    analytics.record_event, "download_completed", app_slug=app_slug,
                    artifact_id=artifact_id, platform=str(artifact.get("platform") or "")
                ),
            )

    mirrored = find_mirrored_artifact(artifact_id)
    if not mirrored:
        raise HTTPException(404, "Artifact file is unavailable.")
    url = str(mirrored.get("download_url") or "")
    if not _is_apphoryn_release_url(url):
        raise HTTPException(404, "Only Apphoryn-owned release assets can be served by this endpoint.")

    filename = Path(str(mirrored.get("filename") or mirrored.get("name") or "application.bin")).name
    app_record = next((item for item in read_mirror_catalog()
                       if any(str(row.get("id") or "") == artifact_id or str(row.get("sha256") or "") == artifact_id
                              for row in item.get("artifacts") or [])), {})
    app_slug = str(app_record.get("slug") or "")
    request = urllib.request.Request(
        url, headers={"User-Agent": "Apphoryn-Store/1.0", "Accept": "application/octet-stream"}
    )
    opener = urllib.request.build_opener(_ApphorynReleaseRedirects())
    try:
        response = opener.open(request, timeout=60)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise HTTPException(502, "Apphoryn could not retrieve its stored release asset.") from exc

    final_url = urllib.parse.urlparse(response.geturl())
    if final_url.scheme != "https" or final_url.hostname not in _RELEASE_ASSET_HOSTS:
        response.close()
        raise HTTPException(502, "Release asset resolved to an untrusted host.")
    analytics.record_event("download_requested", app_slug=app_slug, artifact_id=artifact_id,
                           platform=str(mirrored.get("platform") or ""))
    declared_length = response.headers.get("Content-Length")
    if declared_length:
        try:
            if int(declared_length) > settings.max_artifact_bytes:
                response.close()
                raise HTTPException(413, "Artifact exceeds the configured download size limit.")
        except ValueError:
            response.close()
            raise HTTPException(502, "Release asset returned an invalid content length.")

    encoded_name = urllib.parse.quote(filename, safe="")
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store",
    }
    if declared_length:
        headers["Content-Length"] = declared_length

    def stream_release_asset():
        total = 0
        completed = False
        try:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    completed = total > 0
                    break
                total += len(chunk)
                if total > settings.max_artifact_bytes:
                    raise RuntimeError("Mirrored release asset exceeded the configured download limit.")
                yield chunk
        finally:
            response.close()
            if completed:
                analytics.record_event("download_completed", app_slug=app_slug, artifact_id=artifact_id,
                                       platform=str(mirrored.get("platform") or ""))

    return StreamingResponse(stream_release_asset(), media_type="application/octet-stream", headers=headers)


@app.get("/api/v1/admin/ai/models")
def ai_models(x_apphoryn_admin: str | None = Header(default=None)):
    """Return model/provider status without ever returning credentials."""
    require_admin(x_apphoryn_admin)
    return ai_router.public_model_catalog()


@app.post("/api/v1/admin/ai/models/refresh")
def refresh_ai_models(x_apphoryn_admin: str | None = Header(default=None)):
    """Force a fresh model discovery against configured providers."""
    require_admin(x_apphoryn_admin)
    return ai_router.public_model_catalog(force=True)


@app.on_event("startup")
def start_background_agent_workers():
    start_agent_workers()


@app.on_event("shutdown")
def stop_background_agent_workers():
    stop_agent_workers()


web_dir = ROOT / "apps" / "web"
app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="storefront")
