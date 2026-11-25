import unittest
from unittest.mock import patch

from app.resource_manager import _local_base_url, resource_status
from app.ai_router import ProviderError


class ResourceManagerTests(unittest.TestCase):
    def test_local_model_endpoint_allowlist_blocks_public_hosts(self):
        self.assertEqual(_local_base_url("http://ollama:11434"), "http://ollama:11434")
        self.assertEqual(_local_base_url("http://127.0.0.1:11434"), "http://127.0.0.1:11434")
        with self.assertRaises(ProviderError):
            _local_base_url("https://model.example.com")
        with self.assertRaises(ProviderError):
            _local_base_url("http://user:pass@localhost:11434")

    def test_compute_and_database_status_never_claims_unconfigured_remote_service_online(self):
        with patch.dict("os.environ", {
            "APPHORYN_GITHUB_COMPUTE_ENABLED": "false",
            "APPHORYN_ANALYTICS_POSTGRES_DSN": "",
            "CLOUDFLARE_ACCOUNT_ID": "",
            "CLOUDFLARE_D1_DATABASE_ID": "",
            "CLOUDFLARE_API_TOKEN": "",
            "OLLAMA_BASE_URL": "",
            "VLLM_BASE_URL": "",
        }, clear=False):
            status = resource_status()
        github = next(item for item in status["compute"] if item["id"] == "github_actions")
        self.assertFalse(github["enabled"])
        db = status["databases"][0]
        self.assertEqual(db["provider"], "local_sqlite")
        self.assertFalse(db["configured"])
        self.assertTrue(status["offline_mode"]["local_queue"])


if __name__ == "__main__":
    unittest.main()
