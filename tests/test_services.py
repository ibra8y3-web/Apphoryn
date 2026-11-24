import base64
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.database import Database, utc_now
from app.services import CatalogService, store_uploaded_artifact
import app.services as services_module


class CatalogServiceTests(unittest.TestCase):
    def test_discovery_marks_license_eligibility_without_importing(self):
        github = Mock()
        github.search_repositories.return_value = {"items": [
            {"name":"Good","full_name":"owner/good","html_url":"https://github.com/owner/good","license":{"spdx_id":"MIT"},"topics":["android"],"stargazers_count":12,"updated_at":"2026-01-01T00:00:00Z","default_branch":"main"},
            {"name":"Unknown","full_name":"owner/unknown","html_url":"https://github.com/owner/unknown","license":None,"topics":[],"stargazers_count":0},
        ]}
        service = CatalogService(github=github, database=Mock())
        items = service.discover("topic:opensource", 10)
        self.assertTrue(items[0]["license_eligible"])
        self.assertFalse(items[1]["license_eligible"])
        self.assertEqual(items[0]["default_branch"], "main")
        github.get_repository.assert_not_called()

    def test_import_creates_a_real_catalog_record_only_after_license_check(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Database(Path(folder) / "catalog.sqlite3")
            github = Mock()
            github.get_repository.return_value = {
                "name": "UsefulTool", "description": "A file utility", "html_url": "https://github.com/owner/usefultool",
                "homepage": None, "license": {"spdx_id": "MIT"}, "topics": ["file-manager"],
                "archived": False, "default_branch": "main",
            }
            github.get_latest_release.return_value = None
            github.request.return_value = {"content": base64.b64encode(b"# Useful Tool\nA small file utility.").decode()}
            service = CatalogService(github=github, database=database)
            result = service.import_repository("https://github.com/owner/usefultool", mirror_release_assets=False)
            self.assertEqual(result["app"]["name"], "UsefulTool")
            self.assertTrue(result["app"]["license_eligible"])
            self.assertEqual(result["artifacts"], [])
            self.assertEqual(len(database.list_apps()), 1)

    def test_missing_license_blocks_import(self):
        github = Mock()
        github.get_repository.return_value = {"name": "NoLicense", "license": None, "archived": False}
        service = CatalogService(github=github, database=Mock())
        with self.assertRaises(ValueError):
            service.import_repository("https://github.com/owner/no-license", mirror_release_assets=False)
        github.get_latest_release.assert_not_called()

    def test_uploaded_build_artifact_is_hashed_and_stored(self):
        with tempfile.TemporaryDirectory() as folder:
            folder_path = Path(folder)
            database = Database(folder_path / "catalog.sqlite3")
            now = utc_now()
            database.upsert_app({
                "id": "app-1", "slug": "owner-tool", "name": "Tool", "summary": "Tool",
                "description": "Tool", "repository_url": "https://github.com/owner/tool", "owner": "owner",
                "license_spdx": "MIT", "license_eligible": True, "category": "other", "topics": [],
                "translations": {}, "created_at": now, "updated_at": now,
            })
            incoming = folder_path / "real-package.apk"
            incoming.write_bytes(b"apk-package-binary-test")
            expected = hashlib.sha256(incoming.read_bytes()).hexdigest()
            old_storage = services_module.settings.storage_path
            object.__setattr__(services_module.settings, "storage_path", folder_path / "artifacts")
            try:
                artifact = store_uploaded_artifact(
                    "app-1", "real-package.apk", "android", "1.0.0", "https://github.com/owner/tool", "v1.0.0",
                    incoming, expected, database=database,
                )
                self.assertEqual(artifact["sha256"], expected)
                self.assertTrue(Path(artifact["storage_path"]).is_file())
            finally:
                object.__setattr__(services_module.settings, "storage_path", old_storage)


if __name__ == "__main__":
    unittest.main()
