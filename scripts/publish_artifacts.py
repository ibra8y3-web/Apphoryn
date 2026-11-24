#!/usr/bin/env python3
"""Stream finished build artifacts to Apphoryn from a separate, trusted runner."""
from __future__ import annotations

import argparse
import hashlib
import http.client
import os
import re
import ssl
from pathlib import Path
from urllib.parse import urlparse

SUFFIXES = {".apk", ".aab", ".exe", ".msi", ".msix", ".appx", ".appimage", ".deb", ".rpm", ".dmg", ".pkg", ".jar", ".bin", ".macosbin"}
PLATFORM = {".apk":"android", ".aab":"android", ".exe":"windows", ".msi":"windows", ".msix":"windows", ".appx":"windows", ".appimage":"linux", ".deb":"linux", ".rpm":"linux", ".dmg":"macos", ".pkg":"macos", ".jar":"java", ".bin":"linux", ".macosbin":"macos"}


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def post_file(base: str, headers: dict[str, str], path: Path) -> str:
    parsed = urlparse(base)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Artifact ingest URL must use HTTPS.")
    request_path = parsed.path.rstrip("/") + "/api/v1/admin/artifacts/upload"
    if parsed.query:
        raise ValueError("Artifact ingest base URL must not include a query string.")
    connection = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=240, context=ssl.create_default_context())
    try:
        connection.putrequest("POST", request_path)
        connection.putheader("Content-Type", "application/octet-stream")
        connection.putheader("Transfer-Encoding", "chunked")
        for name, value in headers.items():
            connection.putheader(name, value)
        connection.endheaders()
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                connection.send(f"{len(chunk):X}\r\n".encode("ascii"))
                connection.send(chunk)
                connection.send(b"\r\n")
        connection.send(b"0\r\n\r\n")
        response = connection.getresponse()
        body = response.read(4096).decode("utf-8", errors="replace")
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"Artifact ingest failed: HTTP {response.status}: {body}")
        return body
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", required=True)
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--version", default="unknown")
    args = parser.parse_args()
    base = os.environ.get("APPHORYN_INGEST_URL", "").rstrip("/")
    token = os.environ.get("APPHORYN_INGEST_TOKEN", "")
    if not base.startswith("https://") or not token:
        raise SystemExit("APPHORYN_INGEST_URL (HTTPS) and APPHORYN_INGEST_TOKEN are required.")
    if not re.fullmatch(r"[A-Za-z0-9-]{1,80}", args.app_id):
        raise SystemExit("Invalid Apphoryn app id.")
    if not re.fullmatch(r"[A-Za-z0-9._/-]{1,120}", args.ref) or ".." in args.ref:
        raise SystemExit("Invalid Git ref.")
    parsed_repo = urlparse(args.repository)
    repo_parts = [part for part in parsed_repo.path.strip("/").split("/") if part]
    if (parsed_repo.scheme != "https" or parsed_repo.netloc != "github.com" or parsed_repo.query or parsed_repo.fragment
            or len(repo_parts) != 2 or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in repo_parts)):
        raise SystemExit("Source must be an exact HTTPS GitHub owner/repository URL.")
    if not re.fullmatch(r"[A-Za-z0-9-]{1,80}", args.job_id):
        raise SystemExit("Invalid Apphoryn job id.")
    root = Path(args.directory).resolve(strict=True)
    files = []
    for candidate in root.rglob("*"):
        if candidate.is_symlink() or not candidate.is_file() or candidate.suffix.lower() not in SUFFIXES:
            continue
        resolved = candidate.resolve(strict=True)
        if root not in resolved.parents:
            continue
        if resolved.stat().st_size > 1024 * 1024 * 1024:
            raise SystemExit(f"Artifact exceeds 1 GiB upload cap: {candidate.name}")
        files.append(resolved)
    if not files:
        raise SystemExit("No supported installable artifacts to publish.")
    for path in files:
        suffix = path.suffix.lower()
        safe_name = re.sub(r"[^A-Za-z0-9._+-]+", "_", path.name).strip("._")[:180]
        version = re.sub(r"[^A-Za-z0-9._+/-]+", "-", args.version)[:120] or "unknown"
        if not safe_name:
            raise SystemExit(f"Unsafe artifact filename: {path.name!r}")
        headers = {
            "X-Apphoryn-Token": token,
            "X-Apphoryn-App-Id": args.app_id,
            "X-Apphoryn-Filename": safe_name,
            "X-Apphoryn-Platform": PLATFORM[suffix],
            "X-Apphoryn-Version": version,
            "X-Apphoryn-Source-Repo": args.repository,
            "X-Apphoryn-Git-Ref": args.ref,
            "X-Apphoryn-Sha256": file_hash(path),
            "X-Apphoryn-Job-Id": args.job_id[:80],
        }
        print(post_file(base, headers, path))
    print(f"Published {len(files)} artifact(s) to Apphoryn.")


if __name__ == "__main__":
    main()
