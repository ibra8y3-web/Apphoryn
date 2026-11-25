from __future__ import annotations

import base64
import hashlib
import http.client
import json
import re
import ssl
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .artifact_validation import ArtifactValidationError, validate_artifact
from .domain import classify_artifact, canonical_repository_url, license_allows_redistribution, safe_filename, valid_git_ref

API = "https://api.github.com"


class GitHubMirrorError(RuntimeError):
    pass


def _safe_release_component(value: str) -> str:
    return (re.sub(r"[^A-Za-z0-9._-]+", "-", str(value)).strip("-._") or "unknown")[:80]


def architecture_from_filename(filename: str) -> str | None:
    name = filename.lower()
    for label, fragments in (
        ("arm64", ("arm64", "aarch64", "arm-64", "arm64v8")),
        ("arm32", ("arm32", "armv7", "arm-32", "arm32v7")),
        ("x86_64", ("x86_64", "x64", "amd64")),
        ("x86", ("i386", "i686", "x86")),
        ("universal", ("universal", "all-arch")),
    ):
        if any(fragment in name for fragment in fragments):
            return label
    return None


class GitHubReleaseMirror:
    """Stores validated binaries in Apphoryn-owned GitHub Releases and publishes a static manifest."""

    def __init__(self, token: str, repository_full_name: str, branch: str = "main"):
        if not token:
            raise GitHubMirrorError("A GitHub token with Contents: write permission is required.")
        parts = repository_full_name.split("/", 1)
        if len(parts) != 2 or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts):
            raise GitHubMirrorError("GITHUB_REPOSITORY must have owner/repository form.")
        self.owner, self.repo = parts
        self.full_name = repository_full_name
        self.token = token
        self.branch = branch
        self.catalog_path = "apps/web/catalog/index.json"

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None, *, allow_404: bool = False) -> Any:
        url = path if path.startswith("https://") else API + path
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=body, method=method, headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Apphoryn-Release-Mirror/1.0",
            **({"Content-Type": "application/json"} if body is not None else {}),
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                raw = response.read()
                return json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read(4096).decode("utf-8", errors="replace")
            if allow_404 and exc.code == 404:
                return None
            raise GitHubMirrorError(f"GitHub API {method} {path} failed (HTTP {exc.code}): {detail}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise GitHubMirrorError(f"GitHub API request failed: {exc}") from exc

    def _ensure_release(self, tag: str, name: str, body: str) -> dict[str, Any]:
        existing = self.request("GET", f"/repos/{self.full_name}/releases/tags/{urllib.parse.quote(tag, safe='')}", allow_404=True)
        if existing:
            return existing
        return self.request("POST", f"/repos/{self.full_name}/releases", {
            "tag_name": tag, "target_commitish": self.branch, "name": name[:120],
            "body": body[:12000], "draft": False, "prerelease": False,
        })

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _hash_remote_asset(url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        allowed = {"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com", "github-releases.githubusercontent.com"}
        if parsed.scheme != "https" or parsed.hostname not in allowed:
            raise GitHubMirrorError("Existing release asset has an untrusted download URL; it will not be reused.")
        request = urllib.request.Request(url, headers={"User-Agent": "Apphoryn-Release-Mirror/1.0"})
        digest = hashlib.sha256()
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                final = urllib.parse.urlparse(response.geturl())
                if final.scheme != "https" or final.hostname not in allowed:
                    raise GitHubMirrorError("Existing release asset redirected to an untrusted host.")
                for chunk in iter(lambda: response.read(1024 * 1024), b""):
                    digest.update(chunk)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise GitHubMirrorError(f"Could not verify the existing Apphoryn release asset: {exc}") from exc
        return digest.hexdigest()

    def _upload_asset(self, release: dict[str, Any], path: Path, asset_name: str) -> dict[str, Any]:
        digest = self._file_sha256(path)
        assets = self.request("GET", f"/repos/{self.full_name}/releases/{release['id']}/assets")
        by_name = {item.get("name"): item for item in assets}
        prior = by_name.get(asset_name)
        if prior:
            prior_digest = str(prior.get("digest") or "").removeprefix("sha256:").lower()
            if prior_digest == digest:
                return prior
            if not prior_digest and prior.get("browser_download_url"):
                if self._hash_remote_asset(str(prior["browser_download_url"])) == digest:
                    return prior
            asset_name = f"{path.stem}-{digest[:12]}{path.suffix}"
            prior = by_name.get(asset_name)
            if prior:
                prior_digest = str(prior.get("digest") or "").removeprefix("sha256:").lower()
                if prior_digest == digest:
                    return prior
                if not prior_digest and prior.get("browser_download_url"):
                    if self._hash_remote_asset(str(prior["browser_download_url"])) == digest:
                        return prior
                raise GitHubMirrorError(f"An asset named {asset_name} already exists and could not be verified as identical.")

        template = str(release.get("upload_url") or "")
        if not template.startswith("https://uploads.github.com/"):
            raise GitHubMirrorError("GitHub release did not provide an approved uploads.github.com URL.")
        upload_url = template.split("{", 1)[0] + "?name=" + urllib.parse.quote(asset_name)
        parsed = urllib.parse.urlparse(upload_url)
        if parsed.scheme != "https" or parsed.hostname != "uploads.github.com":
            raise GitHubMirrorError("Unexpected release asset upload host.")
        connection = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=240, context=ssl.create_default_context())
        try:
            connection.putrequest("POST", parsed.path + "?" + parsed.query)
            for key, value in {
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/octet-stream",
                "Content-Length": str(path.stat().st_size),
            }.items():
                connection.putheader(key, value)
            connection.endheaders()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    connection.send(chunk)
            response = connection.getresponse()
            raw = response.read(8192).decode("utf-8", errors="replace")
            if response.status not in (200, 201):
                raise GitHubMirrorError(f"Release asset upload failed (HTTP {response.status}): {raw[:1000]}")
            uploaded = json.loads(raw)
            remote_digest = str(uploaded.get("digest") or "").removeprefix("sha256:").lower()
            if remote_digest and remote_digest != digest:
                raise GitHubMirrorError(f"GitHub-reported digest mismatch for {asset_name}.")
            return uploaded
        finally:
            connection.close()

    def _read_catalog(self) -> tuple[dict[str, Any], str | None]:
        result = self.request("GET", f"/repos/{self.full_name}/contents/{self.catalog_path}?ref={urllib.parse.quote(self.branch)}", allow_404=True)
        if not result:
            return {"schema_version": 1, "generated_at": None, "apps": []}, None
        content = result.get("content")
        if not content and str(result.get("size", "")) == "0":
            # GitHub returns zero-byte Contents files with no base64 payload.
            # Treat this as an empty first-run catalog, but retain the SHA so
            # the first publisher updates this file instead of racing to create it.
            return {"schema_version": 1, "generated_at": None, "apps": []}, result.get("sha")
        if result.get("encoding") != "base64" or not content:
            # GitHub's Contents API omits inline Base64 for files at or above
            # its 1 MB inline-content boundary. The blob SHA remains available,
            # so retrieve that exact immutable Git object instead of failing
            # every import once the Apphoryn catalog grows beyond the limit.
            blob_sha = str(result.get("sha") or "").strip()
            if not re.fullmatch(r"[0-9a-fA-F]{40}", blob_sha):
                raise GitHubMirrorError(
                    "Apphoryn catalog has no inline content and no valid Git blob SHA."
                )
            blob = self.request(
                "GET", f"/repos/{self.full_name}/git/blobs/{blob_sha}"
            )
            content = blob.get("content") if isinstance(blob, dict) else None
            if not content or blob.get("encoding") != "base64":
                raise GitHubMirrorError(
                    "GitHub did not return Base64 content for the Apphoryn catalog blob."
                )
        raw = base64.b64decode(content.replace("\n", ""))
        try:
            manifest = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubMirrorError("Existing Apphoryn catalog manifest is invalid JSON.") from exc
        if not isinstance(manifest, dict) or not isinstance(manifest.get("apps"), list):
            raise GitHubMirrorError("Existing Apphoryn catalog manifest has an unsupported schema.")
        return manifest, result.get("sha")

    def _write_catalog(self, manifest: dict[str, Any], sha: str | None, message: str) -> None:
        payload = {
            "message": message,
            "content": base64.b64encode((json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()).decode("ascii"),
            "branch": self.branch,
        }
        if sha:
            payload["sha"] = sha
        self.request("PUT", f"/repos/{self.full_name}/contents/{self.catalog_path}", payload)

    def publish_ai_status(self, status: dict[str, Any]) -> None:
        """Publish a sanitized model-provider health snapshot; never persist credentials."""
        manifest, sha = self._read_catalog()
        safe_providers = []
        for item in status.get("providers", []):
            if not isinstance(item, dict):
                continue
            provider = str(item.get("provider") or "").strip().lower()
            if not provider:
                continue
            state = str(item.get("state") or "unknown")
            if state not in {"available", "paused", "not_configured", "unavailable"}:
                state = "unknown"
            models = item.get("models") if isinstance(item.get("models"), list) else []
            safe_models = []
            for model in models[:20]:
                if not isinstance(model, dict):
                    continue
                model_name = str(model.get("model") or "").strip()[:180]
                if not model_name or "\r" in model_name or "\n" in model_name:
                    continue
                safe_models.append({
                    "model": model_name,
                    "free_class": str(model.get("free_class") or "")[:80],
                })
            safe_providers.append({
                "provider": provider,
                "state": state,
                "models_discovered": max(0, int(item.get("models_discovered") or len(safe_models))),
                "models": safe_models,
            })
        manifest["ai_status"] = {
            "checked_at": str(status.get("checked_at") or datetime.now(timezone.utc).isoformat()),
            "free_only": bool(status.get("free_only", True)),
            "available_provider_count": sum(item["state"] == "available" for item in safe_providers),
            "available_model_count": max(0, int(status.get("available_model_count") or 0)),
            "providers": safe_providers,
        }
        manifest["generated_at"] = datetime.now(timezone.utc).isoformat()
        self._write_catalog(manifest, sha, "Update Apphoryn AI provider health status")

    def _upload_notice(self, release: dict[str, Any], app: dict[str, Any], slug: str) -> str | None:
        license_text = str(app.get("license_text") or "")
        if not license_text.strip():
            return None
        notice_text = (
            f"Apphoryn third-party software notice\nProject: {app.get('name') or slug}\n"
            f"Source: {app.get('repository_url', '')}\nSPDX license: {app.get('license_spdx') or 'unknown'}\n\n"
            f"Original upstream license text follows:\n\n{license_text}\n"
        )
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as notice:
            notice.write(notice_text)
            notice_path = Path(notice.name)
        try:
            uploaded = self._upload_asset(release, notice_path, f"THIRD-PARTY-NOTICE-{slug}.txt")
            return uploaded.get("browser_download_url")
        finally:
            notice_path.unlink(missing_ok=True)

    def publish_app(self, app: dict[str, Any], artifacts: list[dict[str, Any]]) -> dict[str, Any]:
        """Publish only verified installer binaries; source-only projects create no store Release."""
        if not app.get("license_eligible") or not license_allows_redistribution(app.get("license_spdx")):
            raise GitHubMirrorError("Refusing to mirror a project without an approved SPDX redistribution license.")

        if not artifacts:
            # Register source metadata even before a binary exists. The public
            # storefront filters source-only rows, while the isolated build
            # workflow needs this stable record before it can publish outputs.
            manifest, sha = self._read_catalog()
            apps = manifest.setdefault("apps", [])
            repository_url = str(app.get("repository_url") or "")
            repository_id = str(app.get("github_repo_id") or "") or None
            current = next((
                item for item in apps
                if item.get("repository_url") == repository_url
                or (repository_id and str(item.get("github_repo_id") or "") == repository_id)
            ), None)
            slug = _safe_release_component(str(app.get("slug") or app.get("name") or "app"))
            if current is None:
                current = {"id": str(app.get("slug") or slug), "slug": str(app.get("slug") or slug), "artifacts": []}
                apps.append(current)
            current.update({
                "name": app.get("name") or slug,
                "summary": app.get("summary") or current.get("summary") or "",
                "description": app.get("description") or current.get("description") or "",
                "translations": app.get("translations") or current.get("translations") or {},
                "description_translations": app.get("description_translations") or current.get("description_translations") or {},
                "translation_status": app.get("translation_status") or current.get("translation_status") or "missing",
                "ai_provider": app.get("ai_provider") or current.get("ai_provider"),
                "ai_model": app.get("ai_model") or current.get("ai_model"),
                "icon_url": app.get("icon_url") or current.get("icon_url"),
                "repository_url": repository_url,
                "github_repo_id": repository_id or current.get("github_repo_id"),
                "source_updated_at": app.get("source_updated_at") or current.get("source_updated_at"),
                "homepage_url": app.get("homepage_url") or current.get("homepage_url"),
                "owner": app.get("owner") or current.get("owner") or "",
                "default_branch": app.get("default_branch") or "main",
                "source_commit_sha": app.get("source_commit_sha") or current.get("source_commit_sha"),
                "license_text": app.get("license_text") or current.get("license_text") or "",
                "license_spdx": app.get("license_spdx"),
                "license_eligible": bool(app.get("license_eligible")),
                "project_kind": app.get("project_kind") or "unknown",
                "build_recommended": bool(app.get("build_recommended", False)),
                "classification_source": app.get("classification_source") or "heuristic",
                "classification_reason": app.get("classification_reason") or "",
                "category": app.get("category") or "other",
                "topics": app.get("topics") or [],
                "latest_version": app.get("latest_version"),
                "release_url": app.get("release_url"),
                "artifacts": list(current.get("artifacts") or []),
            })
            current["platforms"] = sorted({
                str(item.get("platform")) for item in current["artifacts"] if item.get("platform")
            })
            current["project_status"] = "installable" if current["artifacts"] else "source_available"
            manifest["schema_version"] = 1
            manifest["generated_at"] = datetime.now(timezone.utc).isoformat()
            manifest["apps"] = sorted(apps, key=lambda item: str(item.get("name", "")).casefold())
            self._write_catalog(manifest, sha, f"Register Apphoryn source project: {app.get('name') or slug}")
            return {
                "release_url": current.get("mirror_release_url"),
                "manifest_path": self.catalog_path,
                "mirrored_artifacts": [],
                "source_project_registered": True,
                "project_status": current["project_status"],
            }

        slug = _safe_release_component(str(app.get("slug") or app.get("name") or "app"))
        version = _safe_release_component(str(app.get("latest_version") or "unknown"))
        manifest, sha = self._read_catalog()
        existing = next((
            item for item in manifest.get("apps", [])
            if item.get("repository_url") == str(app.get("repository_url") or "")
            or (
                app.get("github_repo_id")
                and str(item.get("github_repo_id") or "") == str(app.get("github_repo_id"))
            )
        ), None)
        known_hashes = {
            str(item.get("sha256") or "").lower()
            for item in (existing or {}).get("artifacts", [])
            if item.get("sha256")
        }

        # Preflight all files before making a Release. Source archives, invalid
        # packages and duplicate bytes must never result in notice-only Releases.
        prepared: list[tuple[dict[str, Any], Path, str, str, dict[str, Any]]] = []
        already_present = 0
        for artifact in artifacts:
            source_path = Path(str(artifact.get("storage_path") or ""))
            if source_path.is_symlink() or not source_path.is_file():
                raise GitHubMirrorError(f"Validated artifact file is missing or unsafe: {source_path}")
            filename = str(artifact.get("filename") or source_path.name)
            classification = classify_artifact(filename)
            if not classification:
                raise GitHubMirrorError(f"Artifact is not a supported installable package: {filename}")
            digest = self._file_sha256(source_path)
            expected = str(artifact.get("sha256") or "").lower()
            if not expected or digest != expected:
                raise GitHubMirrorError(f"Stored artifact SHA-256 changed before publishing: {source_path.name}")
            if digest in known_hashes:
                already_present += 1
                continue
            validation = validate_artifact(source_path, require_apk_signature=True)
            platform, package_type = classification
            prepared.append((artifact, source_path, platform, package_type, validation))
            known_hashes.add(digest)

        if not prepared:
            # Even when this release contains only already-mirrored bytes, refresh
            # the app's metadata (including corrected bilingual text and artwork).
            current = existing
            if current is not None:
                current.update({
                    "name": app.get("name") or current.get("name") or slug,
                    "summary": app.get("summary") or current.get("summary") or "",
                    "description": app.get("description") or current.get("description") or "",
                    "translations": app.get("translations") or current.get("translations") or {},
                    "description_translations": app.get("description_translations") or current.get("description_translations") or {},
                    "translation_status": app.get("translation_status") or current.get("translation_status") or "missing",
                    "ai_provider": app.get("ai_provider") or current.get("ai_provider"),
                    "ai_model": app.get("ai_model") or current.get("ai_model"),
                    "icon_url": app.get("icon_url") or current.get("icon_url"),
                    "source_updated_at": app.get("source_updated_at") or current.get("source_updated_at"),
                    "homepage_url": app.get("homepage_url") or current.get("homepage_url"),
                    "owner": app.get("owner") or current.get("owner") or "",
                    "default_branch": app.get("default_branch") or current.get("default_branch") or "main",
                    "source_commit_sha": app.get("source_commit_sha") or current.get("source_commit_sha"),
                    "license_text": app.get("license_text") or current.get("license_text") or "",
                    "license_spdx": app.get("license_spdx") or current.get("license_spdx"),
                    "license_eligible": bool(app.get("license_eligible")),
                    "project_kind": app.get("project_kind") or current.get("project_kind") or "unknown",
                    "build_recommended": bool(app.get("build_recommended", current.get("build_recommended", False))),
                    "classification_source": app.get("classification_source") or current.get("classification_source") or "heuristic",
                    "classification_reason": app.get("classification_reason") or current.get("classification_reason") or "",
                    "category": app.get("category") or current.get("category") or "other",
                    "topics": app.get("topics") or current.get("topics") or [],
                    "latest_version": app.get("latest_version") or current.get("latest_version"),
                    "release_url": app.get("release_url") or current.get("release_url"),
                })
                manifest["generated_at"] = datetime.now(timezone.utc).isoformat()
                manifest["apps"] = sorted(manifest.get("apps", []), key=lambda item: str(item.get("name", "")).casefold())
                self._write_catalog(manifest, sha, f"Refresh Apphoryn metadata: {app.get('name') or slug}")
            return {
                "release_url": (existing or {}).get("mirror_release_url"),
                "manifest_path": self.catalog_path,
                "mirrored_artifacts": [],
                "already_present": already_present,
                "source_project_registered": current is not None,
            }

        tag = f"apphoryn-{slug}-{version}"
        release = self._ensure_release(
            tag, f"{app.get('name') or slug} {app.get('latest_version') or version} — Apphoryn mirror",
            "Apphoryn-owned mirror of validated package files.\\n\\n"
            f"Upstream project: {app.get('repository_url', '')}\\nLicense: {app.get('license_spdx') or 'unknown'}\\n\\n"
            "Format and signature checks are not malware or safety certification.",
        )
        mirrored = []
        for artifact, source_path, platform, package_type, validation in prepared:
            digest = self._file_sha256(source_path)
            uploaded = self._upload_asset(release, source_path, str(artifact.get("filename") or source_path.name))
            name = uploaded.get("name") or source_path.name
            mirrored.append({
                "id": digest,
                "filename": name,
                "original_filename": str(artifact.get("filename") or source_path.name),
                "platform": platform,
                "package_type": package_type,
                "architecture": architecture_from_filename(str(artifact.get("filename") or source_path.name)),
                "version": artifact.get("version") or app.get("latest_version"),
                "size_bytes": int(artifact.get("size_bytes") or source_path.stat().st_size),
                "sha256": digest,
                "upstream_digest_verified": bool(artifact.get("upstream_digest_verified")),
                "validation_status": "validated",
                "signature_status": artifact.get("signature_status") or validation.get("signature_status", "not_checked"),
                "signer_certificate_sha256": artifact.get("signer_certificate_sha256") or validation.get("signer_certificate_sha256"),
                "download_url": uploaded.get("browser_download_url") or f"https://github.com/{self.full_name}/releases/download/{tag}/{urllib.parse.quote(name)}",
                "mirror_release_url": release.get("html_url"),
            })

        # Upload legal notices only after at least one actual package asset exists.
        license_notice_url = self._upload_notice(release, app, slug)
        manifest, sha = self._read_catalog()
        apps = manifest.setdefault("apps", [])
        repository_url = str(app.get("repository_url") or "")
        repository_id = str(app.get("github_repo_id") or "") or None
        current = next((
            item for item in apps
            if item.get("repository_url") == repository_url
            or (repository_id and str(item.get("github_repo_id") or "") == repository_id)
        ), None)
        if current is None:
            current = {"id": str(app.get("slug") or slug), "slug": str(app.get("slug") or slug)}
            apps.append(current)
        current.update({
            "name": app.get("name") or slug,
            "summary": app.get("summary") or "",
            "description": app.get("description") or "",
            "translations": app.get("translations") or current.get("translations") or {},
            "description_translations": app.get("description_translations") or current.get("description_translations") or {},
            "translation_status": app.get("translation_status") or current.get("translation_status") or "missing",
            "ai_provider": app.get("ai_provider") or current.get("ai_provider"),
            "ai_model": app.get("ai_model") or current.get("ai_model"),
            "icon_url": app.get("icon_url") or current.get("icon_url"),
            "repository_url": repository_url,
            "github_repo_id": repository_id or current.get("github_repo_id"),
            "source_updated_at": app.get("source_updated_at") or current.get("source_updated_at"),
            "homepage_url": app.get("homepage_url") or current.get("homepage_url"),
            "owner": app.get("owner") or "",
            "default_branch": app.get("default_branch") or "main",
            "source_commit_sha": app.get("source_commit_sha") or current.get("source_commit_sha"),
            "license_text": app.get("license_text") or current.get("license_text") or "",
            "license_spdx": app.get("license_spdx"),
            "license_eligible": bool(app.get("license_eligible")),
            "project_kind": app.get("project_kind") or "unknown",
            "build_recommended": bool(app.get("build_recommended", False)),
            "classification_source": app.get("classification_source") or "heuristic",
            "classification_reason": app.get("classification_reason") or "",
            "project_status": "installable",
            "category": app.get("category") or "other",
            "topics": app.get("topics") or [],
            "latest_version": app.get("latest_version"),
            "release_url": app.get("release_url"),
            "mirror_release_url": release.get("html_url"),
            "license_notice_url": license_notice_url or current.get("license_notice_url"),
        })
        all_artifacts = current.setdefault("artifacts", [])
        known = {str(item.get("sha256") or "").lower() for item in all_artifacts}
        for item in mirrored:
            if item["sha256"].lower() not in known:
                all_artifacts.append(item)
                known.add(item["sha256"].lower())
        current["platforms"] = sorted({
            str(item.get("platform")) for item in all_artifacts if item.get("platform")
        })
        current["project_status"] = "installable" if all_artifacts else "source_available"
        manifest["schema_version"] = 1
        manifest["generated_at"] = datetime.now(timezone.utc).isoformat()
        manifest["apps"] = sorted(apps, key=lambda item: str(item.get("name", "")).casefold())
        self._write_catalog(manifest, sha, f"Update Apphoryn catalog: {app.get('name') or slug} {version}")
        return {
            "release_url": release.get("html_url"),
            "manifest_path": self.catalog_path,
            "mirrored_artifacts": mirrored,
            "already_present": already_present,
        }

    def publish_built_artifacts(self, repository_url: str, git_ref: str, directory: str | Path) -> dict[str, Any]:
        canonical = canonical_repository_url(repository_url)
        root = Path(directory).resolve(strict=True)
        manifest, sha = self._read_catalog()
        app = next((item for item in manifest.get("apps", []) if item.get("repository_url") == canonical), None)
        if app is None or not app.get("license_eligible") or not license_allows_redistribution(app.get("license_spdx")):
            raise GitHubMirrorError("Build result has no pre-existing catalog record with an approved SPDX license.")
        if not valid_git_ref(git_ref):
            raise GitHubMirrorError("Invalid Git ref supplied for build publication.")
        candidates = []
        for path in root.rglob("*"):
            if path.is_symlink() or not path.is_file() or not classify_artifact(path.name):
                continue
            resolved = path.resolve(strict=True)
            if root not in resolved.parents:
                continue
            if resolved.stat().st_size <= 0 or resolved.stat().st_size > 1024 * 1024 * 1024:
                raise GitHubMirrorError(f"Build output is empty or exceeds the 1 GiB publication cap: {resolved.name}")
            candidates.append(resolved)
        if not candidates:
            raise GitHubMirrorError("No recognized build outputs were supplied for publication.")

        slug = _safe_release_component(str(app.get("slug") or app.get("name") or "app"))
        tag = f"apphoryn-{slug}-build-{_safe_release_component(git_ref)}"
        release = self._ensure_release(
            tag, f"{app.get('name') or slug} build {git_ref} — Apphoryn",
            f"Built by Apphoryn on disposable native runners.\nUpstream: {canonical}\nRef: {git_ref}\nLicense: {app.get('license_spdx')}\n"
            "Format checks are not a malware scan or safety certification.",
        )
        license_notice_url = self._upload_notice(release, app, slug)
        published = []
        known = {str(item.get("sha256")) for item in app.setdefault("artifacts", [])}
        for path in candidates:
            validation = validate_artifact(path, require_apk_signature=True)
            platform, package_type = classify_artifact(path.name) or ("", "")
            digest = self._file_sha256(path)
            if digest in known:
                continue
            target_name = safe_filename(f"{platform}-{path.name}")
            uploaded = self._upload_asset(release, path, target_name)
            name = uploaded.get("name") or target_name
            row = {
                "id": digest, "filename": name, "original_filename": path.name,
                "platform": platform, "package_type": package_type,
                "architecture": architecture_from_filename(path.name), "version": git_ref,
                "size_bytes": path.stat().st_size, "sha256": digest,
                "upstream_digest_verified": False, "validation_status": "validated",
                "signature_status": validation.get("signature_status", "not_checked"),
                "signer_certificate_sha256": validation.get("signer_certificate_sha256"),
                "download_url": uploaded.get("browser_download_url") or f"https://github.com/{self.full_name}/releases/download/{tag}/{urllib.parse.quote(name)}",
                "mirror_release_url": release.get("html_url"), "build_ref": git_ref,
            }
            app["artifacts"].append(row)
            known.add(digest)
            published.append(row)
        if not published:
            raise GitHubMirrorError("All build outputs were already present or produced no new package files.")
        app["platforms"] = sorted({str(item.get("platform")) for item in app["artifacts"] if item.get("platform")})
        if license_notice_url:
            app["license_notice_url"] = license_notice_url
        app.setdefault("build_refs", [])
        if git_ref not in app["build_refs"]:
            app["build_refs"].append(git_ref)
        manifest["generated_at"] = datetime.now(timezone.utc).isoformat()
        self._write_catalog(manifest, sha, f"Publish built packages for {app.get('name') or slug} ({git_ref})")
        return {"release_url": release.get("html_url"), "published": published}

    def get_available_platforms(self, repository_url: str) -> set[str]:
        canonical = canonical_repository_url(repository_url)
        manifest, _ = self._read_catalog()
        app = next((item for item in manifest.get("apps", []) if item.get("repository_url") == canonical), None)
        if not app:
            return set()
        return {str(item.get("platform")) for item in app.get("artifacts", []) if item.get("platform")}

    def has_recent_build_attempt(
        self, repository_url: str, git_ref: str, max_age_hours: int = 72,
        targets: list[str] | None = None, adapter_revision: str | None = None,
    ) -> bool:
        canonical = canonical_repository_url(repository_url)
        manifest, _ = self._read_catalog()
        app = next((item for item in manifest.get("apps", []) if item.get("repository_url") == canonical), None)
        if not app:
            return False
        requested = set(targets or [])
        available = {str(item.get("platform")) for item in app.get("artifacts", []) if item.get("platform")}
        if requested and requested.issubset(available):
            return True
        if not requested and git_ref in (app.get("build_refs") or []):
            return True
        now = datetime.now(timezone.utc)
        for attempt in app.get("build_attempts", []):
            if attempt.get("git_ref") != git_ref:
                continue
            if adapter_revision and attempt.get("adapter_revision") != adapter_revision:
                continue
            try:
                stamp = datetime.fromisoformat(str(attempt.get("requested_at")).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            if (now - stamp).total_seconds() >= max_age_hours * 3600:
                continue
            attempted = {str(value) for value in attempt.get("targets", [])}
            if not requested or requested.issubset(available | attempted):
                return True
        return False

    def dispatch_build(
        self, repository_url: str, git_ref: str, targets: list[str], app_id: str = "",
        adapter_revision: str | None = None,
    ) -> None:
        canonical = canonical_repository_url(repository_url)
        allowed = {"android", "windows", "linux", "macos"}
        normalized = sorted(set(targets))
        if not valid_git_ref(git_ref):
            raise GitHubMirrorError("Invalid Git ref supplied for build dispatch.")
        if not normalized or not set(normalized).issubset(allowed):
            raise GitHubMirrorError("Unsupported or empty build target list.")
        manifest, sha = self._read_catalog()
        app = next((item for item in manifest.get("apps", []) if item.get("repository_url") == canonical), None)
        if app is None or not app.get("license_eligible") or not license_allows_redistribution(app.get("license_spdx")):
            raise GitHubMirrorError("Build dispatch requires an existing catalog entry with an approved redistribution license.")
        attempts = app.setdefault("build_attempts", [])
        attempt = {"git_ref": git_ref, "targets": normalized, "requested_at": datetime.now(timezone.utc).isoformat()}
        if adapter_revision:
            attempt["adapter_revision"] = adapter_revision
        attempts.append(attempt)
        app["build_attempts"] = attempts[-50:]
        self._write_catalog(manifest, sha, f"Queue Apphoryn build for {app.get('name') or app_id} ({git_ref})")
        self.request("POST", f"/repos/{self.full_name}/actions/workflows/build-community-app.yml/dispatches", {
            "ref": self.branch,
            "inputs": {"repository_url": canonical, "git_ref": git_ref, "targets": ",".join(normalized), "app_id": app_id or "apphoryn-app"},
        })
