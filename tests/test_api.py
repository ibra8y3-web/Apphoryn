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
    def test_projects_endpoint_includes_source_only_projects_without_making_them_store_apps(self, read_catalog):
        projects = self.client.get("/api/v1/catalog/projects")
        self.assertEqual(projects.status_code, 200)
        rows = projects.json()["items"]
        self.assertEqual({row["id"] for row in rows}, {"source-only", "ready-app"})
        self.assertEqual(next(row for row in rows if row["id"] == "source-only")["artifacts"], [])

        # The installable app catalogue must still require at least one package.
        with patch("app.main.db.list_apps", return_value=[]):
            apps = self.client.get("/api/v1/catalog/apps")
        self.assertEqual(apps.status_code, 200)
        self.assertEqual(apps.json()["items"], [])


if __name__ == "__main__":
    unittest.main()
