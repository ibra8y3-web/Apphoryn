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


    def test_pending_outbox_batch_is_claimed_once_during_retry_window(self):
        self.store.record_event("app_view", app_slug="offline-app")
        first = self.store._pending_batch(limit=10)
        second = self.store._pending_batch(limit=10)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(first[0]["app_slug"], "offline-app")

    def test_cloudflare_d1_uses_one_parameterized_insert_for_the_whole_batch(self):
        rows = [
            {"id": "one", "event_type": "app_view", "app_slug": "app-one",
             "artifact_id": None, "platform": None, "details": {}, "created_at": "2026-01-01T00:00:00+00:00"},
            {"id": "two", "event_type": "download_requested", "app_slug": "app-two",
             "artifact_id": "sha256:two", "platform": "android", "details": {}, "created_at": "2026-01-01T00:00:01+00:00"},
        ]
        config = {"account_id": "account", "database_id": "database", "token": "secret"}
        with patch.object(AnalyticsStore, "_d1_request") as request:
            AnalyticsStore._sync_d1(config, rows)
        self.assertEqual(request.call_count, 2)
        batch_payload = request.call_args_list[-1].args[3]
        self.assertEqual(len(batch_payload["batch"]), 1)
        statement = batch_payload["batch"][0]
        self.assertIn("INSERT OR IGNORE", statement["sql"])
        self.assertEqual(len(statement["params"]), 14)
        self.assertLessEqual(len(statement["params"]), 100)

    def test_invalid_event_types_are_rejected(self):
        self.assertIsNone(self.store.record_event("login", details={"user": "someone"}))
        self.assertEqual(self.store.summary()["totals"], {})


if __name__ == "__main__":
    unittest.main()
