import unittest

from scripts.build_with_recovery import RECOVERY_ACTIONS


class BuildRecoveryPolicyTests(unittest.TestCase):
    def test_each_target_has_only_known_safe_recovery_actions(self):
        self.assertEqual(
            RECOVERY_ACTIONS["android"],
            {"retry_same", "gradle_refresh_dependencies", "none"},
        )
        for target in ("windows", "linux", "macos"):
            self.assertEqual(
                RECOVERY_ACTIONS[target],
                {"retry_same", "npm_reinstall", "dotnet_restore", "go_mod_download", "none"},
            )

    def test_no_platform_can_select_an_arbitrary_shell_command(self):
        for actions in RECOVERY_ACTIONS.values():
            self.assertNotIn("run_shell", actions)
            self.assertNotIn("edit_source", actions)
            self.assertNotIn("publish_without_validation", actions)


if __name__ == "__main__":
    unittest.main()
