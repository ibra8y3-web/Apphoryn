import unittest
from unittest.mock import patch

from app import main


class ApphorynOwnedReleaseDownloadTests(unittest.TestCase):
    def test_accepts_only_https_release_assets_owned_by_configured_apphoryn_repo(self):
        with patch.object(main.settings, "github_owner", "ibra8y3-web"), patch.object(
            main.settings, "github_repository", "Apphoryn"
        ):
            self.assertTrue(main._is_apphoryn_release_url(
                "https://github.com/ibra8y3-web/Apphoryn/releases/download/v1/app.apk"
            ))
            self.assertFalse(main._is_apphoryn_release_url(
                "https://github.com/other/vendor/releases/download/v1/app.apk"
            ))
            self.assertFalse(main._is_apphoryn_release_url(
                "http://github.com/ibra8y3-web/Apphoryn/releases/download/v1/app.apk"
            ))
            self.assertFalse(main._is_apphoryn_release_url(
                "https://github.com/ibra8y3-web/Apphoryn/releases/download/v1/app.apk?token=secret"
            ))
            self.assertFalse(main._is_apphoryn_release_url(
                "https://user:password@github.com/ibra8y3-web/Apphoryn/releases/download/v1/app.apk"
            ))

    def test_rejects_when_owned_repository_is_not_configured(self):
        with patch.object(main.settings, "github_owner", ""), patch.object(
            main.settings, "github_repository", ""
        ):
            self.assertFalse(main._is_apphoryn_release_url(
                "https://github.com/ibra8y3-web/Apphoryn/releases/download/v1/app.apk"
            ))


if __name__ == "__main__":
    unittest.main()
