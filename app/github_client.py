from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .config import settings

API = "https://api.github.com"


class GitHubError(RuntimeError):
    pass


class GitHubClient:
    def __init__(self, token: str | None = None):
        self.token = token if token is not None else settings.github_token

    def request(self, path: str, params: dict[str, str] | None = None) -> Any:
        if path.startswith("http://") or path.startswith("https://"):
            url = path
        else:
            url = API + path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Apphoryn-Open-Source-Store/0.1",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read(1024).decode("utf-8", errors="replace")
            raise GitHubError(f"GitHub API returned HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise GitHubError(f"GitHub request failed: {exc}") from exc

    def search_repositories(self, query: str, page: int = 1, per_page: int = 20) -> dict[str, Any]:
        return self.request("/search/repositories", {"q": query, "sort": "updated", "order": "desc", "page": str(page), "per_page": str(min(max(per_page, 1), 50))})

    def list_public_repositories(self, since: int = 0, per_page: int = 100) -> list[dict[str, Any]]:
        """Return the next public repositories with IDs greater than the saved cursor."""
        response = self.request("/repositories", {
            "since": str(max(0, int(since))),
            "per_page": str(min(max(int(per_page), 1), 100)),
        })
        if not isinstance(response, list):
            raise GitHubError("GitHub public repository listing returned an unexpected response.")
        return response

    def get_repository(self, owner: str, repo: str) -> dict[str, Any]:
        return self.request(f"/repos/{owner}/{repo}")

    def get_latest_release(self, owner: str, repo: str) -> dict[str, Any] | None:
        try:
            return self.request(f"/repos/{owner}/{repo}/releases/latest")
        except GitHubError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise

    def mutate(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        """Perform an authenticated GitHub API mutation on the configured origin only."""
        if not self.token:
            raise GitHubError("APPHORYN_GITHUB_TOKEN is required for repository mutations.")
        if not path.startswith("/repos/") or path.startswith("//") or "://" in path:
            raise GitHubError("Only GitHub repository API paths are permitted.")
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "Apphoryn-Open-Source-Store/1.0",
            "Content-Type": "application/json",
        }
        request = urllib.request.Request(API + path, data=data, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read(2 * 1024 * 1024)
                if not raw:
                    return {"status": response.status}
                try:
                    return json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    return {"status": response.status}
        except urllib.error.HTTPError as exc:
            detail = exc.read(1600).decode("utf-8", errors="replace")
            raise GitHubError(f"GitHub API mutation returned HTTP {exc.code}: {detail[:1000]}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise GitHubError(f"GitHub API mutation failed: {type(exc).__name__}") from exc

    def dispatch_workflow(self, owner: str, repo: str, workflow: str, ref: str, inputs: dict[str, str]) -> dict[str, Any]:
        if not self.token:
            raise GitHubError("APPHORYN_GITHUB_TOKEN is required to dispatch build workflows.")
        path = f"/repos/{owner}/{repo}/actions/workflows/{workflow}/dispatches"
        body = json.dumps({"ref": ref, "inputs": inputs}).encode("utf-8")
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "Apphoryn-Open-Source-Store/0.1",
            "Content-Type": "application/json",
        }
        request = urllib.request.Request(API + path, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.status not in (200, 201, 204):
                    raise GitHubError(f"Workflow dispatch failed with HTTP {response.status}.")
                raw = response.read(1024 * 1024)
                if not raw:
                    return {"status": response.status}
                try:
                    return json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    return {"status": response.status}
        except urllib.error.HTTPError as exc:
            detail = exc.read(1024).decode("utf-8", errors="replace")
            raise GitHubError(f"Workflow dispatch failed with HTTP {exc.code}: {detail}") from exc
