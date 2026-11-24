import unittest

from app import main


class ApphorynOwnedReleaseDownloadTests(unittest.TestCase):
    def test_accepts_only_https_release_assets_owned_by_configured_apphoryn_repo(self):
        self.assertTrue(main._is_apphoryn_release_url(
            "https://github.com/ibra8y3-web/Apphoryn/releases/download/v1/app.apk",
            "ibra8y3-web", "Apphoryn"
        ))
        self.assertFalse(main._is_apphoryn_release_url(
            "https://github.com/other/vendor/releases/download/v1/app.apk",
            "ibra8y3-web", "Apphoryn"
        ))
        self.assertFalse(main._is_apphoryn_release_url(
            "http://github.com/ibra8y3-web/Apphoryn/releases/download/v1/app.apk",
            "ibra8y3-web", "Apphoryn"
        ))
        self.assertFalse(main._is_apphoryn_release_url(
            "https://github.com/ibra8y3-web/Apphoryn/releases/download/v1/app.apk?token=secret",
            "ibra8y3-web", "Apphoryn"
        ))
        self.assertFalse(main._is_apphoryn_release_url(
            "https://user:password@github.com/ibra8y3-web/Apphoryn/releases/download/v1/app.apk",
            "ibra8y3-web", "Apphoryn"
        ))

    def test_rejects_when_owned_repository_is_not_configured(self):
        url = "https://github.com/ibra8y3-web/Apphoryn/releases/download/v1/app.apk"
        self.assertFalse(main._is_apphoryn_release_url(url, "", ""))
        self.assertFalse(main._is_apphoryn_release_url(url, "ibra8y3-web", ""))


if __name__ == "__main__":
    unittest.main()
