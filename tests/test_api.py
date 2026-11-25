import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app


class ApiSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_github_actions_internal_endpoints_reject_missing_identity(self):
        response = self.client.get("/api/v1/internal/github-actions/ai-status")
        self.assertEqual(response.status_code, 401)
        response = self.client.post(
            "/api/v1/internal/github-actions/catalog-description",
            json={"name": "Example", "owner": "example"},
        )
        self.assertEqual(response.status_code, 401)

    @patch("app.main.ai_router.public_model_catalog", return_value={"models": [], "providers": []})
    @patch("app.main.verify_github_actions_oidc", return_value={"run_id": "987654"})
    def test_github_actions_internal_status_uses_verified_identity(self, verify, status):
        response = self.client.get(
            "/api/v1/internal/github-actions/ai-status",
            headers={"Authorization": "Bearer test-token"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["providers"], [])
        verify.assert_called_once_with("test-token")
        status.assert_called_once_with(force=True)

    @patch("app.main.DescriptionGenerator.generate", return_value={
        "summary": "An AI-generated application.",
        "description": "A description from the Railway model router.",
        "generated_by_ai": "true",
        "ai_provider": "openrouter",
    })
    @patch("app.main.verify_github_actions_oidc", return_value={"run_id": "987655"})
    def test_github_actions_internal_description_calls_railway_generator(self, verify, generate):
        response = self.client.post(
            "/api/v1/internal/github-actions/catalog-description",
            headers={"Authorization": "Bearer test-token"},
            json={
                "name": "Example",
                "owner": "example",
                "repository_description": "A real utility.",
                "topics": ["desktop-app"],
                "readme_excerpt": "Install the application.",
                "category": "utility",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["ai_provider"], "openrouter")
        generate.assert_called_once()
        self.assertFalse(generate.call_args.kwargs["allow_remote"])

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

    @patch("app.main.catalog.discover", return_value=[])
    def test_discovery_endpoint_uses_catalog_service(self, discover):
        response = self.client.get("/api/v1/discovery/search", params={"q": "topic:opensource"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"], [])
        discover.assert_called_once()


if __name__ == "__main__":
    unittest.main()
