"""Verify short-lived GitHub Actions OIDC identities for trusted Apphoryn automation."""
from __future__ import annotations

from typing import Any

import jwt
from jwt import PyJWKClient


ISSUER = "https://token.actions.githubusercontent.com"
AUDIENCE = "apphoryn-catalog-automation"
ALLOWED_REPOSITORY = "ibra8y3-web/Apphoryn"
ALLOWED_WORKFLOWS = {"harvest.yml", "global-scout.yml"}
ALLOWED_EVENTS = {"push", "schedule", "workflow_dispatch"}
JWKS_URL = f"{ISSUER}/.well-known/jwks"
_jwks_client = PyJWKClient(JWKS_URL, cache_jwk_set=True, lifespan=3600)


class GitHubActionsOIDCError(ValueError):
    """Raised when a request is not from an approved Apphoryn Actions workflow."""


def validate_github_actions_claims(claims: dict[str, Any]) -> dict[str, Any]:
    if claims.get("repository") != ALLOWED_REPOSITORY:
        raise GitHubActionsOIDCError("Untrusted repository.")
    if claims.get("ref") != "refs/heads/main":
        raise GitHubActionsOIDCError("Only the protected main branch may call this endpoint.")
    if claims.get("event_name") not in ALLOWED_EVENTS:
        raise GitHubActionsOIDCError("This GitHub Actions event is not allowed.")
    allowed_refs = {
        f"{ALLOWED_REPOSITORY}/.github/workflows/{name}@refs/heads/main"
        for name in ALLOWED_WORKFLOWS
    }
    if str(claims.get("workflow_ref") or "") not in allowed_refs:
        raise GitHubActionsOIDCError("Untrusted GitHub Actions workflow.")
    if not str(claims.get("run_id") or "").isdigit():
        raise GitHubActionsOIDCError("Missing GitHub Actions run identity.")
    return claims


def verify_github_actions_oidc(token: str) -> dict[str, Any]:
    if not token or len(token) > 20000:
        raise GitHubActionsOIDCError("Missing or oversized GitHub Actions token.")
    try:
        signing_key = _jwks_client.get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            audience=AUDIENCE,
            issuer=ISSUER,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            leeway=30,
        )
    except (jwt.PyJWTError, ValueError, TypeError) as exc:
        raise GitHubActionsOIDCError("GitHub Actions identity verification failed.") from exc
    if not isinstance(claims, dict):
        raise GitHubActionsOIDCError("Invalid GitHub Actions token claims.")
    return validate_github_actions_claims(claims)
