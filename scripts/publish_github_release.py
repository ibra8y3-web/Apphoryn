#!/usr/bin/env python3
"""Publish isolated build outputs as assets in Apphoryn-owned GitHub Releases."""
from __future__ import annotations

import argparse
import base64
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.artifact_validation import ArtifactValidationError, validate_artifact
from app.domain import classify_artifact
from app.github_release_mirror import GitHubMirrorError, GitHubReleaseMirror


def has_build_outputs(directory: str | Path) -> bool:
    root = Path(directory)
    return root.is_dir() and any(
        item.is_file() and not item.is_symlink() and classify_artifact(item.name) is not None
        for item in root.rglob("*")
    )


def _apksigner_path() -> str | None:
    found = shutil.which("apksigner")
    if found:
        return found
    sdk = Path(os.getenv("ANDROID_HOME") or os.getenv("ANDROID_SDK_ROOT") or "")
    candidates = sorted((sdk / "build-tools").glob("*/apksigner")) if sdk else []
    return str(candidates[-1]) if candidates else None


def prepare_android_build_outputs(directory: str | Path) -> dict[str, int]:
    """Keep upstream signatures, or sign unsigned Release APKs in the trusted publisher.

    Signing keys are never passed to third-party source-build jobs.
    """
    root = Path(directory)
    apk_files = [item for item in root.rglob("*.apk") if item.is_file() and not item.is_symlink()]
    if not apk_files:
        return {"already_signed": 0, "signed_by_apphoryn": 0, "not_published": 0}

    keystore_b64 = os.getenv("APPHORYN_ANDROID_KEYSTORE_BASE64", "").strip()
    store_password = os.getenv("APPHORYN_ANDROID_KEYSTORE_PASSWORD", "")
    alias = os.getenv("APPHORYN_ANDROID_KEY_ALIAS", "").strip()
    key_password = os.getenv("APPHORYN_ANDROID_KEY_PASSWORD", "")
    signer = _apksigner_path()
    has_signing_key = bool(keystore_b64 and store_password and alias and key_password and signer)
    result = {"already_signed": 0, "signed_by_apphoryn": 0, "not_published": 0}

    for apk in apk_files:
        try:
            validate_artifact(apk, require_apk_signature=True)
            result["already_signed"] += 1
            continue
        except ArtifactValidationError:
            try:
                validate_artifact(apk, require_apk_signature=False)
            except ArtifactValidationError as exc:
                rejected = apk.with_name(apk.name + ".invalid")
                apk.rename(rejected)
                print(f"Rejected invalid Android package {apk.name}: {str(exc)[:250]}")
                result["not_published"] += 1
                continue

        if not has_signing_key:
            unsigned = apk.with_name(apk.name + ".unsigned")
            apk.rename(unsigned)
            print(
                "Android package not published: no persistent Apphoryn release keystore is configured. "
                "Set APPHORYN_ANDROID_KEYSTORE_BASE64, APPHORYN_ANDROID_KEYSTORE_PASSWORD, "
                "APPHORYN_ANDROID_KEY_ALIAS and APPHORYN_ANDROID_KEY_PASSWORD as GitHub Actions secrets."
            )
            result["not_published"] += 1
            continue

        with tempfile.TemporaryDirectory(prefix="apphoryn-android-sign-") as temp:
            temp_root = Path(temp)
            keystore = temp_root / "apphoryn-release.keystore"
            signed = temp_root / "signed.apk"
            try:
                keystore.write_bytes(base64.b64decode(keystore_b64, validate=True))
                os.chmod(keystore, 0o600)
                signing_env = os.environ.copy()
                signing_env["APPHORYN_ANDROID_KEYSTORE_PASSWORD"] = store_password
                signing_env["APPHORYN_ANDROID_KEY_PASSWORD"] = key_password
                subprocess.run([
                    signer, "sign", "--ks", str(keystore), "--ks-key-alias", alias,
                    "--ks-pass", "env:APPHORYN_ANDROID_KEYSTORE_PASSWORD",
                    "--key-pass", "env:APPHORYN_ANDROID_KEY_PASSWORD",
                    "--out", str(signed), str(apk),
                ], capture_output=True, text=True, timeout=180, check=True, env=signing_env)
                validate_artifact(signed, require_apk_signature=True)
                os.replace(signed, apk)
                result["signed_by_apphoryn"] += 1
            except (OSError, subprocess.SubprocessError, ValueError, ArtifactValidationError) as exc:
                unsigned = apk.with_name(apk.name + ".unsigned")
                apk.rename(unsigned)
                print(f"Android package was not signed and will not be published: {type(exc).__name__}: {str(exc)[:250]}")
                result["not_published"] += 1
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--ref", required=True)
    args = parser.parse_args()
    if not has_build_outputs(args.directory):
        print("No verified build outputs were produced for this request; nothing to publish.")
        return
    signing = prepare_android_build_outputs(args.directory)
    if any(signing.values()):
        print("Android release signing summary: " + ", ".join(f"{key}={value}" for key, value in signing.items()))
    if not has_build_outputs(args.directory):
        if signing["not_published"]:
            raise SystemExit(
                "No installable package was published. Android APK outputs were withheld because they were unsigned or invalid; "
                "configure a persistent Apphoryn Android release keystore in GitHub Actions secrets to publish source-built APKs."
            )
        print("No publishable outputs remain after security checks; nothing to publish.")
        return
    token = os.getenv("GITHUB_TOKEN", "")
    target_repository = os.getenv("GITHUB_REPOSITORY", "")
    branch = os.getenv("APPHORYN_GITHUB_REF", "main")
    if not token or not target_repository:
        raise SystemExit("GITHUB_TOKEN and GITHUB_REPOSITORY are required; no package was published.")
    try:
        mirror = GitHubReleaseMirror(token, target_repository, branch)
        result = mirror.publish_built_artifacts(args.repository, args.ref, Path(args.directory))
    except (GitHubMirrorError, OSError, ValueError) as exc:
        raise SystemExit(f"Apphoryn build publication failed: {exc}") from exc
    print(f"Published {len(result['published'])} validated build package(s).")
    print(result["release_url"])
    for artifact in result["published"]:
        print(f"{artifact['platform']} {artifact['filename']} sha256={artifact['sha256']}")


if __name__ == "__main__":
    main()
