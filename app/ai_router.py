from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

_TIMEOUT = 35
_CACHE_SECONDS = max(60, int(os.getenv("APPHORYN_AI_MODELS_CACHE_SECONDS", "900")))
_FREE_ONLY = os.getenv("APPHORYN_AI_FREE_ONLY", "true").lower() not in {"0", "false", "no", "off"}
_ALLOW_CREDIT = os.getenv("APPHORYN_AI_ALLOW_CREDIT_BASED_PROVIDERS", "false").lower() in {"1", "true", "yes", "on"}
_ENABLE_CF = os.getenv("APPHORYN_AI_ENABLE_CLOUDFLARE", "false").lower() in {"1", "true", "yes", "on"}

@dataclass(frozen=True)
class Candidate:
    provider: str
    model: str
    kind: str
    base: str
    key: str
    context: int = 0
    free_class: str = "provider_free_tier"

    def public(self) -> dict[str, Any]:
        return {"provider": self.provider, "model": self.model, "context_length": self.context,
                "free_class": self.free_class, "api_type": self.kind}

class ProviderError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status

_cache: dict[str, Any] = {"until": 0.0, "models": [], "providers": []}
_cooldowns: dict[str, float] = {}

def _request(url: str, headers: dict[str, str] | None = None,
             body: dict[str, Any] | None = None) -> Any:
    h = {"Accept": "application/json", "User-Agent": "Apphoryn-AI-Router/1.0"}
    h.update(headers or {})
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    if data is not None:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as response:
            return json.loads(response.read(8 * 1024 * 1024).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(1200).decode("utf-8", errors="replace")
        except Exception:
            pass
        raise ProviderError(f"HTTP {exc.code}: {detail[:350]}", exc.code) from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise ProviderError(f"Request failed ({type(exc).__name__}).") from exc

def _status(provider: str, configured: bool, enabled: bool, note: str, count: int = 0) -> dict[str, Any]:
    return {"provider": provider, "configured": configured, "enabled": enabled,
            "models_discovered": count, "note": note}

def _priority() -> list[str]:
    return [x.strip().lower() for x in os.getenv(
        "APPHORYN_AI_PROVIDER_PRIORITY", "gemini,groq,openrouter,cloudflare,comet").split(",") if x.strip()]

def discover_models(force: bool = False) -> tuple[list[Candidate], list[dict[str, Any]]]:
    global _cache
    now = time.time()
    if not force and now < _cache["until"]:
        return list(_cache["models"]), list(_cache["providers"])
    models: list[Candidate] = []
    providers: list[dict[str, Any]] = []

    # Gemini and Groq list models but do not expose an enforceable account billing tier.
    # In free-only mode, require an explicit owner confirmation before sending billable API traffic.
    confirm_provider_free_tier = os.getenv(
        "APPHORYN_AI_CONFIRM_GEMINI_GROQ_FREE_TIER", "false"
    ).lower() in {"1", "true", "yes", "on"}

    key = os.getenv("GEMINI_API_KEY", "").strip()
    if key and _FREE_ONLY and not confirm_provider_free_tier:
        providers.append(_status(
            "gemini", True, False,
            "Configured but paused: the API cannot verify the project's billing tier. Set "
            "APPHORYN_AI_CONFIRM_GEMINI_GROQ_FREE_TIER=true only after confirming a free-tier project."
        ))
    elif key:
        try:
            data = _request("https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000",
                            {"x-goog-api-key": key})
            for item in data.get("models", []):
                methods = item.get("supportedGenerationMethods", [])
                model = str(item.get("name") or "").removeprefix("models/")
                if model and (not methods or "generateContent" in methods):
                    models.append(Candidate(
                        "gemini", model, "gemini",
                        "https://generativelanguage.googleapis.com/v1beta", key,
                        int(item.get("inputTokenLimit") or 0),
                        "provider_free_tier_confirmed" if _FREE_ONLY else "account_billing_plan"
                    ))
            providers.append(_status("gemini", True, True,
                "Model list loaded. The provider account must remain on its free tier; quota is not exposed by this endpoint.",
                sum(m.provider == "gemini" for m in models)))
        except ProviderError as exc:
            providers.append(_status("gemini", True, True, str(exc)))
    else:
        providers.append(_status("gemini", False, False, "GEMINI_API_KEY is not configured."))

    key = os.getenv("GROQ_API_KEY", "").strip()
    if key and _FREE_ONLY and not confirm_provider_free_tier:
        providers.append(_status(
            "groq", True, False,
            "Configured but paused: the API cannot verify the account's free billing tier. "
            "Confirm the free plan before enabling this provider."
        ))
    elif key:
        try:
            data = _request("https://api.groq.com/openai/v1/models",
                            {"Authorization": f"Bearer {key}"})
            for item in data.get("data", []):
                model = str(item.get("id") or "")
                if model:
                    models.append(Candidate(
                        "groq", model, "openai", "https://api.groq.com/openai/v1", key,
                        int(item.get("context_window") or 0),
                        "provider_free_tier_confirmed" if _FREE_ONLY else "account_billing_plan"
                    ))
            providers.append(_status("groq", True, True,
                "Model list loaded. The provider account must remain on its free tier; quota is not exposed by this endpoint.",
                sum(m.provider == "groq" for m in models)))
        except ProviderError as exc:
            providers.append(_status("groq", True, True, str(exc)))
    else:
        providers.append(_status("groq", False, False, "GROQ_API_KEY is not configured."))

    # OpenRouter free-only: only explicit :free variants plus its free auto-router.
    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if key:
        try:
            data = _request("https://openrouter.ai/api/v1/models", {"Authorization": f"Bearer {key}"})
            for item in data.get("data", []):
                model = str(item.get("id") or "")
                if model and (not _FREE_ONLY or model.endswith(":free")):
                    models.append(Candidate("openrouter", model, "openai",
                        "https://openrouter.ai/api/v1", key, int(item.get("context_length") or 0),
                        "free_variant" if model.endswith(":free") else "paid_or_zero_price"))
            if _FREE_ONLY:
                models.append(Candidate("openrouter", "openrouter/free", "openai",
                    "https://openrouter.ai/api/v1", key, 0, "free_router"))
            providers.append(_status("openrouter", True, True,
                "Free-only: :free models and openrouter/free only." if _FREE_ONLY else "All account-visible models enabled.",
                sum(m.provider == "openrouter" for m in models)))
        except ProviderError as exc:
            providers.append(_status("openrouter", True, True, str(exc)))
    else:
        providers.append(_status("openrouter", False, False, "OPENROUTER_API_KEY is not configured."))

    # CometAPI exposes its catalog at /api/models. Free-only mode admits only
    # entries whose catalog pricing is explicitly zero, or an owner allowlist
    # combined with the explicit credit-spend opt-in.
    key = os.getenv("COMET_API_KEY", "").strip()
    allowlist = {x.strip() for x in os.getenv("APPHORYN_COMET_FREE_MODEL_IDS", "").split(",") if x.strip()}

    def comet_is_zero_priced(item: dict[str, Any]) -> bool:
        if item.get("free") is True or item.get("is_free") is True:
            return True
        pricing = item.get("pricing") or item.get("price") or {}
        if not isinstance(pricing, dict) or not pricing:
            return False
        price_values = [
            pricing.get("prompt"), pricing.get("completion"),
            pricing.get("input"), pricing.get("output"),
            pricing.get("input_price"), pricing.get("output_price"),
        ]
        present = [value for value in price_values if value is not None]
        if not present:
            return False
        def zero(value: Any) -> bool:
            try:
                return float(str(value).replace("$", "").strip()) == 0.0
            except (TypeError, ValueError):
                return False
        return all(zero(value) for value in present)

    if key:
        try:
            data = _request("https://api.cometapi.com/api/models", {"Authorization": f"Bearer {key}"})
            rows = data if isinstance(data, list) else (
                data.get("data") or data.get("models") or data.get("items") or data.get("result") or []
            )
            discovered = 0
            for item in rows if isinstance(rows, list) else []:
                if not isinstance(item, dict):
                    continue
                model = str(item.get("id") or item.get("model") or item.get("name") or "").strip()
                if not model:
                    continue
                explicitly_allowed = model in allowlist and _ALLOW_CREDIT
                if _FREE_ONLY and not (comet_is_zero_priced(item) or explicitly_allowed):
                    continue
                context = int(item.get("context_length") or item.get("context_window") or 0)
                models.append(Candidate(
                    "comet", model, "openai", "https://api.cometapi.com/v1", key, context,
                    "zero_priced_catalog" if comet_is_zero_priced(item) else "explicit_credit_allowlist"
                ))
                discovered += 1
            note = (
                "Only catalog-confirmed zero-priced models are enabled."
                if _FREE_ONLY and discovered else
                "No verified zero-priced Comet models were returned; credit-based models remain disabled."
                if _FREE_ONLY else
                "Comet catalog loaded; account billing applies."
            )
            providers.append(_status("comet", True, discovered > 0, note, discovered))
        except ProviderError as exc:
            providers.append(_status("comet", True, False, str(exc)))
    else:
        providers.append(_status("comet", False, False, "COMET_API_KEY is not configured."))

    # Cloudflare requires an account ID as well as a token, and explicit opt-in.
    key = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
    account = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
    if key and account and _ENABLE_CF:
        try:
            query = urllib.parse.urlencode({"task": "text-generation", "hide_experimental": "true", "per_page": 100})
            url = f"https://api.cloudflare.com/client/v4/accounts/{urllib.parse.quote(account)}/ai/models/search?{query}"
            data = _request(url, {"Authorization": f"Bearer {key}"})
            for item in data.get("result", []):
                model = str(item.get("name") or item.get("id") or "")
                if model:
                    models.append(Candidate("cloudflare", model, "cloudflare",
                        "https://api.cloudflare.com/client/v4", key, int(item.get("context_length") or 0),
                        "account_quota_not_verified"))
            providers.append(_status("cloudflare", True, True,
                "Enabled by explicit opt-in. Cloudflare account quota/billing must be monitored.",
                sum(m.provider == "cloudflare" for m in models)))
        except ProviderError as exc:
            providers.append(_status("cloudflare", True, True, str(exc)))
    else:
        note = "CLOUDFLARE_ACCOUNT_ID is required." if key and not account else (
            "Disabled until APPHORYN_AI_ENABLE_CLOUDFLARE=true; billing cannot be inferred safely." if key
            else "CLOUDFLARE_API_TOKEN is not configured.")
        providers.append(_status("cloudflare", bool(key), False, note))

    priority = _priority()
    models.sort(key=lambda m: (priority.index(m.provider) if m.provider in priority else len(priority),
                               -m.context, m.model))
    _cache = {"until": now + _CACHE_SECONDS, "models": models, "providers": providers}
    return list(models), list(providers)

def has_configured_ai() -> bool:
    return any(os.getenv(k, "").strip() for k in (
        "GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY", "COMET_API_KEY", "CLOUDFLARE_API_TOKEN"))

def _gemini(candidate: Candidate, messages: list[dict[str, str]]) -> dict[str, Any]:
    system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system")
    user = "\n\n".join(m["content"] for m in messages if m.get("role") != "system")
    body: dict[str, Any] = {"contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"}}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    url = f"{candidate.base}/models/{urllib.parse.quote(candidate.model, safe='')}:generateContent"
    data = _request(url, {"x-goog-api-key": candidate.key}, body)
    parts = data["candidates"][0]["content"]["parts"]
    return json.loads("".join(str(p.get("text") or "") for p in parts))

def _openai(candidate: Candidate, messages: list[dict[str, str]]) -> dict[str, Any]:
    body = {"model": candidate.model, "messages": messages, "temperature": 0.2,
            "response_format": {"type": "json_object"}}
    headers = {"Authorization": f"Bearer {candidate.key}"}
    if candidate.provider == "openrouter":
        headers.update({"HTTP-Referer": os.getenv("APPHORYN_PUBLIC_BASE_URL", "https://apphoryn.example"),
                        "X-Title": "Apphoryn"})
    data = _request(candidate.base.rstrip("/") + "/chat/completions", headers, body)
    content = data["choices"][0]["message"]["content"]
    if isinstance(content, list):
        content = "".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
    return json.loads(content)

def _cloudflare(candidate: Candidate, messages: list[dict[str, str]]) -> dict[str, Any]:
    account = urllib.parse.quote(os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip())
    model = urllib.parse.quote(candidate.model, safe="@/.-_")
    url = f"{candidate.base}/accounts/{account}/ai/run/{model}"
    data = _request(url, {"Authorization": f"Bearer {candidate.key}"}, {"messages": messages})
    result = data.get("result", {})
    content = result.get("response") or result.get("output")
    if not content and result.get("choices"):
        content = result["choices"][0].get("message", {}).get("content")
    if not content:
        raise ProviderError("Cloudflare returned no text content.")
    return json.loads(content) if isinstance(content, str) else content

def generate_json(messages: list[dict[str, str]]) -> tuple[dict[str, Any], dict[str, str]]:
    candidates, _ = discover_models()
    errors: list[str] = []
    attempted = 0
    for candidate in candidates:
        key = f"{candidate.provider}:{candidate.model}"
        if _cooldowns.get(key, 0) > time.time():
            continue
        attempted += 1
        try:
            if candidate.kind == "gemini":
                result = _gemini(candidate, messages)
            elif candidate.kind == "cloudflare":
                result = _cloudflare(candidate, messages)
            else:
                result = _openai(candidate, messages)
            if not isinstance(result, dict):
                raise ProviderError("Model response was not a JSON object.")
            return result, {"provider": candidate.provider, "model": candidate.model}
        except (ProviderError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            status = exc.status if isinstance(exc, ProviderError) else None
            cooldown = 180 if status == 429 else 900 if status in {401, 403} else 45 if status and status >= 500 else 60
            _cooldowns[key] = time.time() + cooldown
            errors.append(f"{candidate.provider}/{candidate.model}: {str(exc)[:150]}")
    if candidates and not attempted:
        raise ProviderError("All discovered models are cooling down; retry after their cooldown windows.")
    raise ProviderError("No model completed the request. " + " | ".join(errors[:8]))

def public_model_catalog(force: bool = False) -> dict[str, Any]:
    models, providers = discover_models(force)
    return {"free_only": _FREE_ONLY, "models": [m.public() for m in models], "providers": providers,
            "warning": "Model APIs do not reveal every account's billing tier or remaining free quota. Keep keys on free plans; Comet and Cloudflare are disabled by default in free-only mode."}
