import base64
import json
import unittest

from app.build_support import detect_supported_build_targets


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
    def test_detects_android_dotnet_and_go_targets_from_real_build_files(self):
        github = FakeGitHub([
            "android/gradlew",
            "desktop/App.csproj",
            "tools/go.mod",
        ])
        result = detect_supported_build_targets("owner", "repo", "main", github)
        self.assertEqual(result["android"], "Gradle wrapper detected.")
        self.assertEqual(result["windows"], ".NET project detected.")
        self.assertEqual(result["linux"], ".NET project detected.")
        self.assertEqual(result["macos"], ".NET project detected.")
        self.assertFalse(any("electron" in value.lower() for value in result.values()))

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
