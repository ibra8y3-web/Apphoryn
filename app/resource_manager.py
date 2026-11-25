from __future__ import annotations

import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .config import settings


_REPOSITORY_URL = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")


def _probe_json(url: str, token: str = "", timeout: float = 3.0) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "127.0.0.1", "localhost", "ollama", "vllm", "host.docker.internal"
    }:
        raise ValueError("Local model endpoint must use a loopback or explicitly supported Compose host.")
    headers = {"Accept": "application/json", "User-Agent": "Apphoryn-ResourceManager/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = __import__("json").loads(response.read(1024 * 1024).decode("utf-8"))
    return payload if isinstance(payload, dict) else {}


def resource_status() -> dict[str, Any]:
    from .agent_system import STORE
    from .analytics import analytics

    workers = STORE.worker_rows()
    active = sum(1 for row in workers if row.get("status") == "running")
    local = {
        "id": "local_backend",
        "type": "self_hosted",
        "configured": True,
        "available": True,
        "active_workers": active,
        "workers": len(workers),
        "description": "Workers execute on the machine/container running Apphoryn. They can continue local work while the internet is unavailable if local models and data are available.",
        "limits": "Bounded by the host CPU/RAM/disk; not an external free VPS.",
    }

    github_enabled = os.getenv("APPHORYN_GITHUB_COMPUTE_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
    github_configured = bool(settings.github_token and settings.github_owner and settings.github_repository)
    github = {
        "id": "github_actions",
        "type": "ephemeral_cloud_runner",
        "configured": github_configured,
        "enabled": github_enabled,
        "available": bool(github_configured and github_enabled),
        "repository": f"{settings.github_owner}/{settings.github_repository}" if settings.github_owner and settings.github_repository else None,
        "description": "Starts a fresh GitHub-hosted runner for approved source analysis. A public repository's standard runners are free; private-repository usage is subject to the account's included quota and billing.",
        "limits": "Ephemeral job only; not an always-on server, and it cannot work while the backend cannot reach GitHub.",
    }

    ollama_url = os.getenv("OLLAMA_BASE_URL", "").strip().rstrip("/")
    ollama_token = os.getenv("OLLAMA_API_KEY", "").strip()
    ollama: dict[str, Any] = {
        "id": "ollama",
        "type": "local_open_source_model_server",
        "configured": bool(ollama_url),
        "available": False,
        "models": [],
        "description": "Runs open models on a computer you control. Models must be downloaded before an offline period.",
        "limits": "Uses the host's CPU/GPU/RAM; downloading models requires connectivity and disk space.",
    }
    if ollama_url:
        try:
            endpoint = ollama_url
            if endpoint.endswith("/v1"):
                endpoint = endpoint[:-3]
            data = _probe_json(endpoint + "/api/tags", token=ollama_token)
            rows = data.get("models") or []
            ollama["available"] = True
            ollama["models"] = [
                str(row.get("name") or row.get("model"))
                for row in rows[:60] if isinstance(row, dict) and (row.get("name") or row.get("model"))
            ]
        except Exception as exc:
            ollama["error"] = f"{type(exc).__name__}: {str(exc)[:180]}"

    vllm_url = os.getenv("VLLM_BASE_URL", "").strip().rstrip("/")
    vllm_token = os.getenv("VLLM_API_KEY", "").strip()
    vllm: dict[str, Any] = {
        "id": "vllm",
        "type": "local_open_source_model_server",
        "configured": bool(vllm_url),
        "available": False,
        "models": [],
        "description": "Optional vLLM OpenAI-compatible server for GPU-equipped self-hosted machines.",
        "limits": "Requires suitable GPU/VRAM for the selected model; vLLM API must be protected behind the private network or a trusted proxy.",
    }
    if vllm_url:
        try:
            data = _probe_json(vllm_url + "/models", token=vllm_token)
            rows = data.get("data") or []
            vllm["available"] = True
            vllm["models"] = [
                str(row.get("id")) for row in rows[:60]
                if isinstance(row, dict) and row.get("id")
            ]
        except Exception as exc:
            vllm["error"] = f"{type(exc).__name__}: {str(exc)[:180]}"

    database = analytics.backend_status()
    return {
        "compute": [local, github],
        "model_servers": [ollama, vllm],
        "databases": [
            database,
            {
                "id": "neon_supabase_postgres",
                "type": "managed_postgres",
                "configured": database.get("provider") == "postgres",
                "available": None,
                "description": "Neon and Supabase connection strings are PostgreSQL-compatible. Actual connectivity is confirmed only by a successful analytics replication.",
                "limits": "Free-plan quotas and inactivity pauses are provider controlled; do not treat a free plan as an uptime guarantee.",
            },
            {
                "id": "cloudflare_d1",
                "type": "managed_edge_sql",
                "configured": database.get("provider") == "cloudflare_d1",
                "available": None,
                "description": "Cloudflare D1 can receive replicated analytics when account, database and API token credentials are configured.",
                "limits": "Account free-tier storage and daily query limits apply.",
            },
        ],
        "offline_mode": {
            "local_queue": True,
            "local_analytics": True,
            "local_workers": True,
            "requires_local_host": True,
            "requires_cached_model": True,
            "remote_services_available_without_internet": False,
        },
    }


def dispatch_security_scan(repository_url: str, git_ref: str = "HEAD",
                           approval: bool = False) -> dict[str, Any]:
    if not approval:
        raise PermissionError("Explicit approval is required before dispatching a GitHub-hosted compute job.")
    if os.getenv("APPHORYN_GITHUB_COMPUTE_ENABLED", "false").lower() not in {"1", "true", "yes", "on"}:
        raise RuntimeError("GitHub Actions compute is disabled. Set APPHORYN_GITHUB_COMPUTE_ENABLED=true to enable it.")
    if not (settings.github_token and settings.github_owner and settings.github_repository):
        raise RuntimeError("Configure Apphoryn's GitHub token, owner and repository before using hosted compute.")
    match = _REPOSITORY_URL.fullmatch(repository_url.strip())
    if not match:
        raise ValueError("Only a canonical https://github.com/OWNER/REPOSITORY URL is accepted.")
    owner, repo = match.group(1), match.group(2).removesuffix(".git")
    if (not re.fullmatch(r"[A-Za-z0-9._/-]{1,180}", git_ref)
            or any(part in {"", ".", ".."} for part in git_ref.split("/"))):
        raise ValueError("Invalid Git ref.")
    from .github_client import GitHubClient
    client = GitHubClient(settings.github_token)
    metadata = client.get_repository(owner, repo)
    if metadata.get("private"):
        raise PermissionError("Hosted source scanning accepts public repositories only. Do not send private source to a hosted runner.")
    if metadata.get("archived"):
        raise ValueError("Archived repositories are not dispatched automatically.")
    target = f"{owner}/{repo}"
    client.dispatch_workflow(
        settings.github_owner, settings.github_repository, "agent-compute.yml", settings.github_ref,
        {"repository": target, "git_ref": git_ref},
    )
    return {
        "provider": "github_actions",
        "status": "dispatched",
        "target_repository": target,
        "git_ref": git_ref,
        "workflow": "agent-compute.yml",
        "workflow_repository": f"{settings.github_owner}/{settings.github_repository}",
        "runner_type": "standard-github-hosted",
        "cost_note": "Standard runners are free for public repositories. Private repository usage may consume included minutes or incur charges.",
    }
