#!/usr/bin/env python3
"""Build an eligible source repository on a disposable, platform-native CI runner.

This is deliberately an adapter-based builder, not a universal compiler. It never
passes deployment/signing secrets to third-party build commands.
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KNOWN_SUFFIXES = {".apk", ".aab", ".exe", ".msi", ".msix", ".appx", ".appimage", ".deb", ".rpm", ".dmg", ".pkg", ".jar", ".bin", ".macosbin"}


def run(command: list[str], cwd: Path, timeout: int = 2400) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, check=True, timeout=timeout)


def clone_source(repository_url: str, git_ref: str, source_dir: Path) -> None:
    run(["git", "clone", "--no-tags", "--filter=blob:none", repository_url, str(source_dir)], cwd=ROOT, timeout=600)
    # Resolve the exact requested branch/tag/commit after cloning.
    run(["git", "-C", str(source_dir), "fetch", "--depth", "1", "origin", git_ref], cwd=ROOT, timeout=600)
    run(["git", "-C", str(source_dir), "checkout", "--detach", "FETCH_HEAD"], cwd=ROOT)


def find_apksigner() -> str | None:
    found = shutil.which("apksigner")
    if found:
        return found
    sdk = Path(os.environ.get("ANDROID_HOME", os.environ.get("ANDROID_SDK_ROOT", "")))
    candidates = sorted((sdk / "build-tools").glob("*/apksigner*"), reverse=True) if sdk.exists() else []
    return str(candidates[0]) if candidates else None


def apk_is_signed(path: Path, apksigner: str | None) -> bool:
    if not apksigner:
        return False
    try:
        result = subprocess.run([apksigner, "verify", "--verbose", str(path)], capture_output=True, text=True, timeout=60)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def build_android(source: Path, output: Path) -> None:
    wrappers = list(source.glob("**/gradlew"))[:10]
    wrappers = [p for p in wrappers if "build" not in p.parts and ".gradle" not in p.parts]
    if not wrappers:
        raise RuntimeError("No Gradle wrapper found; Android adapter cannot build this repository.")
    apksigner = find_apksigner()
    for wrapper in wrappers:
        project = wrapper.parent
        wrapper.chmod(wrapper.stat().st_mode | 0o111)
        run([str(wrapper), "--no-daemon", "--stacktrace", "assembleRelease"], cwd=project, timeout=2400)
        release_apks = list(project.glob("**/build/outputs/apk/**/*.apk"))
        for candidate in release_apks:
            if candidate.is_file() and apk_is_signed(candidate, apksigner):
                shutil.copy2(candidate, output / candidate.name)
        # A debug APK is signed by the Android Gradle Plugin's generated debug key.
        # It is installable for testing, but is not a publisher-signed production release.
        if not any(p.suffix.lower() == ".apk" for p in output.iterdir() if p.is_file()):
            run([str(wrapper), "--no-daemon", "--stacktrace", "assembleDebug"], cwd=project, timeout=2400)
            debug_apks = list(project.glob("**/build/outputs/apk/**/*debug*.apk"))
            for candidate in debug_apks:
                if candidate.is_file() and apk_is_signed(candidate, apksigner):
                    shutil.copy2(candidate, output / candidate.name)
        # AAB files are retained as store bundles, but are not direct-install APKs.
        for candidate in project.glob("**/build/outputs/bundle/**/*.aab"):
            if candidate.is_file():
                shutil.copy2(candidate, output / candidate.name)
    if not any(p.suffix.lower() in {".apk", ".aab"} for p in output.iterdir() if p.is_file()):
        raise RuntimeError("No signed APK or Android App Bundle was produced. Unsigned APKs are rejected.")


def build_dotnet(source: Path, output: Path, target: str) -> None:
    projects = list(source.glob("**/*.csproj"))[:10]
    if not projects:
        raise RuntimeError("No .NET project found.")
    runtimes = {"windows": "win-x64", "linux": "linux-x64", "macos": "osx-x64"}
    rid = runtimes[target]
    for project in projects:
        run(["dotnet", "publish", str(project), "-c", "Release", "-r", rid, "--self-contained", "true", "-o", str(output / project.stem)], cwd=source, timeout=2400)
    # The project may produce an .exe on Windows; other raw native outputs are
    # intentionally not relabelled as an installer package.


def build_go(source: Path, output: Path, target: str) -> None:
    projects = list(source.glob("**/go.mod"))[:10]
    if not projects:
        raise RuntimeError("No Go module found.")
    env = os.environ.copy()
    env["CGO_ENABLED"] = "0"
    env["GOOS"] = {"windows": "windows", "linux": "linux", "macos": "darwin"}[target]
    env["GOARCH"] = "amd64"
    for module in projects:
        dest = output / (module.parent.name + (".exe" if target == "windows" else (".macosbin" if target == "macos" else ".bin")))
        command = ["go", "build", "-trimpath", "-ldflags=-s -w", "-o", str(dest), "."]
        print("+", " ".join(command), flush=True)
        subprocess.run(command, cwd=module.parent, env=env, check=True, timeout=1800)


def build_electron(source: Path, output: Path) -> None:
    package_files = list(source.glob("**/package.json"))[:8]
    candidates = [p for p in package_files if "electron" in p.read_text(encoding="utf-8", errors="ignore").lower()]
    if not candidates:
        raise RuntimeError("No Electron project detected.")
    for package in candidates:
        project = package.parent
        run(["npm", "ci", "--no-audit", "--no-fund"], cwd=project, timeout=1200)
        data = package.read_text(encoding="utf-8", errors="ignore")
        if '"dist"' in data or "electron-builder" in data:
            run(["npm", "run", "dist"], cwd=project, timeout=2400)
        else:
            raise RuntimeError("Electron project has no recognized dist script; configure a platform packaging script.")
        for candidate in (project / "dist").glob("**/*"):
            if candidate.is_file() and candidate.suffix.lower() in KNOWN_SUFFIXES:
                shutil.copy2(candidate, output / candidate.name)


def collect_known_artifacts(source: Path, output: Path) -> None:
    # Collect only recognized installable formats, never arbitrary source files.
    for candidate in source.glob("**/*"):
        if not candidate.is_file() or candidate.suffix.lower() not in KNOWN_SUFFIXES:
            continue
        if any(part in {".git", "node_modules", ".gradle", "__pycache__"} for part in candidate.parts):
            continue
        if candidate.stat().st_size == 0:
            continue
        destination = output / candidate.name
        if not destination.exists():
            shutil.copy2(candidate, destination)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--target", required=True, choices=["android", "windows", "linux", "macos"])
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    work = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / f"apphoryn-source-{os.getpid()}"
    if work.exists():
        shutil.rmtree(work)
    source = work / "source"
    work.mkdir(parents=True)
    try:
        clone_source(args.repository, args.ref, source)
        if args.target == "android":
            build_android(source, output)
        else:
            # Build adapters are explicit and finite. Projects without a known
            # adapter fail visibly rather than being published as successful.
            if list(source.glob("**/*.csproj")):
                build_dotnet(source, output, args.target)
            elif list(source.glob("**/go.mod")):
                build_go(source, output, args.target)
            elif list(source.glob("**/package.json")):
                build_electron(source, output)
            else:
                raise RuntimeError(f"No supported {args.target} build adapter detected. Add a reviewed adapter for this project.")
        collect_known_artifacts(source, output)
        artifacts = [p for p in output.rglob("*") if p.is_file() and p.suffix.lower() in KNOWN_SUFFIXES]
        if not artifacts:
            raise RuntimeError("Build finished without a recognized installable artifact.")
        print("APPHORYN_BUILD_SUCCESS")
        for path in artifacts:
            print(f"APPHORYN_ARTIFACT={path}")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print(f"Build command failed with exit code {exc.returncode}", file=sys.stderr)
        raise
    except Exception as exc:
        print(f"Apphoryn build failed: {exc}", file=sys.stderr)
        raise
