import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.ci_build import command_for_runner, remove_upstream_android_signing_gate


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
