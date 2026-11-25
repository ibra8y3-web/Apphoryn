#!/usr/bin/env python3
"""Build a licensed source project on a disposable native runner.

This is an explicit adapter-based builder, not a universal compiler. It never
passes Apphoryn publishing tokens or signing keys to third-party build commands.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.artifact_validation import ArtifactValidationError, validate_artifact
from app.domain import canonical_repository_url, license_allows_redistribution, valid_git_ref
from app.github_client import GitHubClient

KNOWN_SUFFIXES = {".apk", ".exe", ".msi", ".msix", ".appx", ".appimage", ".deb", ".rpm", ".dmg", ".pkg", ".jar", ".bin", ".macosbin"}


def run(command: list[str], cwd: Path, timeout: int = 2400) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, check=True, timeout=timeout)


def clone_source(repository_url: str, git_ref: str, source_dir: Path) -> None:
    run(["git", "clone", "--no-tags", "--filter=blob:none", repository_url, str(source_dir)], cwd=ROOT, timeout=600)
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
            if candidate.is_file():
                # Preserve upstream release signatures when supplied by the project.
                # Unsigned release APKs are passed only as data to the trusted publisher,
                # which may sign them with Apphoryn's persistent release keystore.
                shutil.copy2(candidate, output / candidate.name)
    if not any(p.suffix.lower() == ".apk" for p in output.iterdir() if p.is_file()):
        raise RuntimeError("Gradle produced no release APK. Debug APKs and AAB-only builds are not installable APK outputs.")


def build_dotnet(source: Path, output: Path, target: str) -> None:
    projects = list(source.glob("**/*.csproj"))[:10]
    if not projects:
        raise RuntimeError("No .NET project found.")
    runtimes = {"windows": "win-x64", "linux": "linux-x64", "macos": "osx-x64"}
    rid = runtimes[target]
    for project in projects:
        published = output / project.stem
        run(["dotnet", "publish", str(project), "-c", "Release", "-r", rid, "--self-contained", "true", "-p:UseAppHost=true", "-o", str(published)], cwd=source, timeout=2400)
        if target == "linux":
            apphost = published / project.stem
            if apphost.is_file():
                shutil.copy2(apphost, output / f"{project.stem}.bin")
        elif target == "macos":
            apphost = published / project.stem
            if apphost.is_file():
                shutil.copy2(apphost, output / f"{project.stem}.macosbin")


def build_go(source: Path, output: Path, target: str) -> None:
    projects = [
        path for path in source.glob("**/go.mod")
        if not any(part in {"vendor", ".git", "node_modules", "build"} for part in path.parts)
    ][:10]
    if not projects:
        raise RuntimeError("No Go module found.")
    env = os.environ.copy()
    env["CGO_ENABLED"] = "0"
    env["GOOS"] = {"windows": "windows", "linux": "linux", "macos": "darwin"}[target]
    env["GOARCH"] = "amd64"
    built = 0

    for module in projects:
        module_root = module.parent
        nested_roots = {
            other.parent.resolve()
            for other in projects
            if other.parent != module_root and module_root in other.parent.parents
        }
        main_packages: set[Path] = set()
        for source_file in module_root.rglob("*.go"):
            if source_file.name.endswith("_test.go"):
                continue
            relative_parts = source_file.relative_to(module_root).parts
            if any(part in {"vendor", ".git", "node_modules", "build"} for part in relative_parts):
                continue
            if any(root in source_file.resolve().parents for root in nested_roots):
                continue
            try:
                source_text = source_file.read_text(encoding="utf-8", errors="replace")[:128 * 1024]
            except OSError:
                continue
            if re.search(r"(?m)^\\s*package\\s+main(?:\\s|$)", source_text):
                main_packages.add(source_file.parent)

        for package_dir in sorted(main_packages):
            relative = package_dir.relative_to(module_root)
            package_arg = "." if relative == Path(".") else "./" + relative.as_posix()
            base_name = module_root.name or "app"
            if relative != Path("."):
                base_name += "-" + "-".join(relative.parts)
            suffix = ".exe" if target == "windows" else (".macosbin" if target == "macos" else ".bin")
            dest = output / f"{base_name}{suffix}"
            command = ["go", "build", "-trimpath", "-ldflags=-s -w", "-o", str(dest), package_arg]
            print("+", " ".join(command), flush=True)
            subprocess.run(command, cwd=module_root, env=env, check=True, timeout=1800)
            built += 1

    if not built:
        raise RuntimeError(
            "Go modules were found, but no executable package (package main) exists. "
            "A library or empty module is not built as a standalone application."
        )


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
        if not valid_git_ref(args.ref):
            raise RuntimeError("Invalid Git ref; only a safe branch, tag or commit identifier is accepted.")
        canonical = canonical_repository_url(args.repository)
        owner, repo_name = canonical.removeprefix("https://github.com/").split("/", 1)
        metadata = GitHubClient().get_repository(owner, repo_name)
        spdx = (metadata.get("license") or {}).get("spdx_id")
        if metadata.get("archived"):
            raise RuntimeError("Archived repositories cannot be built for automatic redistribution.")
        if not license_allows_redistribution(spdx):
            raise RuntimeError(f"Build blocked: upstream SPDX license {spdx or 'missing'} is not approved for redistribution.")
        clone_source(canonical, args.ref, source)
        if args.target == "android":
            build_android(source, output)
        elif list(source.glob("**/*.csproj")):
            build_dotnet(source, output, args.target)
        elif list(source.glob("**/go.mod")):
            build_go(source, output, args.target)
        elif list(source.glob("**/package.json")):
            build_electron(source, output)
        else:
            raise RuntimeError(f"No supported {args.target} build adapter detected. Add a reviewed adapter for this project.")
        artifacts = [p for p in output.rglob("*") if p.is_file() and p.suffix.lower() in KNOWN_SUFFIXES]
        if not artifacts:
            raise RuntimeError("Build finished without a recognized installable artifact.")
        validated = []
        for path in artifacts:
            try:
                details = validate_artifact(path, require_apk_signature=True)
                validated.append((path, details))
            except ArtifactValidationError as exc:
                path.unlink(missing_ok=True)
                print(f"REJECTED_ARTIFACT={path.name}: {exc}", file=sys.stderr)
        if not validated:
            raise RuntimeError("No structurally valid, signed installable artifact remained after verification.")
        print("APPHORYN_BUILD_SUCCESS")
        for path, details in validated:
            print(f"APPHORYN_ARTIFACT={path} SHA256_VALIDATED={details['valid']} SIGNATURE={details['signature_status']}")
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
