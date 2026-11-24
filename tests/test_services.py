import base64
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.database import Database, utc_now
from app.services import BuildService, CatalogService, store_uploaded_artifact
from app.github_client import GitHubError
import app.services as services_module


def pe_fixture_bytes() -> bytes:
    data = bytearray(1024)
    pe_offset = 128
    data[:2] = b"MZ"
    data[0x3C:0x40] = pe_offset.to_bytes(4, "little")
    data[pe_offset:pe_offset + 4] = bytes((0x50, 0x45, 0, 0))
    data[pe_offset + 4:pe_offset + 6] = (0x8664).to_bytes(2, "little")
    data[pe_offset + 6:pe_offset + 8] = (1).to_bytes(2, "little")
    data[pe_offset + 20:pe_offset + 22] = (0xF0).to_bytes(2, "little")
    data[pe_offset + 22:pe_offset + 24] = (0x0002).to_bytes(2, "little")
    optional = pe_offset + 24
    data[optional:optional + 2] = (0x20B).to_bytes(2, "little")
    data[optional + 16:optional + 20] = (0x1000).to_bytes(4, "little")
    section = optional + 0xF0
    data[section:section + 5] = b".text"
    data[section + 8:section + 12] = (0x1000).to_bytes(4, "little")
    data[section + 12:section + 16] = (0x1000).to_bytes(4, "little")
    data[section + 16:section + 20] = (128).to_bytes(4, "little")
    data[section + 20:section + 24] = (512).to_bytes(4, "little")
    data[section + 36:section + 40] = (0x60000020).to_bytes(4, "little")
    data[512:640] = bytes((0xC3,)) * 128
    return bytes(data)


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

    def test_transient_license_api_failure_is_not_misclassified_as_missing_license(self):
        github = Mock()
        github.get_repository.return_value = {
            "id": 123, "name": "Tool", "html_url": "https://github.com/owner/tool",
            "license": {"spdx_id": "MIT"}, "archived": False, "default_branch": "main",
        }
        github.get_latest_release.return_value = None
        github.request.side_effect = GitHubError("GitHub API returned HTTP 502: upstream unavailable")
        service = CatalogService(github=github, database=Mock())
        with self.assertRaisesRegex(GitHubError, "HTTP 502"):
            service.import_repository("https://github.com/owner/tool", mirror_release_assets=False)

    def test_real_release_download_keeps_installer_extension_for_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            database = Database(root / "catalog.sqlite3")
            github = Mock()
            github.get_repository.return_value = {
                "id": 987654321, "name": "Tool", "description": "A desktop tool",
                "html_url": "https://github.com/owner/tool", "homepage": None,
                "license": {"spdx_id": "MIT"}, "topics": ["utility"],
                "archived": False, "disabled": False, "default_branch": "main",
                "updated_at": "2026-11-24T00:00:00Z",
            }
            github.get_latest_release.return_value = {
                "tag_name": "v1.0.0",
                "html_url": "https://github.com/owner/tool/releases/tag/v1.0.0",
                "assets": [{
                    "name": "Tool-Setup.exe",
                    "browser_download_url": "https://github.com/owner/tool/releases/download/v1.0.0/Tool-Setup.exe",
                    "digest": None,
                }],
            }

            def github_request(path):
                if path.endswith("/license"):
                    return {"content": base64.b64encode(b"MIT License\nPermission is hereby granted.").decode()}
                if path.endswith("/commits/main"):
                    return {"sha": "cd" * 20}
                return {"content": base64.b64encode(b"# Tool\nA useful desktop tool.").decode()}

            github.request.side_effect = github_request
            payload = pe_fixture_bytes()
            expected = hashlib.sha256(payload).hexdigest()
            original_download = services_module._download_asset
            old_storage = services_module.settings.storage_path
            object.__setattr__(services_module.settings, "storage_path", root / "artifacts")

            def fake_download(url, destination, max_bytes):
                self.assertEqual(destination.suffix.lower(), ".exe")
                destination.write_bytes(payload)
                return len(payload), expected

            services_module._download_asset = fake_download
            try:
                service = CatalogService(github=github, database=database)
                result = service.import_repository("https://github.com/owner/tool", mirror_release_assets=True)
                self.assertEqual(len(result["artifacts"]), 1)
                self.assertEqual(result["artifacts"][0]["sha256"], expected)
                self.assertEqual(result["artifacts"][0]["filename"], "Tool-Setup.exe")
                self.assertTrue(Path(result["artifacts"][0]["storage_path"]).is_file())
            finally:
                services_module._download_asset = original_download
                object.__setattr__(services_module.settings, "storage_path", old_storage)

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
            pe = pe_fixture_bytes()
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


    def test_manual_build_registers_source_project_before_dispatch(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Database(Path(folder) / "build.sqlite3")
            app_record = {
                "id": "source-project", "slug": "owner-source", "name": "Source Project",
                "summary": "Buildable source", "description": "Source project description",
                "repository_url": "https://github.com/owner/source", "owner": "owner",
                "github_repo_id": "123", "license_spdx": "MIT", "license_eligible": True,
                "category": "developer", "topics": [], "created_at": utc_now(), "updated_at": utc_now(),
            }
            database.upsert_app(app_record)
            catalog = Mock()
            catalog.import_repository.return_value = {"app": app_record, "artifacts": []}
            old_values = {
                "github_owner": services_module.settings.github_owner,
                "github_repository": services_module.settings.github_repository,
                "github_token": services_module.settings.github_token,
                "github_ref": services_module.settings.github_ref,
            }
            try:
                object.__setattr__(services_module.settings, "github_owner", "owner")
                object.__setattr__(services_module.settings, "github_repository", "Apphoryn")
                object.__setattr__(services_module.settings, "github_token", "test-token")
                object.__setattr__(services_module.settings, "github_ref", "main")
                with patch("app.services.GitHubReleaseMirror") as mirror_class:
                    mirror = mirror_class.return_value
                    service = BuildService(catalog=catalog, github=Mock(), database=database)
                    job = service.dispatch("https://github.com/owner/source", "main", ["linux"])
                    self.assertEqual(job["status"], "queued")
                    mirror.register_project.assert_called_once_with(app_record)
                    mirror.publish_app.assert_not_called()
                    mirror.dispatch_build.assert_called_once_with(
                        "https://github.com/owner/source", "main", ["linux"], "owner-source"
                    )
                    self.assertEqual(database.list_jobs()[0]["app_id"], "source-project")
            finally:
                for key, value in old_values.items():
                    object.__setattr__(services_module.settings, key, value)


if __name__ == "__main__":
    unittest.main()
