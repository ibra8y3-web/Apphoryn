from __future__ import annotations

import hashlib
import re
from pathlib import PurePosixPath
from urllib.parse import urlparse

REDISTRIBUTION_LICENSES = {
    "MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC",
    "MPL-2.0", "GPL-2.0", "GPL-2.0-only", "GPL-2.0-or-later",
    "GPL-3.0", "GPL-3.0-only", "GPL-3.0-or-later", "LGPL-2.1",
    "LGPL-2.1-only", "LGPL-2.1-or-later", "LGPL-3.0", "LGPL-3.0-only",
    "LGPL-3.0-or-later", "AGPL-3.0", "AGPL-3.0-only", "AGPL-3.0-or-later",
    "0BSD", "Zlib", "Unlicense", "BSL-1.0",
}

ARTIFACT_TYPES = {
    ".apk": ("android", "Android package"),
    ".xapk": ("android", "Android split package bundle"),
    ".exe": ("windows", "Windows executable"),
    ".msi": ("windows", "Windows installer"),
    ".msix": ("windows", "Windows package"),
    ".appx": ("windows", "Windows package"),
    ".appimage": ("linux", "Linux AppImage"),
    ".deb": ("linux", "Debian package"),
    ".rpm": ("linux", "RPM package"),
    ".dmg": ("macos", "macOS disk image"),
    ".pkg": ("macos", "macOS installer"),
    ".jar": ("java", "Java archive"),
    ".bin": ("linux", "Linux portable executable"),
    ".macosbin": ("macos", "macOS native executable"),
}


def canonical_repository_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme != "https" or parsed.netloc.lower() not in {"github.com", "www.github.com"}:
        raise ValueError("Only public HTTPS GitHub repository URLs are accepted.")
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if len(parts) != 2 or any(p in {".", ".."} for p in parts):
        raise ValueError("URL must identify exactly one GitHub owner/repository.")
    owner, repo = parts
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", repo):
        raise ValueError("Invalid GitHub owner or repository name.")
    repo = repo.removesuffix(".git")
    return f"https://github.com/{owner}/{repo}"


def repository_slug(repository_url: str) -> str:
    path = urlparse(canonical_repository_url(repository_url)).path.strip("/")
    slug = re.sub(r"[^a-z0-9]+", "-", path.lower()).strip("-")
    return slug[:90] or "software-project"


def normalize_license(license_spdx: str | None) -> str | None:
    return license_spdx.strip() if license_spdx else None


def license_allows_redistribution(license_spdx: str | None) -> bool:
    return normalize_license(license_spdx) in REDISTRIBUTION_LICENSES


def valid_git_ref(value: str) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 120:
        return False
    if value.startswith("-") or value.endswith(("/", ".", ".lock")) or ".." in value or "//" in value or "@{" in value:
        return False
    if any(part.startswith(".") for part in value.split("/")):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9._/-]+", value))


def classify_artifact(filename: str) -> tuple[str, str] | None:
    return ARTIFACT_TYPES.get(PurePosixPath(filename.lower()).suffix)


def safe_filename(filename: str) -> str:
    name = PurePosixPath(filename.replace("\\", "/")).name
    name = re.sub(r"[^A-Za-z0-9._+-]+", "_", name).strip("._")
    if not name or name in {".", ".."}:
        raise ValueError("Unsafe artifact filename.")
    return name[:180]


def sha256_file(path: str, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def infer_category(topics: list[str] | None, description: str | None) -> str:
    words = " ".join((topics or []) + [description or ""]).lower()
    mappings = {
        "developer": ("developer", "programming", "ide", "editor", "compiler", "sdk"),
        "security": ("security", "privacy", "cryptography", "password", "firewall"),
        "multimedia": ("audio", "video", "image", "photo", "media", "graphics"),
        "network": ("network", "browser", "server", "vpn", "proxy", "internet"),
        "productivity": ("office", "document", "notes", "calendar", "task", "productivity"),
        "system": ("system", "file manager", "backup", "disk", "utility", "storage"),
        "ai": ("artificial intelligence", "machine learning", "llm", "neural", "ai model"),
        "games": ("game", "gaming", "game engine"),
    }
    for category, keywords in mappings.items():
        if any(word in words for word in keywords):
            return category
    return "other"
