import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app


class ApiSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_health_endpoint(self):
        response = self.client.get("/api/v1/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_storefront_is_served(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Apphoryn", response.text)

    def test_catalog_endpoint_returns_real_records_list(self):
        response = self.client.get("/api/v1/catalog/apps")
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.json()["items"], list)

    @patch("app.main.require_admin")
    @patch("app.main.settings")
    @patch("app.main.GitHubReleaseMirror")
    def test_admin_can_dispatch_global_scout(self, mirror_class, settings_mock, require_admin):
        settings_mock.github_token = "test-token"
        settings_mock.github_owner = "owner"
        settings_mock.github_repository = "Apphoryn"
        settings_mock.github_ref = "main"
        mirror = mirror_class.return_value
        response = self.client.post(
            "/api/v1/admin/scout/dispatch",
            headers={"X-Apphoryn-Admin": "admin-token"},
            json={"max_repositories": 500, "max_pages": 8, "skip_seeds": True},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "dispatched")
        mirror.dispatch_scout.assert_called_once_with(500, 8, True)
        require_admin.assert_called_once_with("admin-token")

    def test_global_scout_rejects_out_of_range_scan_limits(self):
        response = self.client.post(
            "/api/v1/admin/scout/dispatch",
            json={"max_repositories": 10001, "max_pages": 8},
        )
        self.assertEqual(response.status_code, 422)

    @patch("app.main.catalog.discover", return_value=[])
    def test_discovery_endpoint_uses_catalog_service(self, discover):
        response = self.client.get("/api/v1/discovery/search", params={"q": "topic:opensource"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"], [])
        discover.assert_called_once()


    @patch("app.main.db.list_apps", return_value=[])
    @patch("app.main.read_mirror_catalog", return_value=[
        {
            "id": "source-only", "slug": "source-only", "name": "Source Only Project",
            "repository_url": "https://github.com/owner/source-only",
            "license_eligible": True, "project_status": "source_available", "artifacts": [],
        },
        {
            "id": "ready-app", "slug": "ready-app", "name": "Ready App",
            "repository_url": "https://github.com/owner/ready-app",
            "license_eligible": True, "artifacts": [{"filename": "ready.exe"}],
        },
        {
            "id": "blocked", "slug": "blocked", "name": "Blocked",
            "repository_url": "https://github.com/owner/blocked",
            "license_eligible": False, "artifacts": [],
        },
    ])
    def test_projects_endpoint_includes_source_only_projects_without_making_them_store_apps(self, list_apps, read_catalog):
        projects = self.client.get("/api/v1/catalog/projects")
        self.assertEqual(projects.status_code, 200)
        rows = projects.json()["items"]
        self.assertEqual({row["id"] for row in rows}, {"source-only", "ready-app"})
        self.assertEqual(next(row for row in rows if row["id"] == "source-only")["artifacts"], [])

        # The installable app catalogue must still require at least one package.
        ready_app = {
            "id": "ready-app", "slug": "ready-app", "name": "Ready App",
            "repository_url": "https://github.com/owner/ready-app",
            "license_eligible": True, "artifacts": [{"filename": "ready.exe"}],
        }
        with patch("app.main.read_mirror_catalog", return_value=[ready_app]):
            apps = self.client.get("/api/v1/catalog/apps")
        self.assertEqual(apps.status_code, 200)
        self.assertEqual({row["id"] for row in apps.json()["items"]}, {"ready-app"})
        self.assertNotIn("source-only", {row["id"] for row in apps.json()["items"]})


    def test_manual_source_only_import_registers_project_without_empty_release(self):
        project = {
            "id": "project-id", "slug": "owner-source", "name": "Source Project",
            "repository_url": "https://github.com/owner/source",
            "license_spdx": "MIT", "license_eligible": True, "artifacts": [],
        }
        with (
            patch("app.main.require_admin"),
            patch("app.main.settings") as settings_mock,
            patch("app.main.catalog.import_repository", return_value={"app": project, "artifacts": []}),
            patch("app.main.GitHubReleaseMirror") as mirror_class,
        ):
            settings_mock.github_token = "test-token"
            settings_mock.github_owner = "owner"
            settings_mock.github_repository = "Apphoryn"
            settings_mock.github_ref = "main"
            mirror_class.return_value.register_project.return_value = {
                "id": "project-id", "slug": "owner-source", "project_status": "source_available",
            }
            response = self.client.post(
                "/api/v1/admin/import",
                headers={"X-Apphoryn-Admin": "admin-token"},
                json={"repository_url": project["repository_url"], "mirror_release_assets": True},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "source_project_registered")
        mirror_class.return_value.register_project.assert_called_once_with(project)
        mirror_class.return_value.publish_app.assert_not_called()


    def test_store_merges_renamed_repository_by_stable_github_id(self):
        database_record = {
            "id": "local-old-id", "slug": "old-name", "name": "Old Name",
            "repository_url": "https://github.com/old-owner/tool",
            "github_repo_id": "987654321", "license_eligible": True,
            "artifacts": [{"id": "sha-old", "sha256": "sha-old", "filename": "tool-1.exe"}],
        }
        mirror_record = {
            "id": "stable-store-id", "slug": "new-name", "name": "New Name",
            "repository_url": "https://github.com/new-owner/tool",
            "github_repo_id": "987654321", "license_eligible": True,
            "artifacts": [{"id": "sha-new", "sha256": "sha-new", "filename": "tool-2.exe"}],
        }
        with (
            patch("app.main.db.list_apps", return_value=[database_record]),
            patch("app.main.read_mirror_catalog", return_value=[mirror_record]),
        ):
            response = self.client.get("/api/v1/catalog/apps")
        self.assertEqual(response.status_code, 200)
        items = response.json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["repository_url"], mirror_record["repository_url"])
        self.assertEqual({row["sha256"] for row in items[0]["artifacts"]}, {"sha-old", "sha-new"})


    def test_download_streams_apphoryn_release_file_without_redirect(self):
        package_bytes = b"APPHORYN-PACKAGE"

        class FakeResponse:
            status = 200
            headers = {
                "Content-Length": str(len(package_bytes)),
                "Accept-Ranges": "bytes",
                "ETag": '"package-etag"',
            }

            def __init__(self):
                self.sent = False
                self.closed = False

            def read(self, size):
                if self.sent:
                    return b""
                self.sent = True
                return package_bytes

            def close(self):
                self.closed = True

        class FakeOpener:
            def __init__(self):
                self.response = FakeResponse()
                self.request = None

            def open(self, request, timeout):
                self.request = request
                return self.response

        opener = FakeOpener()
        settings_mock = type("Settings", (), {
            "github_owner": "ibra8y3-web",
            "github_repository": "Apphoryn",
            "max_artifact_bytes": 1024 * 1024,
        })()
        mirrored = {
            "id": "a" * 64, "filename": "Example-Setup.exe",
            "download_url": "https://github.com/ibra8y3-web/Apphoryn/releases/download/v1/Example-Setup.exe",
        }
        with (
            patch("app.main.db.get_artifact", return_value=None),
            patch("app.main.find_mirrored_artifact", return_value=mirrored),
            patch("app.main.settings", settings_mock),
            patch("app.main.urllib.request.build_opener", return_value=opener),
        ):
            response = self.client.get("/api/v1/downloads/" + "a" * 64)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, package_bytes)
        self.assertIn("Example-Setup.exe", response.headers["content-disposition"])
        self.assertEqual(opener.request.full_url, mirrored["download_url"])
        self.assertTrue(opener.response.closed)

    def test_download_rejects_upstream_project_release_urls(self):
        opener = object()
        settings_mock = type("Settings", (), {
            "github_owner": "ibra8y3-web",
            "github_repository": "Apphoryn",
            "max_artifact_bytes": 1024 * 1024,
        })()
        mirrored = {
            "id": "b" * 64, "filename": "external.exe",
            "download_url": "https://github.com/original-owner/original-project/releases/download/v1/external.exe",
        }
        with (
            patch("app.main.db.get_artifact", return_value=None),
            patch("app.main.find_mirrored_artifact", return_value=mirrored),
            patch("app.main.settings", settings_mock),
            patch("app.main.urllib.request.build_opener", return_value=opener) as build_opener,
        ):
            response = self.client.get("/api/v1/downloads/" + "b" * 64)

        self.assertEqual(response.status_code, 404)
        build_opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
