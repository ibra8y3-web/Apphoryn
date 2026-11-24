from __future__ import annotations

import hashlib
import os
import re
import shutil
import uuid
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .config import settings
from .ai_descriptions import DescriptionGenerator
from .artifact_validation import ArtifactValidationError, validate_artifact
from .github_release_mirror import GitHubMirrorError, GitHubReleaseMirror
from .database import Database, db, utc_now
from .domain import (
    canonical_repository_url, classify_artifact, infer_category, license_allows_redistribution,
    repository_slug, safe_filename, sha256_file, valid_git_ref,
)
from .github_client import GitHubClient

ALLOWED_DOWNLOAD_HOSTS = {
    "github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com",
    "github-releases.githubusercontent.com", "githubusercontent.com",
}


def _repository_parts(repository_url: str) -> tuple[str, str]:
    path = urllib.parse.urlparse(repository_url).path.strip("/")
    owner, repo = path.split("/", 1)
    return owner, repo


class _SafeGitHubRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlparse(newurl)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_DOWNLOAD_HOSTS:
            raise urllib.error.HTTPError(newurl, code, "Redirect target is not an approved GitHub asset host", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download_asset(url: str, destination: Path, max_bytes: int) -> tuple[int, str]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {"github.com", "api.github.com"}:
        raise ValueError("Release asset URL is not an approved GitHub HTTPS URL.")
    request = urllib.request.Request(url, headers={"User-Agent": "Apphoryn-Importer/0.1", "Accept": "application/octet-stream"})
    opener = urllib.request.build_opener(_SafeGitHubRedirects())
    size = 0
    digest = hashlib.sha256()
    try:
        with opener.open(request, timeout=60) as response:
            final_host = urllib.parse.urlparse(response.geturl()).hostname or ""
            if final_host not in ALLOWED_DOWNLOAD_HOSTS:
                raise ValueError("Unexpected download redirect host; asset rejected.")
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > max_bytes:
                raise ValueError("Release asset exceeds configured size limit.")
            with destination.open("wb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError("Release asset exceeded configured size limit while downloading.")
                    output.write(chunk)
                    digest.update(chunk)
    except (urllib.error.URLError, TimeoutError) as exc:
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"Could not download release asset: {exc}") from exc
    return size, digest.hexdigest()


class CatalogService:
    def __init__(self, github: GitHubClient | None = None, database: Database = db):
        self.github = github or GitHubClient()
        self.database = database

    def _resolve_project_icon(self, owner: str, repository: str, branch: str, fallback: str | None) -> str | None:
        """Prefer a small, clearly named application artwork file from the source repository."""
        try:
            ref = urllib.parse.quote(branch or "main", safe="")
            payload = self.github.request(f"/repos/{owner}/{repository}/git/trees/{ref}?recursive=1")
            tree = payload.get("tree", []) if isinstance(payload, dict) else []
            candidates: list[tuple[int, str]] = []
            image_suffixes = {".png", ".webp", ".jpg", ".jpeg", ".svg"}
            for item in tree:
                if not isinstance(item, dict) or item.get("type") != "blob":
                    continue
                path = str(item.get("path") or "")
                lower = path.lower()
                suffix = Path(lower).suffix
                if suffix not in image_suffixes:
                    continue
                size = int(item.get("size") or 0)
                if size <= 0 or size > 1024 * 1024:
                    continue
                name = Path(lower).name
                parts = set(Path(lower).parts)
                if any(term in lower for term in ("screenshot", "banner", "cover", "social-preview", "og-image", "readme")):
                    continue
                score = 0
                if name in {"app-icon.png", "app_icon.png", "appicon.png", "icon.png", "icon.svg", "logo.png", "logo.svg"}:
                    score += 100
                elif "icon" in name:
                    score += 80
                elif "logo" in name:
                    score += 60
                else:
                    continue
                if parts.intersection({"assets", "icons", "icon", "res", "resources"}):
                    score += 15
                if parts.intersection({"docs", "examples", "screenshots", "website"}):
                    score -= 40
                if size > 512 * 1024:
                    score -= 10
                candidates.append((score, path))
            if candidates:
                candidates.sort(key=lambda item: (-item[0], len(item[1]), item[1].casefold()))
                selected = candidates[0][1]
                encoded_path = "/".join(urllib.parse.quote(part, safe="") for part in selected.split("/"))
                return f"https://raw.githubusercontent.com/{owner}/{repository}/{ref}/{encoded_path}"
        except Exception:
            # Project artwork is optional; source-owner avatar remains the safe fallback.
            pass
        return fallback

    def discover(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        search = self.github.search_repositories(query=query, per_page=limit)
        candidates = []
        for repo in search.get("items", []):
            license_info = repo.get("license") or {}
            spdx = license_info.get("spdx_id")
            topics = repo.get("topics") or []
            candidates.append({
                "name": repo.get("name", ""),
                "full_name": repo.get("full_name", ""),
                "description": repo.get("description") or "",
                "repository_url": repo.get("html_url", ""),
                "icon_url": (repo.get("owner") or {}).get("avatar_url") or None,
                "homepage_url": repo.get("homepage") or None,
                "license_spdx": spdx,
                "license_eligible": license_allows_redistribution(spdx),
                "archived": bool(repo.get("archived")),
                "stars": int(repo.get("stargazers_count") or 0),
                "updated_at": repo.get("updated_at"),
                "default_branch": repo.get("default_branch") or "main",
                "topics": topics,
            })
        return candidates

    def import_repository(self, repository_url: str, mirror_release_assets: bool = True) -> dict[str, Any]:
        canonical = canonical_repository_url(repository_url)
        owner, repo_name = _repository_parts(canonical)
        repo = self.github.get_repository(owner, repo_name)
        license_info = repo.get("license") or {}
        spdx = license_info.get("spdx_id")
        eligible = license_allows_redistribution(spdx)
        if not eligible:
            raise ValueError(f"Automatic redistribution is blocked: license '{spdx or 'missing'}' is not on Apphoryn's approved allowlist.")
        if repo.get("archived"):
            raise ValueError("Archived repositories are not automatically imported.")

        release = self.github.get_latest_release(owner, repo_name)
        version = (release or {}).get("tag_name")
        readme_excerpt = ""
        license_text = ""
        try:
            license_response = self.github.request(f"/repos/{owner}/{repo_name}/license")
            encoded_license = license_response.get("content") or ""
            if encoded_license:
                import base64
                license_text = base64.b64decode(encoded_license).decode("utf-8", errors="replace")[:100000]
        except Exception:
            pass
        try:
            readme = self.github.request(f"/repos/{owner}/{repo_name}/readme")
            import base64
            readme_excerpt = base64.b64decode(readme.get("content", "")).decode("utf-8", errors="replace")[:12000]
        except Exception:
            pass
        source_commit_sha = None
        try:
            commit = self.github.request(f"/repos/{owner}/{repo_name}/commits/{repo.get('default_branch') or 'main'}")
            candidate_sha = str(commit.get("sha") or "")
            if re.fullmatch(r"[0-9a-fA-F]{40,64}", candidate_sha):
                source_commit_sha = candidate_sha.lower()
        except Exception:
            pass
        if not license_text.strip():
            raise ValueError("The SPDX license is recognized, but its upstream license text could not be retrieved; automatic redistribution is blocked to preserve license notices.")
        category = infer_category(repo.get("topics") or [], repo.get("description"))
        generated = DescriptionGenerator().generate(name=repo.get("name") or repo_name, owner=owner, description=repo.get("description") or "", topics=repo.get("topics") or [], readme_excerpt=readme_excerpt, category=category)
        record = {
            "id": str(uuid.uuid4()),
            "slug": repository_slug(canonical),
            "name": repo.get("name") or repo_name,
            "summary": generated["summary"],
            "description": generated["description"],
            "repository_url": canonical,
            "github_repo_id": str(repo.get("id") or "") or None,
            "source_updated_at": repo.get("updated_at"),
            "homepage_url": repo.get("homepage") or None,
            "icon_url": self._resolve_project_icon(
                owner, repo_name, repo.get("default_branch") or "main",
                (repo.get("owner") or {}).get("avatar_url") or None,
            ),
            "owner": owner,
            "default_branch": repo.get("default_branch") or "main",
            "source_commit_sha": source_commit_sha,
            "license_text": license_text,
            "license_spdx": spdx,
            "license_eligible": True,
            "category": category,
            "topics": repo.get("topics") or [],
            "translations": generated.get("translations", {}),
            "description_translations": generated.get("description_translations", {}),
            "translation_status": generated.get("translation_status", "missing"),
            "ai_provider": generated.get("ai_provider"),
            "ai_model": generated.get("ai_model"),
            "project_kind": generated.get("project_kind", "unknown"),
            "build_recommended": bool(generated.get("build_recommended", False)),
            "classification_source": generated.get("classification_source", "heuristic"),
            "classification_reason": generated.get("classification_reason", ""),
            "latest_version": version,
            "release_url": (release or {}).get("html_url"),
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
        app = self.database.upsert_app(record)
        imported = []
        rejected_assets = []
        if release and mirror_release_assets:
            for asset in release.get("assets", []):
                filename = safe_filename(asset.get("name") or "")
                if "google-play" in filename.lower() or "symbols" in filename.lower():
                    continue
                classification = classify_artifact(filename)
                if not classification:
                    continue
                download_url = asset.get("browser_download_url") or ""
                destination_dir = settings.storage_path / app["id"]
                destination_dir.mkdir(parents=True, exist_ok=True)
                temporary = destination_dir / f".{uuid.uuid4().hex}.part{Path(filename).suffix.lower()}"
                version_dir = re.sub(r"[^A-Za-z0-9._+-]+", "_", str(version or "unknown"))[:100] or "unknown"
                target_dir = destination_dir / version_dir
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / f"{uuid.uuid4().hex[:8]}-{filename}"
                try:
                    size, digest = _download_asset(download_url, temporary, settings.max_import_bytes)
                    upstream_digest = asset.get("digest") or ""
                    upstream_verified = False
                    if upstream_digest.startswith("sha256:"):
                        expected = upstream_digest.split(":", 1)[1].lower()
                        if expected != digest:
                            raise ArtifactValidationError(f"SHA-256 mismatch for upstream asset {filename}.")
                        upstream_verified = True
                    verification = validate_artifact(temporary, require_apk_signature=True)
                except (ValueError, RuntimeError, OSError) as exc:
                    temporary.unlink(missing_ok=True)
                    rejected_assets.append({"filename": filename, "reason": str(exc)[:300]})
                    continue
                os.replace(temporary, target)
                platform, package_type = classification
                artifact = self.database.add_artifact({
                    "id": str(uuid.uuid4()), "app_id": app["id"], "filename": filename,
                    "platform": platform, "package_type": package_type, "version": version,
                    "size_bytes": size, "sha256": digest, "upstream_digest_verified": upstream_verified,
                    "validation_status": "validated", "signature_status": str(verification["signature_status"]),
                    "signer_certificate_sha256": verification.get("signer_certificate_sha256"),
                    "source_url": download_url, "storage_path": str(target), "created_at": utc_now(),
                })
                imported.append(artifact)
        return {"app": app, "artifacts": imported, "rejected_assets": rejected_assets, "release_found": bool(release)}


class BuildService:
    ALLOWED_TARGETS = {"android", "windows", "linux", "macos"}

    def __init__(self, catalog: CatalogService | None = None, github: GitHubClient | None = None, database: Database = db):
        self.github = github or GitHubClient()
        self.catalog = catalog or CatalogService(self.github, database)
        self.database = database

    def dispatch(self, repository_url: str, git_ref: str, targets: list[str]) -> dict[str, Any]:
        canonical = canonical_repository_url(repository_url)
        if not valid_git_ref(git_ref):
            raise ValueError("Invalid Git ref.")
        normalized_targets = sorted(set(targets))
        if not normalized_targets or not set(normalized_targets).issubset(self.ALLOWED_TARGETS):
            raise ValueError("Choose one or more supported build targets.")
        imported = self.catalog.import_repository(canonical, mirror_release_assets=False)
        app = imported["app"]
        if not settings.github_owner or not settings.github_repository or not settings.github_token:
            raise RuntimeError("Configure APPHORYN_GITHUB_OWNER, APPHORYN_GITHUB_REPOSITORY and APPHORYN_GITHUB_TOKEN (Actions + Contents write) to enable remote builds.")
        job_id = str(uuid.uuid4())
        timestamp = utc_now()
        job = {
            "id": job_id, "app_id": app["id"], "repository_url": canonical,
            "git_ref": git_ref, "targets": normalized_targets, "status": "queued",
            "message": "Build workflow dispatch requested; build code runs on isolated native GitHub runners.", "created_at": timestamp, "updated_at": timestamp,
        }
        self.database.add_job(job)
        try:
            mirror = GitHubReleaseMirror(settings.github_token, f"{settings.github_owner}/{settings.github_repository}", settings.github_ref)
            mirror.publish_app(app, [])
            mirror.dispatch_build(canonical, git_ref, normalized_targets, app.get("slug") or app["id"])
        except Exception as exc:
            self.database.update_job(job_id, "dispatch_failed", str(exc))
            raise
        return job


def store_uploaded_artifact(app_id: str, filename: str, platform: str, version: str,
                            source_url: str, git_ref: str, data_path: Path, expected_sha256: str,
                            database: Database = db) -> dict[str, Any]:
    app = database.get_app(app_id)
    if not app:
        raise ValueError("Unknown Apphoryn application id.")
    if canonical_repository_url(source_url) != app["repository_url"]:
        raise ValueError("Artifact source repository does not match catalog record.")
    filename = safe_filename(filename)
    classification = classify_artifact(filename)
    if not classification or classification[0] != platform:
        raise ValueError("Artifact extension does not match declared platform.")
    size = data_path.stat().st_size
    if size <= 0 or size > settings.max_artifact_bytes:
        raise ValueError("Artifact is empty or exceeds configured maximum size.")
    digest = sha256_file(str(data_path))
    if digest.lower() != expected_sha256.lower():
        raise ValueError("Uploaded artifact SHA-256 does not match the supplied digest.")
    try:
        verification = validate_artifact(data_path, require_apk_signature=True)
    except ArtifactValidationError as exc:
        raise ValueError(f"Artifact validation failed: {exc}") from exc
    existing = next((item for item in database.list_artifacts(app_id) if item.get("sha256") == digest), None)
    if existing:
        return existing
    destination_dir = settings.storage_path / app_id
    destination_dir.mkdir(parents=True, exist_ok=True)
    artifact_id = str(uuid.uuid4())
    target = destination_dir / f"{artifact_id}-{filename}"
    shutil.move(str(data_path), str(target))
    result = database.add_artifact({
        "id": artifact_id, "app_id": app_id, "filename": filename,
        "platform": platform, "package_type": classification[1], "version": version,
        "size_bytes": size, "sha256": digest, "upstream_digest_verified": False,
        "validation_status": "validated", "signature_status": str(verification["signature_status"]),
        "signer_certificate_sha256": verification.get("signer_certificate_sha256"),
        "source_url": f"{app['repository_url']}@{git_ref}", "storage_path": str(target), "created_at": utc_now(),
    })
    return result
