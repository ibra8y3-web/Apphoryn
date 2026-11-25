import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from app.analytics import AnalyticsStore


class AnalyticsStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = AnalyticsStore(Path(self.tempdir.name) / "analytics.sqlite3")

    def tearDown(self):
        self.tempdir.cleanup()

    def test_views_downloads_and_searches_are_persisted(self):
        self.store.record_event("app_view", app_slug="sample-app")
        self.store.record_event("download_requested", app_slug="sample-app", artifact_id="sha256:abc", platform="android")
        self.store.record_event("download_completed", app_slug="sample-app", artifact_id="sha256:abc", platform="android")
        self.store.record_search("private search phrase", 4)
        summary = self.store.summary()
        self.assertEqual(summary["views"], 1)
        self.assertEqual(summary["download_requests"], 1)
        self.assertEqual(summary["completed_downloads"], 1)
        self.assertEqual(summary["searches"], 1)
        self.assertEqual(summary["per_app"][0]["app_slug"], "sample-app")
        self.assertEqual(summary["pending_replication"], 4)

    def test_raw_query_ip_and_unapproved_event_fields_are_not_persisted(self):
        self.store.record_event("app_view", app_slug="sample-app", details={
            "ip": "203.0.113.4", "query": "secret query", "authorization": "Bearer bad",
            "client_platform": "android"
        })
        with self.store.connect() as con:
            row = con.execute("SELECT details_json FROM analytics_events").fetchone()
        self.assertNotIn("203.0.113.4", row["details_json"])
        self.assertNotIn("secret query", row["details_json"])
        self.assertNotIn("Bearer bad", row["details_json"])
        self.assertIn("android", row["details_json"])

    def test_search_only_persists_a_hash_not_the_search_text(self):
        phrase = "private user search text"
        self.store.record_search(phrase, 2)
        with self.store.connect() as con:
            row = con.execute("SELECT details_json FROM analytics_events").fetchone()
        self.assertNotIn(phrase, row["details_json"])
        self.assertIn("query_sha256", row["details_json"])

    def test_local_mode_keeps_events_without_claiming_remote_sync(self):
        self.store.record_event("app_view", app_slug="offline-app")
        with patch.dict("os.environ", {
            "APPHORYN_ANALYTICS_POSTGRES_DSN": "",
            "CLOUDFLARE_ACCOUNT_ID": "",
            "CLOUDFLARE_D1_DATABASE_ID": "",
            "CLOUDFLARE_API_TOKEN": "",
        }, clear=False):
            result = self.store.sync_once()
        self.assertEqual(result["status"], "local_only")
        self.assertEqual(self.store.summary()["pending_replication"], 1)

    def test_invalid_event_types_are_rejected(self):
        self.assertIsNone(self.store.record_event("login", details={"user": "someone"}))
        self.assertEqual(self.store.summary()["totals"], {})


if __name__ == "__main__":
    unittest.main()
