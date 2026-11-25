"""Client identification and admin-token throttling that work behind a reverse proxy."""
from __future__ import annotations

import hmac
import os
import threading
import time

from fastapi import HTTPException, Request

_FAILED: dict[str, list[float]] = {}
_LOCK = threading.Lock()


def _max_failed() -> int:
    """Failed attempts allowed per client in the window. 0 (default) = unlimited, never locked out."""
    try:
        return max(0, int(os.getenv("APPHORYN_ADMIN_MAX_FAILED", "0") or 0))
    except ValueError:
        return 0


WINDOW_SECONDS = 600


def client_ip(request: Request | None) -> str:
    """Return the real client address.

    Directly exposed servers use the socket peer. Behind a reverse proxy set
    ``APPHORYN_TRUSTED_PROXY_HOPS`` to the number of trusted proxies in front of
    the app (1 for a single Nginx/Caddy/Render/Railway proxy); the address the
    outermost trusted proxy appended to ``X-Forwarded-For`` is used. Entries to
    its left are client-controlled and are ignored, so they cannot be spoofed.
    Set ``APPHORYN_TRUST_CLOUDFLARE=true`` to prefer ``CF-Connecting-IP``.
    """
    if request is None:
        return "unknown"
    peer = request.client.host if request.client else "unknown"
    try:
        hops = max(0, int(os.getenv("APPHORYN_TRUSTED_PROXY_HOPS", "0") or 0))
    except ValueError:
        hops = 0
    if hops <= 0:
        return peer
    if os.getenv("APPHORYN_TRUST_CLOUDFLARE", "").lower() in {"1", "true", "yes", "on"}:
        cf = (request.headers.get("cf-connecting-ip") or "").strip()
        if cf:
            return cf[:64]
    chain = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    if len(chain) >= hops:
        return chain[-hops][:64]
    return peer


def check_admin_token(token: str | None, expected: str, request: Request | None = None) -> None:
    """Constant-time token check with per-client brute-force throttling."""
    if not expected:
        raise HTTPException(503, "Admin actions are disabled until APPHORYN_ADMIN_TOKEN is configured.")
    key = client_ip(request)
    now = time.monotonic()
    with _LOCK:
        recent = [t for t in _FAILED.get(key, []) if now - t < WINDOW_SECONDS]
        _FAILED[key] = recent
        limit = _max_failed()
        if limit and len(recent) >= limit:
            raise HTTPException(429, "Too many failed attempts. Try again in a few minutes.")
        if len(_FAILED) > 5000:  # bound memory
            for k in [k for k, v in _FAILED.items() if not v or now - v[-1] > WINDOW_SECONDS]:
                _FAILED.pop(k, None)
    if not token or not hmac.compare_digest(token.encode(), expected.encode()):
        with _LOCK:
            _FAILED.setdefault(key, []).append(now)
        time.sleep(0.4)  # slows guessing without ever locking the owner out
        raise HTTPException(401, "A valid Apphoryn admin token is required.")
    with _LOCK:
        _FAILED.pop(key, None)
