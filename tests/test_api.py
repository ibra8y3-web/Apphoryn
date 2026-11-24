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

    @patch("app.main.catalog.discover", return_value=[])
    def test_discovery_endpoint_uses_catalog_service(self, discover):
        response = self.client.get("/api/v1/discovery/search", params={"q": "topic:opensource"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"], [])
        discover.assert_called_once()


if __name__ == "__main__":
    unittest.main()
