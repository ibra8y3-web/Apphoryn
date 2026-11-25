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
_ALLOW_PAID = os.getenv("APPHORYN_AI_ALLOW_PAID_REQUESTS", "false").lower() in {"1", "true", "yes", "on"}
_ENABLE_XAI = os.getenv("APPHORYN_AI_ENABLE_XAI", "false").lower() in {"1", "true", "yes", "on"}
_PREFER_LOCAL = os.getenv("APPHORYN_AI_PREFER_LOCAL", "true").lower() in {"1", "true", "yes", "on"}

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
        "APPHORYN_AI_PROVIDER_PRIORITY", "ollama,vllm,gemini,groq,xai,openrouter,cloudflare,comet").split(",") if x.strip()]

def _local_base_url(value: str) -> str:
    parsed = urllib.parse.urlparse(value.strip())
    if (parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "127.0.0.1", "localhost", "::1", "ollama", "vllm", "host.docker.internal"
    } or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ProviderError("Local model endpoint must be loopback or a named private Docker Compose service.")
    return value.rstrip("/")


def discover_models(force: bool = False) -> tuple[list[Candidate], list[dict[str, Any]]]:
    global _cache
    now = time.time()
    if not force and now < _cache["until"]:
        return list(_cache["models"]), list(_cache["providers"])
    models: list[Candidate] = []
    providers: list[dict[str, Any]] = []


    # Self-hosted open-weight model servers. These endpoints are restricted to loopback
    # or Compose service names so a secret-bearing backend cannot be turned into an SSRF proxy.
    ollama_value = os.getenv("OLLAMA_BASE_URL", "").strip()
    if ollama_value:
        try:
            base = _local_base_url(ollama_value)
            root = base[:-3] if base.endswith("/v1") else base
            key = os.getenv("OLLAMA_API_KEY", "").strip()
            data = _request(root + "/api/tags", {"Authorization": f"Bearer {key}"} if key else {})
            rows = data.get("models") or []
            for item in rows if isinstance(rows, list) else []:
                if not isinstance(item, dict):
                    continue
                model = str(item.get("name") or item.get("model") or "").strip()
                if model:
                    models.append(Candidate("ollama", model, "openai", root + "/v1", key,
                                            int((item.get("details") or {}).get("parameter_size_bytes") or 0),
                                            "local_open_source"))
            providers.append(_status("ollama", True, bool(rows), "Local Ollama model tags loaded; no provider API billing is involved.",
                                     sum(m.provider == "ollama" for m in models)))
        except ProviderError as exc:
            providers.append(_status("ollama", True, False, str(exc)))
    else:
        providers.append(_status("ollama", False, False, "OLLAMA_BASE_URL is not configured."))

    vllm_value = os.getenv("VLLM_BASE_URL", "").strip()
    if vllm_value:
        try:
            base = _local_base_url(vllm_value)
            if not base.endswith("/v1"):
                base += "/v1"
            key = os.getenv("VLLM_API_KEY", "").strip()
            data = _request(base + "/models", {"Authorization": f"Bearer {key}"} if key else {})
            rows = data.get("data") or []
            for item in rows if isinstance(rows, list) else []:
                if not isinstance(item, dict):
                    continue
                model = str(item.get("id") or "").strip()
                if model:
                    models.append(Candidate("vllm", model, "openai", base, key,
                                            int(item.get("context_length") or 0), "local_open_source"))
            providers.append(_status("vllm", True, bool(rows), "Local vLLM model catalog loaded; protected by the configured private endpoint.",
                                     sum(m.provider == "vllm" for m in models)))
        except ProviderError as exc:
            providers.append(_status("vllm", True, False, str(exc)))
    else:
        providers.append(_status("vllm", False, False, "VLLM_BASE_URL is not configured."))

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
    elif key and not _FREE_ONLY and not _ALLOW_PAID:
        providers.append(_status("gemini", True, False,
            "Paused by spending protection: paid requests are disabled. Set APPHORYN_AI_ALLOW_PAID_REQUESTS=true only after approving billing."))
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
    elif key and not _FREE_ONLY and not _ALLOW_PAID:
        providers.append(_status("groq", True, False,
            "Paused by spending protection: paid requests are disabled. Set APPHORYN_AI_ALLOW_PAID_REQUESTS=true only after approving billing."))
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


    # xAI's Grok API is a separate, usage-billed provider (not Groq).
    key = os.getenv("XAI_API_KEY", "").strip()
    if key and _ENABLE_XAI and _ALLOW_PAID and not _FREE_ONLY:
        try:
            data = _request("https://api.x.ai/v1/models", {"Authorization": f"Bearer {key}"})
            for item in data.get("data", []):
                model = str(item.get("id") or "")
                if model:
                    models.append(Candidate("xai", model, "openai", "https://api.x.ai/v1", key,
                                            int(item.get("context_length") or 0), "paid_api_explicit_opt_in"))
            providers.append(_status("xai", True, True,
                "xAI model catalog loaded. API calls are usage-billed; paid access was explicitly enabled.",
                sum(m.provider == "xai" for m in models)))
        except ProviderError as exc:
            providers.append(_status("xai", True, False, str(exc)))
    elif key:
        providers.append(_status("xai", True, False,
            "Grok API is usage-billed and is blocked by default. Enable APPHORYN_AI_ENABLE_XAI=true, "
            "APPHORYN_AI_ALLOW_PAID_REQUESTS=true and APPHORYN_AI_FREE_ONLY=false only after approving paid usage."))
    else:
        providers.append(_status("xai", False, False, "XAI_API_KEY is not configured."))

    # OpenRouter free-only: only explicit :free variants plus its free auto-router.
    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if key:
        try:
            data = _request("https://openrouter.ai/api/v1/models", {"Authorization": f"Bearer {key}"})
            for item in data.get("data", []):
                model = str(item.get("id") or "")
                if model and ((not _FREE_ONLY and _ALLOW_PAID) or model.endswith(":free")):
                    models.append(Candidate("openrouter", model, "openai",
                        "https://openrouter.ai/api/v1", key, int(item.get("context_length") or 0),
                        "free_variant" if model.endswith(":free") else "paid_or_zero_price"))
            if _FREE_ONLY or not _ALLOW_PAID:
                models.append(Candidate("openrouter", "openrouter/free", "openai",
                    "https://openrouter.ai/api/v1", key, 0, "free_router"))
            providers.append(_status("openrouter", True, True,
                "Free-only: :free models and openrouter/free only." if (_FREE_ONLY or not _ALLOW_PAID) else "All account-visible models enabled.",
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
                explicitly_allowed = model in allowlist and _ALLOW_CREDIT and _ALLOW_PAID
                if (_FREE_ONLY or not _ALLOW_PAID) and not (comet_is_zero_priced(item) or explicitly_allowed):
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
    if key and account and _ENABLE_CF and _ALLOW_PAID and not _FREE_ONLY:
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
            "Disabled by spending protection; Cloudflare billing may apply. Explicit paid authorization is required."
            if key and _ENABLE_CF and not _ALLOW_PAID else
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
        "GEMINI_API_KEY", "GROQ_API_KEY", "XAI_API_KEY", "OPENROUTER_API_KEY", "COMET_API_KEY", "CLOUDFLARE_API_TOKEN"))

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
    headers = {"Authorization": f"Bearer {candidate.key}"} if candidate.key else {}
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

def _task_capability_score(candidate: Candidate, task_type: str | None) -> float:
    task = (task_type or "").lower()
    model = candidate.model.casefold()
    score = 0.0
    if task in {"software_engineer", "debugging", "build", "security"}:
        if any(word in model for word in ("coder", "code", "devstral", "codestral", "deepseek-coder")):
            score += 0.35
    if task in {"research", "discovery", "update", "niebex"}:
        if candidate.context >= 64000:
            score += 0.20
        elif candidate.context >= 32000:
            score += 0.10
        if any(word in model for word in ("reason", "r1", "thinking", "research")):
            score += 0.15
    if task in {"writing", "research"} and any(word in model for word in ("qwen", "gemini", "aya", "llama", "mistral")):
        score += 0.08
    if _PREFER_LOCAL and candidate.provider in {"ollama", "vllm"}:
        score += 0.18
    return score


def _rank_candidates(candidates: list[Candidate], task_type: str | None) -> list[Candidate]:
    try:
        from .agent_system import STORE
        history = {(row["provider"], row["model"]): row for row in STORE.model_metrics()}
        evaluations = {}
        for row in STORE.model_evaluation_metrics():
            if row["task_type"] == "general":
                evaluations[(row["provider"], row["model"])] = row
    except Exception:
        history, evaluations = {}, {}

    priorities = _priority()
    def rank(candidate: Candidate):
        metrics = history.get((candidate.provider, candidate.model), {})
        requests = int(metrics.get("requests") or 0)
        success_rate = (float(metrics.get("successes") or 0) / requests) if requests else 0.0
        latency = float(metrics.get("average_latency_ms") or 0)
        latency_score = max(0.0, 1.0 - min(latency, 10000.0) / 10000.0) if latency else 0.0
        evaluation = evaluations.get((candidate.provider, candidate.model), {})
        quality = float(evaluation.get("average_score") or 0)
        evidence_score = (0.30 * success_rate + 0.25 * quality + 0.10 * latency_score)
        priority = priorities.index(candidate.provider) if candidate.provider in priorities else len(priorities)
        return (-(_task_capability_score(candidate, task_type) + evidence_score), priority, -candidate.context)
    return sorted(candidates, key=rank)


def generate_json(messages: list[dict[str, str]], task_type: str | None = None) -> tuple[dict[str, Any], dict[str, str]]:
    candidates, _ = discover_models()
    candidates = _rank_candidates(candidates, task_type)
    errors: list[str] = []
    attempted = 0
    for candidate in candidates:
        key = f"{candidate.provider}:{candidate.model}"
        if _cooldowns.get(key, 0) > time.time():
            continue
        attempted += 1
        started = time.monotonic()
        try:
            if candidate.kind == "gemini":
                result = _gemini(candidate, messages)
            elif candidate.kind == "cloudflare":
                result = _cloudflare(candidate, messages)
            else:
                result = _openai(candidate, messages)
            if not isinstance(result, dict):
                raise ProviderError("Model response was not a JSON object.")
            _record_provider_event(candidate.provider, candidate.model, True, int((time.monotonic() - started) * 1000))
            return result, {"provider": candidate.provider, "model": candidate.model}
        except (ProviderError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            status = exc.status if isinstance(exc, ProviderError) else None
            cooldown = 180 if status == 429 else 900 if status in {401, 403} else 45 if status and status >= 500 else 60
            _cooldowns[key] = time.time() + cooldown
            _record_provider_event(candidate.provider, candidate.model, False,
                                   int((time.monotonic() - started) * 1000), status, str(exc))
            errors.append(f"{candidate.provider}/{candidate.model}: {str(exc)[:150]}")
    if candidates and not attempted:
        raise ProviderError("All discovered models are cooling down; retry after their cooldown windows.")
    raise ProviderError("No model completed the request. " + " | ".join(errors[:8]))

def _record_provider_event(provider: str, model: str | None, success: bool, latency_ms: int | None = None,
                           http_status: int | None = None, error: str | None = None) -> None:
    try:
        from .agent_system import record_provider_event
        record_provider_event(provider, model, success, latency_ms, http_status, error)
    except Exception:
        # Telemetry must never make a successful model request fail.
        pass


def test_provider(provider: str) -> dict[str, Any]:
    """Perform a real, minimal model response test for one configured provider."""
    provider = provider.strip().lower()
    candidates, statuses = discover_models(force=True)
    status = next((item for item in statuses if item.get("provider") == provider), None)
    candidate = next((item for item in candidates if item.provider == provider), None)
    if candidate is None:
        return {"provider": provider, "success": False,
                "reason": (status or {}).get("note", "Provider is not configured or enabled."),
                "configured": bool((status or {}).get("configured")),
                "enabled": bool((status or {}).get("enabled"))}
    messages = [
        {"role": "system", "content": "Return exactly one JSON object: {\"ok\":true}. Do not add other text."},
        {"role": "user", "content": "Connection test. Return {\"ok\":true}."},
    ]
    started = time.monotonic()
    try:
        if candidate.kind == "gemini":
            answer = _gemini(candidate, messages)
        elif candidate.kind == "cloudflare":
            answer = _cloudflare(candidate, messages)
        else:
            answer = _openai(candidate, messages)
        if not isinstance(answer, dict):
            raise ProviderError("Provider returned no JSON object.")
        latency = int((time.monotonic() - started) * 1000)
        _record_provider_event(provider, candidate.model, True, latency)
        return {"provider": provider, "success": True, "model": candidate.model,
                "latency_ms": latency, "response_valid": True}
    except Exception as exc:
        status_code = exc.status if isinstance(exc, ProviderError) else None
        latency = int((time.monotonic() - started) * 1000)
        _record_provider_event(provider, candidate.model, False, latency, status_code, str(exc))
        return {"provider": provider, "success": False, "model": candidate.model,
                "latency_ms": latency, "reason": f"{type(exc).__name__}: {str(exc)[:500]}"}


def _candidate_json(candidate: Candidate, messages: list[dict[str, str]]) -> dict[str, Any]:
    if candidate.kind == "gemini":
        return _gemini(candidate, messages)
    if candidate.kind == "cloudflare":
        return _cloudflare(candidate, messages)
    return _openai(candidate, messages)


def benchmark_models(max_models: int = 3) -> dict[str, Any]:
    """Run a small deterministic quality/latency benchmark on local or catalog-confirmed-free models only."""
    from .agent_system import STORE

    safe_classes = {
        "local_open_source", "provider_free_tier_confirmed",
        "free_variant", "free_router", "zero_priced_catalog",
    }
    candidates, providers = discover_models(force=True)
    candidates = [candidate for candidate in candidates if candidate.free_class in safe_classes]
    candidates = _rank_candidates(candidates, "niebex")[:max(1, min(int(max_models), 5))]
    cases = [
        {
            "id": "arithmetic",
            "messages": [
                {"role": "system", "content": "Return exactly one JSON object with integer key answer. No markdown."},
                {"role": "user", "content": "Calculate 19 + 23. Return {\"answer\":42}."},
            ],
        },
        {
            "id": "arabic",
            "messages": [
                {"role": "system", "content": "Return exactly one JSON object with string key text. No markdown."},
                {"role": "user", "content": "Write the exact Arabic greeting phrase مرحبا بالعالم in the text value."},
            ],
        },
        {
            "id": "coding",
            "messages": [
                {"role": "system", "content": "Return exactly one JSON object with string key code. No markdown."},
                {"role": "user", "content": "Write a Python function named add with parameters a and b that returns a + b."},
            ],
        },
    ]
    results = []
    for candidate in candidates:
        checks: dict[str, bool] = {}
        latencies = []
        for case in cases:
            started = time.monotonic()
            try:
                answer = _candidate_json(candidate, case["messages"])
                latency = int((time.monotonic() - started) * 1000)
                latencies.append(latency)
                if case["id"] == "arithmetic":
                    passed = answer.get("answer") == 42
                elif case["id"] == "arabic":
                    value = str(answer.get("text") or "").replace(" ", "")
                    passed = "مرحبا" in value and "العالم" in value
                else:
                    value = str(answer.get("code") or "").casefold().replace(" ", "")
                    passed = "defadd(" in value and "returna+b" in value
                checks[case["id"]] = bool(passed)
                _record_provider_event(candidate.provider, candidate.model, True, latency)
            except Exception as exc:
                latency = int((time.monotonic() - started) * 1000)
                latencies.append(latency)
                checks[case["id"]] = False
                status = exc.status if isinstance(exc, ProviderError) else None
                _record_provider_event(candidate.provider, candidate.model, False, latency, status, str(exc))
        score = sum(1 for passed in checks.values() if passed) / len(cases)
        average_latency = int(sum(latencies) / len(latencies)) if latencies else 0
        STORE.record_model_evaluation(candidate.provider, candidate.model, "general",
                                       score, average_latency, checks)
        results.append({
            "provider": candidate.provider,
            "model": candidate.model,
            "free_class": candidate.free_class,
            "score": round(score, 3),
            "average_latency_ms": average_latency,
            "checks": checks,
            "requests_sent": len(cases),
        })
    return {
        "free_only_benchmark": True,
        "paid_models_tested": 0,
        "models_tested": len(results),
        "results": results,
        "note": "Three deterministic JSON tasks were executed per selected model. This measures these small test cases, not broad model intelligence.",
        "providers": providers,
    }


def public_model_catalog(force: bool = False) -> dict[str, Any]:
    models, providers = discover_models(force)
    return {
        "free_only": _FREE_ONLY,
        "paid_requests_allowed": _ALLOW_PAID,
        "spending_protection_enabled": not _ALLOW_PAID,
        "models": [m.public() for m in models],
        "providers": providers,
        "local_model_preference": _PREFER_LOCAL,
        "warning": "Model APIs do not reveal every account's billing tier or remaining free quota. Usage and quota values are never fabricated; paid traffic remains blocked unless explicitly authorized.",
    }
