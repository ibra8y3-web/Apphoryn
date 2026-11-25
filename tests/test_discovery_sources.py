import unittest
from unittest.mock import patch

from app.discovery_sources import DiscoverySources


class DiscoverySourcesTests(unittest.TestCase):
    def test_fdroid_and_flathub_sources_return_metadata_with_explicit_trust_states(self):
        replies = [
            {"apps": [{
                "packageName": "org.example.reader",
                "name": "Example Reader",
                "summary": "A sample reader",
                "suggestedVersionName": "1.2",
                "license": "GPL-3.0",
                "sourceCode": "https://github.com/example/reader",
            }]},
            {"hits": [{
                "app_id": "org.example.Reader",
                "name": "Example Reader Desktop",
                "summary": "Desktop reader",
                "project_license": "GPL-3.0",
            }]},
        ]
        with patch("app.discovery_sources._request_json", side_effect=replies) as request:
            result = DiscoverySources().search("reader", 5)
        self.assertEqual(result["count"], 2)
        by_source = {item["source"]: item for item in result["items"]}
        self.assertEqual(by_source["fdroid"]["trust_state"], "metadata_only")
        self.assertEqual(by_source["flathub"]["trust_state"], "flathub_catalog_metadata")
        self.assertEqual(request.call_count, 2)
        self.assertTrue(result["limitations"])

    def test_source_failure_is_reported_without_fabricating_results(self):
        with patch("app.discovery_sources._request_json", side_effect=[
            RuntimeError("F-Droid unavailable"),
            {"hits": []},
        ]):
            result = DiscoverySources().search("reader", 5)
        self.assertFalse(result["sources"]["fdroid"]["ok"])
        self.assertTrue(result["sources"]["flathub"]["ok"])
        self.assertEqual(result["items"], [])
        self.assertEqual(result["count"], 0)

    def test_too_short_query_is_rejected(self):
        with self.assertRaises(ValueError):
            DiscoverySources().search("x", 5)


if __name__ == "__main__":
    unittest.main()
