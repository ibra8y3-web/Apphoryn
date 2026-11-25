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
from app.build_support import dotnet_project_targets, electron_build_script

KNOWN_SUFFIXES = {".apk", ".exe", ".msi", ".msix", ".appx", ".appimage", ".deb", ".rpm", ".dmg", ".pkg", ".jar", ".bin", ".macosbin"}


def command_for_runner(
    command: list[str], *, is_windows: bool | None = None, which=shutil.which
) -> list[str]:
    """Run npm/npx reliably on Windows, where executables are .cmd shims."""
    windows = os.name == "nt" if is_windows is None else is_windows
    if not command or not windows or command[0].lower() not in {"npm", "npx"}:
        return command
    program = command[0].lower()
    executable = which(program + ".cmd") or which(program)
    if not executable:
        raise FileNotFoundError(f"Could not locate Windows {program}.cmd executable.")
    shell = os.environ.get("COMSPEC", "cmd.exe")
    return [shell, "/d", "/s", "/c", subprocess.list2cmdline([executable, *command[1:]])]


def run(command: list[str], cwd: Path, timeout: int = 2400) -> None:
    resolved = command_for_runner(command)
    print("+", " ".join(resolved), flush=True)
    subprocess.run(resolved, cwd=cwd, check=True, timeout=timeout)


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


def remove_upstream_android_signing_gate(source: Path) -> int:
    """Remove only an upstream pre-build signing guard in the disposable clone.

    The source build remains keyless. The trusted publisher validates the APK
    and signs it with Apphoryn's persistent release identity after upload.
    """
    removed = 0
    for gradle_file in source.glob("**/build.gradle*"):
        if not gradle_file.is_file() or gradle_file.is_symlink():
            continue
        content = gradle_file.read_text(encoding="utf-8", errors="replace")
        if "Release signing is not configured" not in content:
            continue
        lines = content.splitlines(keepends=True)
        start = next((
            index for index, line in enumerate(lines)
            if line.startswith("tasks.matching { it.name == \"packageRelease\"")
        ), None)
        if start is None:
            continue
        end = next((
            index for index in range(start + 1, len(lines))
            if lines[index].rstrip("\r\n") == "}"
        ), None)
        if end is None:
            raise RuntimeError(f"Could not safely isolate upstream signing guard in {gradle_file}.")
        block = "".join(lines[start:end + 1])
        if "packageReleaseBundle" not in block or "CAIMACK_" not in block:
            raise RuntimeError(f"Unrecognized upstream signing guard in {gradle_file}; refusing to modify it.")
        gradle_file.write_text("".join(lines[:start] + lines[end + 1:]), encoding="utf-8")
        removed += 1
        print(f"Removed upstream-only release-key requirement from disposable source copy: {gradle_file}")
    return removed


def build_android(source: Path, output: Path) -> None:
    remove_upstream_android_signing_gate(source)
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
    projects = sorted(source.glob("**/*.csproj"), key=lambda path: (len(path.parts), str(path)))[:24]
    if not projects:
        raise RuntimeError("No .NET project found.")
    runtimes = {"windows": "win-x64", "linux": "linux-x64", "macos": "osx-x64"}
    if target not in runtimes:
        raise RuntimeError(f"Unsupported .NET target: {target}")
    rid = runtimes[target]
    selected = []
    for project in projects:
        try:
            project_xml = project.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        relative_path = project.relative_to(source).as_posix()
        if target in dotnet_project_targets(relative_path, project_xml):
            selected.append(project)
    if not selected:
        raise RuntimeError(f"No executable .NET project supports the {target} target.")

    for project in selected:
        published = output / project.stem
        run([
            "dotnet", "publish", str(project), "-c", "Release", "-r", rid,
            "--self-contained", "true", "-p:UseAppHost=true", "-o", str(published),
        ], cwd=source, timeout=2400)
        suffix = ".exe" if target == "windows" else (".macosbin" if target == "macos" else ".bin")
        apphost = published / f"{project.stem}{suffix if suffix == '.exe' else ''}"
        if target == "windows":
            apphost = published / f"{project.stem}.exe"
        elif target in {"linux", "macos"}:
            apphost = published / project.stem
        if apphost.is_file():
            shutil.copy2(apphost, output / f"{project.stem}{suffix}")


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


def build_electron(source: Path, output: Path, target: str) -> None:
    package_files = sorted(source.glob("**/package.json"), key=lambda path: (len(path.parts), str(path)))[:32]
    candidates: list[tuple[Path, dict, str]] = []
    for package_file in package_files:
        try:
            package_data = json.loads(package_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(package_data, dict):
            continue
        dependencies: dict[str, object] = {}
        for key in ("dependencies", "devDependencies", "optionalDependencies"):
            value = package_data.get(key)
            if isinstance(value, dict):
                dependencies.update(value)
        scripts = package_data.get("scripts") if isinstance(package_data.get("scripts"), dict) else {}
        selected_script = electron_build_script(scripts, target)
        if "electron" in dependencies and "electron-builder" in dependencies and selected_script:
            candidates.append((package_file, package_data, selected_script))
    if not candidates:
        raise RuntimeError(f"No Electron packaging script for {target} was detected.")

    built_any = False
    for package_file, package_data, selected_script in candidates:
        project = package_file.parent
        build_command = str((package_data.get("scripts") or {}).get("build") or "")
        frontend = project.parent / "frontend"
        if (
            frontend != project
            and (frontend / "package-lock.json").is_file()
            and "frontend/node_modules" in build_command.replace("\\", "/")
        ):
            run(["npm", "ci", "--no-audit", "--no-fund", "--prefix", str(frontend)], cwd=source, timeout=1800)

        install_command = (
            ["npm", "ci", "--no-audit", "--no-fund"]
            if (project / "package-lock.json").is_file()
            else ["npm", "install", "--no-audit", "--no-fund", "--package-lock=false"]
        )
        run(install_command, cwd=project, timeout=1800)
        run(["npm", "run", selected_script], cwd=project, timeout=3600)

        output_directories = [project / "release", project / "dist", project / "out", project / "build"]
        build_config = package_data.get("build") if isinstance(package_data.get("build"), dict) else {}
        configured_output = (build_config.get("directories") or {}).get("output") if isinstance(build_config.get("directories"), dict) else None
        if isinstance(configured_output, str) and configured_output.strip():
            output_directories.append((project / configured_output).resolve())

        source_root = source.resolve()
        seen: set[Path] = set()
        for directory in output_directories:
            resolved_directory = directory.resolve()
            if resolved_directory in seen or source_root not in resolved_directory.parents:
                continue
            seen.add(resolved_directory)
            if not resolved_directory.is_dir():
                continue
            for candidate in resolved_directory.rglob("*"):
                if not candidate.is_file() or candidate.is_symlink():
                    continue
                if candidate.suffix.lower() not in KNOWN_SUFFIXES:
                    continue
                destination = output / candidate.name
                if destination.exists():
                    destination = output / f"{candidate.stem}-{project.name}{candidate.suffix}"
                shutil.copy2(candidate, destination)
                built_any = True
    if not built_any:
        raise RuntimeError(f"Electron '{target}' build produced no recognized installable artifact.")


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
            build_electron(source, output, args.target)
        else:
            raise RuntimeError(f"No supported {args.target} build adapter detected. Add a reviewed adapter for this project.")
        artifacts = [p for p in output.rglob("*") if p.is_file() and p.suffix.lower() in KNOWN_SUFFIXES]
        if not artifacts:
            raise RuntimeError("Build finished without a recognized installable artifact.")
        validated = []
        for path in artifacts:
            try:
                details = validate_artifact(path, require_apk_signature=False)
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
