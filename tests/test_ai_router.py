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

    def test_comet_free_only_accepts_only_catalog_confirmed_zero_price_models(self):
        env = {
            "GEMINI_API_KEY": "",
            "GROQ_API_KEY": "",
            "OPENROUTER_API_KEY": "",
            "COMET_API_KEY": "comet-test-key",
            "CLOUDFLARE_API_TOKEN": "",
            "CLOUDFLARE_ACCOUNT_ID": "",
            "APPHORYN_COMET_FREE_MODEL_IDS": "",
        }
        payload = {"data": [
            {"id": "vendor/free", "pricing": {"prompt": "0", "completion": "0"}, "context_length": 8192},
            {"id": "vendor/paid", "pricing": {"prompt": "0.10", "completion": "0.20"}, "context_length": 8192},
            {"id": "vendor/unknown-price", "context_length": 8192},
        ]}
        with patch.dict(os.environ, env, clear=False), \
             patch.object(ai_router, "_FREE_ONLY", True), \
             patch.object(ai_router, "_ALLOW_CREDIT", False), \
             patch.object(ai_router, "_request", return_value=payload) as request:
            models, providers = ai_router.discover_models(force=True)
        self.assertEqual([model.model for model in models], ["vendor/free"])
        request.assert_called_once()
        self.assertIn("/api/models", request.call_args.args[0])
        provider = next(item for item in providers if item["provider"] == "comet")
        self.assertTrue(provider["enabled"])
        self.assertEqual(provider["models_discovered"], 1)

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



    def test_xai_grok_is_blocked_by_default_even_when_key_exists(self):
        env = {
            "GEMINI_API_KEY": "", "GROQ_API_KEY": "", "XAI_API_KEY": "xai-test-key",
            "OPENROUTER_API_KEY": "", "COMET_API_KEY": "", "CLOUDFLARE_API_TOKEN": "",
        }
        with patch.dict(os.environ, env, clear=False), \\
             patch.object(ai_router, "_FREE_ONLY", True), \\
             patch.object(ai_router, "_ALLOW_PAID", False), \\
             patch.object(ai_router, "_ENABLE_XAI", True), \\
             patch.object(ai_router, "_request") as request:
            models, providers = ai_router.discover_models(force=True)
        self.assertFalse(any(model.provider == "xai" for model in models))
        status = next(item for item in providers if item["provider"] == "xai")
        self.assertTrue(status["configured"])
        self.assertFalse(status["enabled"])
        self.assertFalse(request.called)

    def test_xai_grok_model_discovery_requires_explicit_paid_authorization(self):
        env = {
            "GEMINI_API_KEY": "", "GROQ_API_KEY": "", "XAI_API_KEY": "xai-test-key",
            "OPENROUTER_API_KEY": "", "COMET_API_KEY": "", "CLOUDFLARE_API_TOKEN": "",
        }
        with patch.dict(os.environ, env, clear=False), \\
             patch.object(ai_router, "_FREE_ONLY", False), \\
             patch.object(ai_router, "_ALLOW_PAID", True), \\
             patch.object(ai_router, "_ENABLE_XAI", True), \\
             patch.object(ai_router, "_request", return_value={"data": [{"id": "grok-test"}]}):
            models, providers = ai_router.discover_models(force=True)
        self.assertEqual([model.model for model in models if model.provider == "xai"], ["grok-test"])
        status = next(item for item in providers if item["provider"] == "xai")
        self.assertTrue(status["enabled"])
\n\nif __name__ == "__main__":
    unittest.main()
