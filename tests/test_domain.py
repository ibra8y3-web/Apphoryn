import tempfile
import unittest
from pathlib import Path

from app.domain import (
    canonical_repository_url, classify_artifact, license_allows_redistribution,
    repository_slug, safe_filename, sha256_file,
)


class DomainTests(unittest.TestCase):
    def test_canonical_github_repository_url(self):
        self.assertEqual(canonical_repository_url("https://github.com/Example/Tool.git"), "https://github.com/Example/Tool")

    def test_rejects_non_github_or_nested_urls(self):
        for value in ("http://github.com/a/b", "https://evil.example/a/b", "https://github.com/a/b/tree/main", "https://github.com/a"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                canonical_repository_url(value)

    def test_license_allowlist(self):
        self.assertTrue(license_allows_redistribution("MIT"))
        self.assertTrue(license_allows_redistribution("Apache-2.0"))
        self.assertFalse(license_allows_redistribution(None))
        self.assertFalse(license_allows_redistribution("NOASSERTION"))

    def test_artifact_classification(self):
        self.assertEqual(classify_artifact("release.apk")[0], "android")
        self.assertEqual(classify_artifact("setup.exe")[0], "windows")
        self.assertIsNone(classify_artifact("readme.md"))

    def test_safe_filename_and_slug(self):
        self.assertEqual(safe_filename("../../setup file.exe"), "setup_file.exe")
        self.assertEqual(repository_slug("https://github.com/a/Great-Tool"), "a-great-tool")

    def test_sha256(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "sample.bin"
            target.write_bytes(b"apphoryn")
            self.assertEqual(sha256_file(str(target)), "0e5c3373a38f4e2b995eaae10a9c35cbffbcf57c3a67879801f6cf3be016d460")


if __name__ == "__main__":
    unittest.main()
