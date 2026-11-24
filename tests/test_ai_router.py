import os
import unittest
from unittest.mock import patch

from app import ai_router


class AIRouterTests(unittest.TestCase):
    def setUp(self):
        ai_router._cache = {"until": 0.0, "models": [], "providers": []}
        ai_router._cooldowns.clear()

    def test_openrouter_free_catalog_excludes_paid_models_and_never_exposes_keys(self):
        env = {
            "GEMINI_API_KEY": "",
            "GROQ_API_KEY": "",
            "OPENROUTER_API_KEY": "private-test-key",
            "COMET_API_KEY": "",
            "CLOUDFLARE_API_TOKEN": "",
        }
        payload = {"data": [
            {"id": "vendor/one:free", "context_length": 32768},
            {"id": "vendor/two", "context_length": 65536},
        ]}
        with patch.dict(os.environ, env, clear=False), \
             patch.object(ai_router, "_FREE_ONLY", True), \
             patch.object(ai_router, "_request", return_value=payload):
            result = ai_router.public_model_catalog(force=True)

        ids = [item["model"] for item in result["models"]]
        self.assertIn("vendor/one:free", ids)
        self.assertIn("openrouter/free", ids)
        self.assertNotIn("vendor/two", ids)
        self.assertNotIn("private-test-key", repr(result))
        self.assertNotIn("key", result["models"][0])

    def test_gemini_and_groq_pause_until_free_tier_is_confirmed(self):
        env = {
            "GEMINI_API_KEY": "gemini-test-key",
            "GROQ_API_KEY": "groq-test-key",
            "OPENROUTER_API_KEY": "",
            "COMET_API_KEY": "",
            "CLOUDFLARE_API_TOKEN": "",
            "APPHORYN_AI_CONFIRM_GEMINI_GROQ_FREE_TIER": "false",
        }
        with patch.dict(os.environ, env, clear=False), \
             patch.object(ai_router, "_FREE_ONLY", True), \
             patch.object(ai_router, "_request") as request:
            models, providers = ai_router.discover_models(force=True)
        self.assertEqual(models, [])
        self.assertFalse(request.called)
        by_name = {item["provider"]: item for item in providers}
        self.assertTrue(by_name["gemini"]["configured"])
        self.assertFalse(by_name["gemini"]["enabled"])
        self.assertTrue(by_name["groq"]["configured"])
        self.assertFalse(by_name["groq"]["enabled"])

    def test_gemini_and_groq_can_be_discovered_after_explicit_free_tier_confirmation(self):
        env = {
            "GEMINI_API_KEY": "gemini-test-key",
            "GROQ_API_KEY": "groq-test-key",
            "OPENROUTER_API_KEY": "",
            "COMET_API_KEY": "",
            "CLOUDFLARE_API_TOKEN": "",
            "APPHORYN_AI_CONFIRM_GEMINI_GROQ_FREE_TIER": "true",
        }
        replies = [
            {"models": [{"name": "models/gemini-test", "supportedGenerationMethods": ["generateContent"], "inputTokenLimit": 8192}]},
            {"data": [{"id": "groq-test", "context_window": 4096}]},
        ]
        with patch.dict(os.environ, env, clear=False), \
             patch.object(ai_router, "_FREE_ONLY", True), \
             patch.object(ai_router, "_request", side_effect=replies):
            models, providers = ai_router.discover_models(force=True)
        self.assertEqual({model.provider for model in models}, {"gemini", "groq"})
        self.assertEqual(len(models), 2)

    def test_generation_fails_over_after_rate_limit(self):
        candidates = [
            ai_router.Candidate("openrouter", "first:free", "openai", "https://example.test/v1", "k1"),
            ai_router.Candidate("openrouter", "second:free", "openai", "https://example.test/v1", "k2"),
        ]
        with patch.object(ai_router, "discover_models", return_value=(candidates, [])), \
             patch.object(ai_router, "_openai", side_effect=[
                 ai_router.ProviderError("rate limited", 429),
                 {"summary": "ok"},
             ]):
            result, route = ai_router.generate_json([{"role": "user", "content": "test"}])
        self.assertEqual(result, {"summary": "ok"})
        self.assertEqual(route["model"], "second:free")


if __name__ == "__main__":
    unittest.main()
