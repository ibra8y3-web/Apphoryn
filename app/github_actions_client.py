"""Client for the Apphoryn Railway AI bridge, authenticated with GitHub Actions OIDC."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


AUDIENCE = "apphoryn-catalog-automation"
OIDC_HOST = "pipelines.actions.githubusercontent.com"
ALLOWED_SERVICE_HOSTS = {"apphoryn-production.up.railway.app"}


class GitHubActionsBridgeError(RuntimeError):
    """Raised when the ephemeral OIDC-to-Railway bridge cannot be used safely."""


def _trusted_oidc_request_host(hostname: str | None) -> bool:
    host = (hostname or "").rstrip(".").lower()
    # GitHub regional OIDC endpoints use names such as
    # pipelinesghubeus6.actions.githubusercontent.com.
    return host == "actions.githubusercontent.com" or host.endswith(".actions.githubusercontent.com")


def _request_actions_oidc_token() -> str:
    request_url = os.getenv("ACTIONS_ID_TOKEN_REQUEST_URL", "").strip()
    request_secret = os.getenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "").strip()
    if not request_url or not request_secret:
        raise GitHubActionsBridgeError("GitHub Actions OIDC permission is unavailable.")
    parsed = urllib.parse.urlsplit(request_url)
    if parsed.scheme != "https" or not _trusted_oidc_request_host(parsed.hostname) or parsed.username or parsed.password:
        raise GitHubActionsBridgeError("GitHub Actions returned an untrusted OIDC URL.")
    query = [(key, value) for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
             if key != "audience"]
    query.append(("audience", AUDIENCE))
    url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), ""))
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {request_secret}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.loads(response.read(128 * 1024).decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise GitHubActionsBridgeError("Could not obtain a short-lived GitHub Actions identity.") from exc
    token = str(result.get("value") or "")
    if not token:
        raise GitHubActionsBridgeError("GitHub Actions returned an empty identity token.")
    return token


def call_apphoryn_internal_api(
    path: str,
    *,
    method: str = "POST",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base_url = os.getenv("APPHORYN_AI_SERVICE_URL", "").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(base_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in ALLOWED_SERVICE_HOSTS
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise GitHubActionsBridgeError("The Apphoryn AI service URL is missing or not approved.")
    if not path.startswith("/api/v1/internal/github-actions/") or ".." in path.split("/"):
        raise GitHubActionsBridgeError("Only the Apphoryn GitHub Actions internal API may be called.")
    verb = method.upper()
    if verb not in {"GET", "POST"}:
        raise GitHubActionsBridgeError("Unsupported internal API method.")
    token = _request_actions_oidc_token()
    data = None if verb == "GET" else json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base_url + path, data=data, headers=headers, method=verb)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.loads(response.read(2 * 1024 * 1024).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read(1200).decode("utf-8", errors="replace")
        except Exception:
            detail = ""
        raise GitHubActionsBridgeError(f"Apphoryn AI bridge returned HTTP {exc.code}: {detail[:300]}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise GitHubActionsBridgeError("The Apphoryn AI service could not complete the request.") from exc
    if not isinstance(result, dict):
        raise GitHubActionsBridgeError("Apphoryn AI service returned an invalid response.")
    return result
