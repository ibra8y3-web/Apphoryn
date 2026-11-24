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
            def github_request(path):
                if path.endswith("/license"):
                    return {"content": base64.b64encode(b"MIT License\nPermission is hereby granted.").decode()}
                if path.endswith("/commits/main"):
                    return {"sha": "ab" * 20}
                return {"content": base64.b64encode(b"# Useful Tool\nA small file utility.").decode()}
            github.request.side_effect = github_request
            service = CatalogService(github=github, database=database)
            result = service.import_repository("https://github.com/owner/usefultool", mirror_release_assets=False)
            self.assertEqual(result["app"]["name"], "UsefulTool")
            self.assertTrue(result["app"]["license_eligible"])
            self.assertEqual(result["app"]["source_commit_sha"], "ab" * 20)
            self.assertEqual(result["artifacts"], [])
            self.assertEqual(len(database.list_apps()), 1)

    def test_project_icon_prefers_a_real_source_icon_over_owner_avatar(self):
        github = Mock()
        github.request.return_value = {"tree": [
            {"path": "README.md", "type": "blob", "size": 2000},
            {"path": "docs/screenshots/icon.png", "type": "blob", "size": 40000},
            {"path": "assets/app-icon.png", "type": "blob", "size": 12000},
            {"path": "assets/banner.png", "type": "blob", "size": 90000},
        ]}
        service = CatalogService(github=github, database=Mock())
        icon = service._resolve_project_icon(
            "owner", "app", "main", "https://avatars.githubusercontent.com/u/12345?v=4"
        )
        self.assertEqual(
            icon,
            "https://raw.githubusercontent.com/owner/app/main/assets/app-icon.png",
        )

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
            incoming = folder_path / "minimal-pe-fixture.exe"
            # This validates PE header parsing only; it is not claimed to be runnable software.
            pe = bytearray(256)
            pe[:2] = b"MZ"
            pe[0x3C:0x40] = (128).to_bytes(4, "little")
            pe[128:132] = b"PE\0\0"
            incoming.write_bytes(pe)
            expected = hashlib.sha256(incoming.read_bytes()).hexdigest()
            old_storage = services_module.settings.storage_path
            object.__setattr__(services_module.settings, "storage_path", folder_path / "artifacts")
            try:
                artifact = store_uploaded_artifact(
                    "app-1", "minimal-pe-fixture.exe", "windows", "1.0.0", "https://github.com/owner/tool", "v1.0.0",
                    incoming, expected, database=database,
                )
                self.assertEqual(artifact["sha256"], expected)
                self.assertEqual(artifact["validation_status"], "validated")
                self.assertTrue(Path(artifact["storage_path"]).is_file())
            finally:
                object.__setattr__(services_module.settings, "storage_path", old_storage)

    def test_fake_apk_with_matching_sha256_is_rejected(self):
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
            incoming = folder_path / "fake.apk"
            incoming.write_bytes(b"not an APK even though the extension says so" * 4)
            expected = hashlib.sha256(incoming.read_bytes()).hexdigest()
            old_storage = services_module.settings.storage_path
            object.__setattr__(services_module.settings, "storage_path", folder_path / "artifacts")
            try:
                with self.assertRaisesRegex(ValueError, "Artifact validation failed"):
                    store_uploaded_artifact(
                        "app-1", "fake.apk", "android", "1.0.0", "https://github.com/owner/tool", "v1.0.0",
                        incoming, expected, database=database,
                    )
                self.assertEqual(database.list_artifacts("app-1"), [])
            finally:
                object.__setattr__(services_module.settings, "storage_path", old_storage)


if __name__ == "__main__":
    unittest.main()
