import unittest
from pathlib import Path

from scripts.harvest_and_import import github_target_parts, load_seed_repositories, should_dispatch_build_fallback


class HarvestTests(unittest.TestCase):
    def test_actions_repository_is_split_for_build_workflow_configuration(self):
        self.assertEqual(github_target_parts("ibra8y3-web/Apphoryn"), ("ibra8y3-web", "Apphoryn"))
        self.assertIsNone(github_target_parts("Apphoryn"))

    def test_source_build_fallback_runs_when_release_has_no_verified_installer(self):
        # A Release can exist but contain only source archives and checksums.
        self.assertTrue(should_dispatch_build_fallback([], ["android", "windows"]))
        self.assertFalse(should_dispatch_build_fallback([], []))
        self.assertFalse(should_dispatch_build_fallback(
            [{"platform": "android", "validation_status": "validated"}],
            ["android", "windows"],
        ))

    def test_initial_seed_list_contains_real_github_repositories(self):
        seeds = load_seed_repositories(Path(__file__).resolve().parents[1] / "catalog-seed-repositories.json")
        self.assertGreaterEqual(len(seeds), 3)
        self.assertTrue(all(item["repository"].startswith("https://github.com/") for item in seeds))
        self.assertEqual(len({item["repository"] for item in seeds}), len(seeds))


if __name__ == "__main__":
    unittest.main()
