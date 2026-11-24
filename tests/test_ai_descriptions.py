import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.ai_descriptions import DescriptionGenerator, fallback_project_classification


class AIProjectClassificationTests(unittest.TestCase):
    def classify(self, *, description="", topics=None, readme=""):
        return fallback_project_classification(
            name="Sample",
            description=description,
            topics=topics or [],
            readme_excerpt=readme,
        )

    def test_standalone_desktop_application_is_recommended_for_build(self):
        result = self.classify(description="A desktop application for managing local files.")
        self.assertEqual(result["project_kind"], "application")
        self.assertTrue(result["build_recommended"])

    def test_reusable_library_is_not_recommended_for_build(self):
        result = self.classify(
            description="A Python library for integrating the Example service.",
            topics=["sdk", "api-client"],
        )
        self.assertEqual(result["project_kind"], "library")
        self.assertFalse(result["build_recommended"])

    def test_documentation_and_uncertain_repositories_are_not_auto_built(self):
        docs = self.classify(description="Documentation site and tutorial collection for new users.")
        unknown = self.classify(description="Personal experiments and source files.")
        self.assertEqual(docs["project_kind"], "documentation")
        self.assertFalse(docs["build_recommended"])
        self.assertEqual(unknown["project_kind"], "unknown")
        self.assertFalse(unknown["build_recommended"])

    def test_description_generator_uses_evidence_fallback_without_ai_credentials(self):
        settings_mock = SimpleNamespace(ai_api_key="", ai_model="")
        with patch("app.ai_descriptions.settings", settings_mock):
            result = DescriptionGenerator().generate(
                name="Example App",
                owner="example",
                description="A mobile application for reading open books.",
                topics=["android", "reader"],
                readme_excerpt="Install the application on Android.",
                category="books",
            )
        self.assertEqual(result["project_kind"], "application")
        self.assertTrue(result["build_recommended"])
        self.assertEqual(result["classification_source"], "heuristic")
        self.assertEqual(result["generated_by_ai"], "false")
        self.assertTrue(result["summary"])


if __name__ == "__main__":
    unittest.main()
