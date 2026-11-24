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
        if result.get("encoding") != "base64" or not result.get("content"):
            raise GitHubMirrorError("Existing Apphoryn catalog manifest is not a base64 file response.")
        raw = base64.b64decode(result["content"].replace("\n", ""))
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
        if not app.get("license_eligible") or not license_allows_redistribution(app.get("license_spdx")):
            raise GitHubMirrorError("Refusing to mirror a project without an approved SPDX redistribution license.")
        slug = _safe_release_component(str(app.get("slug") or app.get("name") or "app"))
        version = _safe_release_component(str(app.get("latest_version") or "source"))
        tag = f"apphoryn-{slug}-{version}"
        release = self._ensure_release(
            tag, f"{app.get('name') or slug} {app.get('latest_version') or 'source'} — Apphoryn mirror",
            "Apphoryn-owned mirror of validated package files.\n\n"
            f"Upstream project: {app.get('repository_url', '')}\nLicense: {app.get('license_spdx') or 'unknown'}\n\n"
            "Format/hash checks are not a malware or safety certification.",
        )
        license_notice_url = self._upload_notice(release, app, slug)
        mirrored = []
        for artifact in artifacts:
            source_path = Path(str(artifact.get("storage_path") or ""))
            if not source_path.is_file() or source_path.is_symlink():
                raise GitHubMirrorError(f"Validated artifact file is missing: {source_path}")
            digest = self._file_sha256(source_path)
            if digest != str(artifact.get("sha256") or "").lower():
                raise GitHubMirrorError(f"Stored artifact SHA-256 changed before publishing: {source_path.name}")
            validation = validate_artifact(source_path, require_apk_signature=True)
            uploaded = self._upload_asset(release, source_path, str(artifact["filename"]))
            name = uploaded.get("name") or source_path.name
            mirrored.append({
                "id": digest, "filename": name, "original_filename": str(artifact["filename"]),
                "platform": artifact.get("platform"), "package_type": artifact.get("package_type"),
                "architecture": architecture_from_filename(str(artifact["filename"])),
                "version": artifact.get("version"), "size_bytes": int(artifact.get("size_bytes") or source_path.stat().st_size),
                "sha256": digest, "upstream_digest_verified": bool(artifact.get("upstream_digest_verified")),
                "validation_status": "validated", "signature_status": artifact.get("signature_status") or validation.get("signature_status"),
                "signer_certificate_sha256": artifact.get("signer_certificate_sha256") or validation.get("signer_certificate_sha256"),
                "download_url": uploaded.get("browser_download_url") or f"https://github.com/{self.full_name}/releases/download/{tag}/{urllib.parse.quote(name)}",
                "mirror_release_url": release.get("html_url"),
            })

        manifest, sha = self._read_catalog()
        apps = manifest.setdefault("apps", [])
        repo_url = str(app.get("repository_url") or "")
        repo_id = str(app.get("github_repo_id") or "") or None
        current = next((item for item in apps if item.get("repository_url") == repo_url or (repo_id and str(item.get("github_repo_id") or "") == repo_id)), None)
        if current is None:
            current = {"id": str(app.get("slug") or slug), "slug": str(app.get("slug") or slug)}
            apps.append(current)
        current.update({
            "name": app.get("name") or slug, "summary": app.get("summary") or "",
            "description": app.get("description") or "", "translations": app.get("translations") or {},
            "repository_url": repo_url, "github_repo_id": repo_id or current.get("github_repo_id"),
            "source_updated_at": app.get("source_updated_at") or current.get("source_updated_at"),
            "homepage_url": app.get("homepage_url"), "owner": app.get("owner") or "",
            "default_branch": app.get("default_branch") or "main",
            "source_commit_sha": app.get("source_commit_sha") or current.get("source_commit_sha"),
            "license_text": app.get("license_text") or current.get("license_text") or "",
            "license_spdx": app.get("license_spdx"), "license_eligible": bool(app.get("license_eligible")),
            "category": app.get("category") or "other", "topics": app.get("topics") or [],
            "latest_version": app.get("latest_version"), "release_url": app.get("release_url"),
            "mirror_release_url": release.get("html_url"),
            "license_notice_url": license_notice_url or current.get("license_notice_url"),
            "scout_checked_at": datetime.now(timezone.utc).isoformat(),
        })
        all_artifacts = current.setdefault("artifacts", [])
        known = {str(item.get("sha256")) for item in all_artifacts}
        for item in mirrored:
            if item["sha256"] not in known:
                all_artifacts.append(item)
                known.add(item["sha256"])
        current["platforms"] = sorted({str(item.get("platform")) for item in all_artifacts if item.get("platform")})
        manifest["schema_version"] = 1
        manifest["generated_at"] = datetime.now(timezone.utc).isoformat()
        manifest["apps"] = sorted(apps, key=lambda item: str(item.get("name", "")).casefold())
        self._write_catalog(manifest, sha, f"Update Apphoryn catalog: {app.get('name') or slug} {version}")
        return {
            "release_url": release.get("html_url"),
            "manifest_path": self.catalog_path,
            "mirrored_artifacts": mirrored,
            "scout_checked_at": current.get("scout_checked_at"),
        }

    def mark_app_checked(self, repository_url: str, github_repo_id: str | None = None) -> bool:
        manifest, sha = self._read_catalog()
        current = next((
            item for item in manifest.get("apps", [])
            if item.get("repository_url") == repository_url
            or (github_repo_id and str(item.get("github_repo_id") or "") == str(github_repo_id))
        ), None)
        if current is None:
            return False
        current["scout_checked_at"] = datetime.now(timezone.utc).isoformat()
        manifest["generated_at"] = current["scout_checked_at"]
        self._write_catalog(manifest, sha, f"Scout checked upstream releases for {current.get('name') or repository_url}")
        return True

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

    def has_recent_build_attempt(self, repository_url: str, git_ref: str, max_age_hours: int = 72) -> bool:
        canonical = canonical_repository_url(repository_url)
        manifest, _ = self._read_catalog()
        app = next((item for item in manifest.get("apps", []) if item.get("repository_url") == canonical), None)
        if not app:
            return False
        if git_ref in (app.get("build_refs") or []):
            return True
        now = datetime.now(timezone.utc)
        for attempt in app.get("build_attempts", []):
            if attempt.get("git_ref") != git_ref:
                continue
            try:
                stamp = datetime.fromisoformat(str(attempt.get("requested_at")).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            if (now - stamp).total_seconds() < max_age_hours * 3600:
                return True
        return False

    def dispatch_build(self, repository_url: str, git_ref: str, targets: list[str], app_id: str = "") -> None:
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
        attempts.append({"git_ref": git_ref, "targets": normalized, "requested_at": datetime.now(timezone.utc).isoformat()})
        app["build_attempts"] = attempts[-50:]
        self._write_catalog(manifest, sha, f"Queue Apphoryn build for {app.get('name') or app_id} ({git_ref})")
        self.request("POST", f"/repos/{self.full_name}/actions/workflows/build-community-app.yml/dispatches", {
            "ref": self.branch,
            "inputs": {"repository_url": canonical, "git_ref": git_ref, "targets": ",".join(normalized), "app_id": app_id or "apphoryn-app"},
        })
