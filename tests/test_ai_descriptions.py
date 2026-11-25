import json
import unittest
from unittest.mock import patch

from app.ai_descriptions import DescriptionGenerator, fallback_project_classification


class AiDescriptionTests(unittest.TestCase):
    def test_github_actions_uses_railway_ai_bridge_without_repository_secrets(self):
        generated = {
            "summary": "A verified desktop utility.",
            "description": "A description from Apphoryn's Railway AI service.",
            "generated_by_ai": "true",
            "ai_provider": "openrouter",
            "ai_model": "openrouter/free",
            "project_kind": "application",
            "build_recommended": True,
        }
        with (
            patch.dict("os.environ", {"APPHORYN_AI_SERVICE_URL": "https://apphoryn-production.up.railway.app"}),
            patch("app.ai_descriptions.call_apphoryn_internal_api", return_value=generated) as remote,
            patch("app.ai_descriptions.ai_router.has_configured_ai", return_value=False),
        ):
            result = DescriptionGenerator().generate(
                name="Example Utility",
                owner="example",
                description="A desktop utility.",
                topics=["desktop-app"],
                readme_excerpt="Install the desktop application.",
                category="utility",
            )
        self.assertEqual(result["ai_provider"], "openrouter")
        self.assertEqual(result["generated_by_ai"], "true")
        remote.assert_called_once()
        self.assertEqual(remote.call_args.args[0], "/api/v1/internal/github-actions/catalog-description")

    def test_fallback_marks_reusable_sdk_as_library_and_never_builds_it(self):
        result = fallback_project_classification(
            name="Widget SDK",
            description="A reusable Python library and SDK for the Widget API.",
            topics=["sdk", "python"],
            readme_excerpt="Install the package and import the library into your application.",
        )
        self.assertEqual(result["project_kind"], "library")
        self.assertFalse(result["build_recommended"])
        self.assertEqual(result["classification_source"], "heuristic")

    def test_ai_generates_and_persists_separate_english_and_arabic_text(self):
        english = {
            "summary": "A cross-platform file transfer application.",
            "description": "Transfers files between devices on a local network.",
            "translations": {"en": {"summary": "A cross-platform file transfer application.", "description": "Transfers files between devices on a local network."}},
            "project_kind": "application",
            "build_recommended": True,
            "classification_reason": "The README documents desktop and mobile installers.",
        }
        arabic = {
            "summary": "تطبيق لنقل الملفات بين الأنظمة المختلفة.",
            "description": "ينقل الملفات بين الأجهزة على الشبكة المحلية.",
        }
        with (
            patch("app.ai_descriptions.ai_router.has_configured_ai", return_value=True),
            patch("app.ai_descriptions.ai_router.generate_json", side_effect=[
                (english, {"provider": "openrouter", "model": "example:free"}),
                (arabic, {"provider": "openrouter", "model": "example:free"}),
            ]),
        ):
            result = DescriptionGenerator().generate(
                name="Local Transfer",
                owner="example",
                description="A file transfer app.",
                topics=["file-sharing"],
                readme_excerpt="Install the app on desktop and mobile devices.",
                category="network",
            )
        self.assertEqual(result["translations"]["en"], "A cross-platform file transfer application.")
        self.assertEqual(result["translations"]["ar"], "تطبيق لنقل الملفات بين الأنظمة المختلفة.")
        self.assertEqual(result["description_translations"]["ar"], "ينقل الملفات بين الأجهزة على الشبكة المحلية.")
        self.assertEqual(result["translation_status"], "complete")
        self.assertEqual(result["ai_provider"], "openrouter")

    def test_fallback_upstream_description_is_translated_without_claiming_ai_authorship(self):
        arabic = {
            "summary": "مدير ملفات للمجتمع",
            "description": "تطبيق لإدارة الملفات يطوره المجتمع.",
        }
        with (
            patch("app.ai_descriptions.ai_router.has_configured_ai", return_value=True),
            patch("app.ai_descriptions.ai_router.generate_json", side_effect=[
                RuntimeError("AI description generation unavailable"),
                (arabic, {"provider": "test-provider", "model": "test-model"}),
            ]),
            patch.dict("os.environ", {"APPHORYN_AI_ALLOW_LEGACY_PROVIDER": "false"}),
        ):
            result = DescriptionGenerator().generate(
                name="Community File Manager",
                owner="example",
                description="A community-maintained file manager.",
                topics=["file-manager"],
                readme_excerpt="Install the application to manage local files.",
                category="utility",
            )

        self.assertEqual(result["description"], "A community-maintained file manager.")
        self.assertEqual(result["description_translations"]["ar"], arabic["description"])
        self.assertEqual(result["translations"]["ar"], arabic["summary"])
        self.assertEqual(result["translation_status"], "complete")
        self.assertEqual(result["generated_by_ai"], "false")
        self.assertEqual(result["classification_source"], "heuristic")
        self.assertEqual(result["translation_ai_provider"], "test-provider")

    def test_translation_only_helper_preserves_the_published_source_wording(self):
        from app.ai_descriptions import translate_catalog_description

        arabic = {
            "summary": "وصف عربي موجز",
            "description": "هذا هو الوصف العربي الأصلي للتطبيق.",
        }
        with (
            patch("app.ai_descriptions.ai_router.generate_json", return_value=(
                arabic, {"provider": "test-provider", "model": "test-model"}
            )),
        ):
            result = translate_catalog_description(
                summary="Original published summary",
                description="Original published description, written by its developer.",
                translations={"en": "Original published summary"},
                description_translations={"en": "Original published description, written by its developer."},
            )

        self.assertEqual(result["summary"], "Original published summary")
        self.assertEqual(result["description"], "Original published description, written by its developer.")
        self.assertEqual(result["translations"]["ar"], "وصف عربي موجز")
        self.assertEqual(result["description_translations"]["ar"], "هذا هو الوصف العربي الأصلي للتطبيق.")
        self.assertEqual(result["translation_status"], "complete")
        self.assertEqual(result["translation_ai_provider"], "test-provider")

    def test_fallback_recognizes_standalone_desktop_application(self):
        result = fallback_project_classification(
            name="Example Desktop App",
            description="A cross-platform desktop application for managing personal notes.",
            topics=["desktop", "notes"],
            readme_excerpt="Download the installer and launch the application.",
        )
        self.assertEqual(result["project_kind"], "application")
        self.assertTrue(result["build_recommended"])

    def test_ai_cannot_recommend_building_a_library_as_an_installable_app(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                response = {
                    "choices": [{
                        "message": {
                            "content": json.dumps({
                                "summary": "A reusable widget SDK.",
                                "description": "A library for embedding widget features in other programs.",
                                "translations": {"en": "A reusable widget SDK."},
                                "project_kind": "library",
                                "build_recommended": True,
                                "classification_reason": "The README describes a reusable SDK.",
                            })
                        }
                    }]
                }
                return json.dumps(response).encode("utf-8")

        settings_mock = type("Settings", (), {
            "ai_api_key": "test-key",
            "ai_model": "test-model",
            "ai_base_url": "https://ai.example/v1",
        })()
        with (
            patch.dict("os.environ", {"APPHORYN_AI_ALLOW_LEGACY_PROVIDER": "true"}),
            patch("app.ai_descriptions.settings", settings_mock),
            patch("app.ai_descriptions.urllib.request.urlopen", return_value=FakeResponse()),
        ):
            result = DescriptionGenerator().generate(
                name="Widget SDK",
                owner="example",
                description="A reusable SDK.",
                topics=["sdk"],
                readme_excerpt="This is a library for application developers.",
                category="developer",
            )

        self.assertEqual(result["project_kind"], "library")
        self.assertFalse(result["build_recommended"])
        self.assertEqual(result["classification_source"], "ai")
        self.assertEqual(result["generated_by_ai"], "true")


if __name__ == "__main__":
    unittest.main()
