import tempfile
import unittest
from pathlib import Path

from app.repository_archive import RepositoryArchive, classify_application


class FakeGitHub:
    def __init__(self):
        self.calls = []

    def request(self, path, params=None):
        params = params or {}
        self.calls.append((path, params))
        if path.endswith("/git/trees/main"):
            return {"tree": [
                {"path": "app/src/main/AndroidManifest.xml", "type": "blob"},
                {"path": "app/build.gradle", "type": "blob"},
                {"path": "README.md", "type": "blob"},
            ]}
        if path.endswith("/readme"):
            return {"content": ""}
        if path.endswith("/releases"):
            page = int(params.get("page", "1"))
            if page == 1:
                return [
                    {"tag_name": f"v{n}", "name": f"Version {n}", "html_url": f"https://github.com/acme/sample/releases/tag/v{n}", "assets": []}
                    for n in range(100)
                ]
            if page == 2:
                return [{"tag_name": "v100", "name": "Version 100", "html_url": "https://github.com/acme/sample/releases/tag/v100", "assets": []}]
            return []
        if path.endswith("/tags"):
            return [{"name": "v0.9", "commit": {"sha": "a" * 40, "url": "https://api.github.com/repos/acme/sample/commits/" + "a" * 40}}]
        raise AssertionError(f"Unexpected GitHub API call: {path}")


class RepositoryArchiveTests(unittest.TestCase):
    def test_classifier_recognizes_nested_android_application_manifest(self):
        status, evidence = classify_application(
            {"description": "A mobile app", "topics": ["android-app"]},
            {
                "app/src/main/AndroidManifest.xml",
                "app/build.gradle",
                "README.md",
            },
            "Install this application on Android.",
        )
        self.assertEqual(status, "application")
        self.assertIn("manifest:android", evidence)

    def test_classifier_does_not_archive_generic_library_as_application(self):
        status, evidence = classify_application(
            {"description": "A reusable parsing library", "topics": ["library", "python"]},
            {"pyproject.toml", "src/parser.py", "README.md"},
            "A library for parsing configuration files.",
        )
        self.assertEqual(status, "not_application")
        self.assertTrue(evidence)

    def test_enqueue_is_idempotent_and_updates_changed_upstream_source(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = RepositoryArchive(Path(folder) / "archive.sqlite3")
            first = {
                "id": 123,
                "full_name": "acme/sample",
                "html_url": "https://github.com/acme/sample",
                "name": "sample",
                "description": "Android app",
                "topics": ["android-app"],
                "updated_at": "2026-01-01T00:00:00Z",
            }
            self.assertEqual(archive.enqueue_batch([first])["queued"], 1)
            self.assertEqual(archive.enqueue_batch([first])["queued"], 0)
            changed = {**first, "updated_at": "2026-02-01T00:00:00Z"}
            self.assertEqual(archive.enqueue_batch([changed])["refreshed"], 1)
            self.assertEqual(archive.get_repository("123")["state"], "queued")

    def test_release_and_tag_history_paginates_without_a_fixed_version_ceiling(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = RepositoryArchive(Path(folder) / "archive.sqlite3")
            github = FakeGitHub()
            archive.enqueue_batch([{
                "id": 123,
                "html_url": "https://github.com/acme/sample",
                "name": "sample",
                "description": "Android application",
                "topics": ["android-app"],
                "updated_at": "2026-01-01T00:00:00Z",
                "default_branch": "main",
            }])

            first = archive.process_next(github)
            self.assertEqual(first["classification"], "application")
            release_one = archive.process_next(github)
            self.assertEqual(release_one["versions"], 100)
            release_two = archive.process_next(github)
            self.assertEqual(release_two["versions"], 1)
            tags = archive.process_next(github)
            self.assertEqual(tags["step"], "tag_page")
            self.assertTrue(tags["complete"])

            versions = archive.list_versions("123", limit=100)
            self.assertEqual(len(versions), 100)
            all_versions = archive.list_versions("123", limit=100, offset=100)
            self.assertEqual(len(all_versions), 2)
            self.assertEqual(
                archive.stats()["versions_total"],
                102,
            )
            self.assertTrue(any(
                path.endswith("/releases") and params.get("page") == "2"
                for path, params in github.calls
            ))

    def test_invalid_non_github_sources_are_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = RepositoryArchive(Path(folder) / "archive.sqlite3")
            result = archive.enqueue_batch([{
                "id": 7,
                "html_url": "https://example.com/acme/sample",
                "name": "not github",
            }])
            self.assertEqual(result["queued"], 0)
            self.assertEqual(result["ignored"], 1)


if __name__ == "__main__":
    unittest.main()
