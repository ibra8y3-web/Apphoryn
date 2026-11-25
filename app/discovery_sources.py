from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any
from xml.etree import ElementTree


class SourceError(RuntimeError):
    pass


def _request_json(url: str, method: str = "GET", body: dict[str, Any] | None = None) -> Any:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {
        "search.f-droid.org", "flathub.org", "dl.flathub.org"
    }:
        raise SourceError("Discovery source host is not allowlisted.")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "Accept": "application/json",
        "User-Agent": "Apphoryn-Discovery/1.0 (+https://github.com/ibra8y3-web/Apphoryn)",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=18) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise SourceError("Discovery source response exceeded the 8 MiB limit.")
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SourceError(f"Discovery API returned HTTP {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise SourceError(f"Discovery source request failed: {type(exc).__name__}.") from exc


def _text(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _fdroid_search(query: str, limit: int) -> list[dict[str, Any]]:
    url = "https://search.f-droid.org/api/search_apps?" + urllib.parse.urlencode({"q": query})
    payload = _request_json(url)
    rows = payload if isinstance(payload, list) else (
        payload.get("apps") or payload.get("results") or payload.get("items") or []
        if isinstance(payload, dict) else []
    )
    results = []
    for item in rows[:limit] if isinstance(rows, list) else []:
        if not isinstance(item, dict):
            continue
        package_id = _text(item, "packageName", "package_name", "id", "package")
        name = _text(item, "name", "localizedName", "packageName") or package_id
        if not package_id or not name:
            continue
        results.append({
            "source": "fdroid",
            "source_record_id": package_id,
            "name": name,
            "summary": _text(item, "summary", "description", "shortDescription"),
            "version": _text(item, "suggestedVersionName", "versionName", "version"),
            "homepage_url": _text(item, "webSite", "homepage", "sourceCode"),
            "license": _text(item, "license", "licenseName"),
            "repository_url": _text(item, "sourceCode", "source_code"),
            "catalog_url": "https://f-droid.org/packages/" + urllib.parse.quote(package_id, safe=""),
            "trust_state": "metadata_only",
            "installable_source": "F-Droid signed repository; package signature and index trust must be independently verified before mirroring",
        })
    return results


def _flathub_search(query: str, limit: int) -> list[dict[str, Any]]:
    payload = _request_json(
        "https://flathub.org/api/v2/search",
        method="POST",
        body={"query": query, "filters": []},
    )
    rows = payload if isinstance(payload, list) else (
        payload.get("hits") or payload.get("items") or payload.get("results") or []
        if isinstance(payload, dict) else []
    )
    results = []
    for item in rows[:limit] if isinstance(rows, list) else []:
        if not isinstance(item, dict):
            continue
        app_id = _text(item, "app_id", "appId", "id", "flatpakAppId")
        name = _text(item, "name", "title") or app_id
        if not app_id or not name:
            continue
        results.append({
            "source": "flathub",
            "source_record_id": app_id,
            "name": name,
            "summary": _text(item, "summary", "description"),
            "version": _text(item, "version", "currentReleaseVersion"),
            "license": _text(item, "project_license", "license"),
            "homepage_url": _text(item, "homepage", "url"),
            "repository_url": "https://github.com/flathub/" + app_id,
            "catalog_url": "https://flathub.org/apps/" + urllib.parse.quote(app_id, safe=""),
            "api_url": "https://flathub.org/api/v2/appstream/" + urllib.parse.quote(app_id, safe=""),
            "trust_state": "flathub_catalog_metadata",
            "installable_source": "Flathub OSTree/Flatpak repository; do not treat metadata or a GitHub manifest as an installer",
        })
    return results


class DiscoverySources:
    """Metadata discovery across official catalogs; never downloads or executes their packages."""

    def search(self, query: str, limit: int = 10) -> dict[str, Any]:
        query = query.strip()[:180]
        limit = max(1, min(int(limit), 20))
        if len(query) < 2:
            raise ValueError("Search query must contain at least two characters.")
        sources: dict[str, Any] = {}
        combined: list[dict[str, Any]] = []
        for name, handler in (("fdroid", _fdroid_search), ("flathub", _flathub_search)):
            try:
                found = handler(query, limit)
                sources[name] = {"ok": True, "count": len(found)}
                combined.extend(found)
            except Exception as exc:
                sources[name] = {"ok": False, "count": 0,
                                 "error": f"{type(exc).__name__}: {str(exc)[:240]}"}
        deduplicated: dict[str, dict[str, Any]] = {}
        for item in combined:
            key = str(item.get("source_record_id") or "").casefold()
            if key:
                deduplicated[f"{item['source']}:{key}"] = item
        return {
            "query": query,
            "sources": sources,
            "count": len(deduplicated),
            "items": list(deduplicated.values())[:limit * 2],
            "limitations": [
                "Discovery metadata is not proof of redistribution permission or package safety.",
                "F-Droid index signatures and APK signer identity must be verified before any F-Droid binary is mirrored.",
                "Flathub appstream metadata is not a verified native Windows/macOS/Android installer.",
            ],
        }
