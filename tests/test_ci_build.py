import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.ci_build import (
    command_for_runner,
    install_android_javascript_dependencies,
    remove_upstream_android_signing_gate,
)


class BuildRunnerTests(unittest.TestCase):
    def test_windows_npm_commands_use_cmd_exe_and_npm_cmd(self):
        resolved = command_for_runner(
            ["npm", "ci", "--no-audit"],
            is_windows=True,
            which=lambda name: r"C:\\Program Files\\nodejs\\npm.cmd" if name == "npm.cmd" else None,
        )
        self.assertEqual(resolved[:3], ["cmd.exe", "/d", "/s"])
        self.assertIn("npm.cmd", resolved[4])
        self.assertIn("ci", resolved[4])

    def test_non_windows_commands_are_unchanged(self):
        command = ["npm", "ci"]
        self.assertIs(command_for_runner(command, is_windows=False), command)

    def test_react_native_android_installs_nearest_locked_package_before_gradle(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)
            project = source / "apps" / "mobile" / "android"
            project.mkdir(parents=True)
            (project / "settings.gradle").write_text(
                'pluginManagement { includeBuild("../node_modules/@react-native/gradle-plugin") }\n'
                'plugins { id("com.facebook.react.settings") }\n',
                encoding="utf-8",
            )
            package_root = project.parent
            (package_root / "package.json").write_text(
                '{"dependencies":{"react-native":"0.82.1"}}', encoding="utf-8"
            )
            (package_root / "package-lock.json").write_text('{}', encoding="utf-8")

            with patch("scripts.ci_build.run") as run_mock:
                installed = install_android_javascript_dependencies(source, project)

            self.assertTrue(installed)
            run_mock.assert_called_once_with(
                ["npm", "ci", "--no-audit", "--no-fund"],
                cwd=package_root,
                timeout=3600,
            )

    def test_android_without_react_native_settings_skips_javascript_install(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)
            project = source / "android"
            project.mkdir(parents=True)
            (project / "settings.gradle").write_text("rootProject.name = 'Native'", encoding="utf-8")
            with patch("scripts.ci_build.run") as run_mock:
                self.assertFalse(install_android_javascript_dependencies(source, project))
            run_mock.assert_not_called()

    def test_android_signing_guard_is_removed_only_from_disposable_clone(self):
        guard = '''tasks.matching { it.name == "packageRelease" || it.name == "packageReleaseBundle" }.configureEach {
    val missing = listOf("storeFile", "storePassword", "keyAlias", "keyPassword") - releaseSigning.keys
    doFirst {
        if (missing.isNotEmpty()) {
            throw GradleException(
                "Release signing is not configured (missing: ${missing.joinToString()}). " +
                    "Fill android/keystore.properties or set the CAIMACK_* variables, see README."
            )
        }
    }
}
'''
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project = root / "android" / "app"
            project.mkdir(parents=True)
            gradle = project / "build.gradle.kts"
            gradle.write_text("android { signingConfig = null }\n" + guard, encoding="utf-8")

            self.assertEqual(remove_upstream_android_signing_gate(root), 1)
            rewritten = gradle.read_text(encoding="utf-8")
            self.assertNotIn("Release signing is not configured", rewritten)
            self.assertIn("android { signingConfig = null }", rewritten)


if __name__ == "__main__":
    unittest.main()
