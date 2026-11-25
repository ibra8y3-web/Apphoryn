import base64
import json
import unittest

from app.build_support import detect_supported_build_targets, dotnet_project_targets


class FakeGitHub:
    def __init__(self, paths, package_json=None, source_files=None):
        self.paths = paths
        self.package_json = package_json or {}
        self.source_files = source_files or {}
        self.calls = []

    def request(self, path):
        self.calls.append(path)
        if "/git/trees/" in path:
            return {"tree": [{"path": item, "type": "blob"} for item in self.paths]}
        if "/contents/" in path:
            package_path = path.split("/contents/", 1)[1].split("?", 1)[0]
            package = self.package_json.get(package_path)
            if package is not None:
                content = json.dumps(package).encode()
            elif package_path in self.source_files:
                content = self.source_files[package_path].encode()
            else:
                return {}
            return {"encoding": "base64", "content": base64.b64encode(content).decode()}
        raise AssertionError(f"Unexpected GitHub API path: {path}")


class BuildSupportTests(unittest.TestCase):
    def test_detects_android_windows_only_dotnet_and_go_targets_from_real_build_files(self):
        github = FakeGitHub([
            "android/gradlew",
            "android/build.gradle.kts",
            "android/app/src/main/AndroidManifest.xml",
            "desktop/App.csproj",
            "tools/go.mod",
        ], source_files={
            "desktop/App.csproj": (
                "<Project Sdk='Microsoft.NET.Sdk'><PropertyGroup>"
                "<OutputType>WinExe</OutputType><TargetFramework>net10.0-windows</TargetFramework>"
                "</PropertyGroup></Project>"
            )
        })
        result = detect_supported_build_targets("owner", "repo", "main", github)
        self.assertEqual(result["android"], "Gradle wrapper, Android manifest and Gradle project detected.")
        self.assertEqual(result["windows"], "Executable .NET target detected in desktop/App.csproj.")
        self.assertNotIn("linux", result)
        self.assertNotIn("macos", result)
        self.assertFalse(any("electron" in value.lower() for value in result.values()))

    def test_dotnet_detector_skips_libraries_and_test_projects(self):
        self.assertEqual(dotnet_project_targets(
            "src/Library.csproj",
            "<Project><PropertyGroup><TargetFramework>net10.0</TargetFramework>"
            "<OutputType>Library</OutputType></PropertyGroup></Project>",
        ), set())
        self.assertEqual(dotnet_project_targets(
            "tests/App.Tests.csproj",
            "<Project><PropertyGroup><TargetFramework>net10.0</TargetFramework>"
            "<OutputType>Exe</OutputType><IsTestProject>true</IsTestProject></PropertyGroup></Project>",
        ), set())

    def test_dotnet_detector_accepts_portable_executable_for_all_desktop_targets(self):
        self.assertEqual(dotnet_project_targets(
            "src/PortableTool.csproj",
            "<Project><PropertyGroup><TargetFramework>net10.0</TargetFramework>"
            "<OutputType>Exe</OutputType></PropertyGroup></Project>",
        ), {"windows", "linux", "macos"})

    def test_detects_electron_only_when_a_packaging_script_exists(self):
        github = FakeGitHub(
            ["apps/desktop/package.json", "apps/web/package.json"],
            {"apps/desktop/package.json": {
                "dependencies": {"electron": "^30.0.0"},
                "devDependencies": {"electron-builder": "^25.0.0"},
                "scripts": {"dist": "electron-builder"},
            }},
        )
        result = detect_supported_build_targets("owner", "repo", "v1.0.0", github)
        self.assertEqual(set(result), {"windows", "linux", "macos"})
        self.assertTrue(all("Electron" in reason for reason in result.values()))


    def test_electron_detection_uses_only_real_platform_scripts(self):
        github = FakeGitHub(
            ["desktop/package.json", "frontend/package.json"],
            {
                "desktop/package.json": {
                    "dependencies": {"electron": "^45.0.0"},
                    "devDependencies": {"electron-builder": "^26.0.0"},
                    "scripts": {
                        "build": "node ../frontend/node_modules/vite/bin/vite.js build",
                        "dist:linux": "npm run build && electron-builder --linux",
                        "dist:windows": "npm run build && electron-builder --win",
                    },
                },
                "frontend/package.json": {
                    "dependencies": {"react": "^19.0.0"},
                    "scripts": {"build": "vite build"},
                },
            },
        )
        result = detect_supported_build_targets("owner", "repo", "main", github)
        self.assertEqual(set(result), {"linux", "windows"})
        self.assertIn("dist:linux", result["linux"])
        self.assertIn("dist:windows", result["windows"])

    def test_go_module_requires_an_executable_package_main_source(self):
        empty_module = FakeGitHub(["tools/go.mod", "tools/README.md"])
        self.assertEqual(detect_supported_build_targets("owner", "repo", "main", empty_module), {})

        executable = FakeGitHub(
            ["tools/go.mod", "tools/cmd/station/main.go"],
            source_files={"tools/cmd/station/main.go": "package main\nfunc main() {}\n"},
        )
        result = detect_supported_build_targets("owner", "repo", "main", executable)
        self.assertEqual(set(result), {"windows", "linux", "macos"})
        self.assertTrue(all("Go executable" in reason for reason in result.values()))

    def test_does_not_dispatch_for_a_repository_without_supported_build_adapters(self):
        github = FakeGitHub(["README.md", "src/index.js", "LICENSE"])
        self.assertEqual(detect_supported_build_targets("owner", "repo", "main", github), {})


if __name__ == "__main__":
    unittest.main()
