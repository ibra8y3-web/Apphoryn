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

    def search_repositories(self, query: str, page: int = 1, per_page: int = 20, sort: str = "updated", order: str = "desc") -> dict[str, Any]:
        if sort not in {"updated", "created", "stars", "forks"} or order not in {"asc", "desc"}:
            raise ValueError("Unsupported GitHub repository search ordering.")
        return self.request("/search/repositories", {
            "q": query, "sort": sort, "order": order,
            "page": str(max(1, page)), "per_page": str(min(max(per_page, 1), 100)),
        })

    def get_repository(self, owner: str, repo: str) -> dict[str, Any]:
        return self.request(f"/repos/{owner}/{repo}")

    def get_latest_release(self, owner: str, repo: str) -> dict[str, Any] | None:
        try:
            return self.request(f"/repos/{owner}/{repo}/releases/latest")
        except GitHubError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise

    def dispatch_workflow(self, owner: str, repo: str, workflow: str, ref: str, inputs: dict[str, str]) -> None:
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
                if response.status not in (201, 204):
                    raise GitHubError(f"Workflow dispatch failed with HTTP {response.status}.")
        except urllib.error.HTTPError as exc:
            detail = exc.read(1024).decode("utf-8", errors="replace")
            raise GitHubError(f"Workflow dispatch failed with HTTP {exc.code}: {detail}") from exc
