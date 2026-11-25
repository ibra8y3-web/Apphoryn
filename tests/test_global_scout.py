import json
import tempfile
import unittest
from pathlib import Path

from scripts.global_scout import (
    configured_discovery_queries,
    known_current,
    load_catalog_index,
    load_retry_queue,
    localize_existing_catalog_rows,
    repo_key,
    retry_record,
    save_retry_queue,
)


class GlobalScoutTests(unittest.TestCase):
    def test_default_global_queries_target_recent_installable_app_categories(self):
        from unittest.mock import patch

        with patch.dict("os.environ", {}, clear=False):
            self.assertTrue(any("topic:android-app" in query for query in configured_discovery_queries()))
            self.assertTrue(any("topic:desktop-app" in query for query in configured_discovery_queries()))
            self.assertTrue(all("pushed:>" not in query for query in configured_discovery_queries()))

    def test_global_queries_can_be_overridden_and_are_deduplicated(self):
        from unittest.mock import patch

        with patch.dict("os.environ", {
            "APPHORYN_GLOBAL_SCOUT_DISCOVERY_QUERIES": "topic:android-app\ntopic:desktop-app\ntopic:android-app"
        }, clear=False):
            self.assertEqual(
                configured_discovery_queries(),
                ["topic:android-app", "topic:desktop-app"],
            )

    def test_zero_byte_catalog_manifest_is_a_valid_empty_starting_point(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "index.json"
            path.write_text("", encoding="utf-8")

            self.assertEqual(load_catalog_index(path), [])

    def test_repository_key_normalizes_trailing_slash_and_git_suffix(self):
        self.assertEqual(repo_key("https://github.com/Owner/Repo.git/"), "https://github.com/owner/repo")

    def test_unchanged_repository_is_skipped_but_newer_source_is_revisited(self):
        existing = {
            "github_repo_id": "123",
            "repository_url": "https://github.com/owner/repo",
            "source_updated_at": "2026-01-01T00:00:00Z",
            "translation_status": "complete",
            "translations": {"ar": "وصف عربي"},
            "description_translations": {"ar": "شرح عربي كامل"},
        }
        by_id = {"123": existing}
        by_url = {repo_key(existing["repository_url"]): existing}
        unchanged = {"id": 123, "html_url": existing["repository_url"], "updated_at": "2026-01-01T00:00:00Z"}
        changed = {"id": 123, "html_url": existing["repository_url"], "updated_at": "2026-01-02T00:00:00Z"}
        self.assertTrue(known_current(unchanged, by_id, by_url))
        self.assertFalse(known_current(changed, by_id, by_url))
        self.assertFalse(known_current(changed, {}, {}))

    def test_missing_arabic_translation_does_not_force_source_reimport(self):
        existing = {
            "github_repo_id": "321",
            "repository_url": "https://github.com/owner/untranslated",
            "source_updated_at": "2026-01-01T00:00:00Z",
            "translation_status": "english_only",
            "translations": {"en": "An English summary"},
            "description_translations": {"en": "An English description"},
        }
        candidate = {
            "id": 321,
            "html_url": existing["repository_url"],
            "updated_at": "2026-01-01T00:00:00Z",
        }
        by_id = {"321": existing}
        by_url = {repo_key(existing["repository_url"]): existing}
        # Translation is handled by the translation-only queue; source freshness
        # must not re-download/re-import the app or replace its published wording.
        self.assertTrue(known_current(candidate, by_id, by_url))

    def test_known_repository_reopens_after_build_adapter_revision_changes(self):
        existing = {
            "github_repo_id": "789",
            "repository_url": "https://github.com/owner/repo",
            "source_updated_at": "2026-01-01T00:00:00Z",
            "translation_status": "complete",
            "translations": {"ar": "وصف عربي"},
            "description_translations": {"ar": "شرح عربي كامل"},
            "artifacts": [],
            "build_attempts": [{
                "git_ref": "abc123",
                "targets": ["windows", "linux"],
                "adapter_revision": "old-revision",
                "requested_at": "2026-11-25T09:00:00+00:00",
            }],
        }
        candidate = {
            "id": 789,
            "html_url": existing["repository_url"],
            "updated_at": "2026-01-01T00:00:00Z",
        }
        by_id = {"789": existing}
        by_url = {repo_key(existing["repository_url"]): existing}
        self.assertFalse(known_current(candidate, by_id, by_url, adapter_revision="new-revision"))
        self.assertTrue(known_current(candidate, by_id, by_url, adapter_revision="old-revision"))

    def test_existing_catalog_localization_preserves_published_english_description(self):
        from unittest.mock import patch

        app = {
            "name": "Example App",
            "slug": "example-app",
            "summary": "Original published summary",
            "description": "Original developer-written description.",
            "translations": {"en": "Original published summary"},
            "description_translations": {"en": "Original developer-written description."},
            "translation_status": "english_only",
            "license_eligible": True,
            "license_spdx": "MIT",
            "repository_url": "https://github.com/owner/example-app",
        }
        localized = {
            "summary": "Original published summary",
            "description": "Original developer-written description.",
            "translations": {"en": "Original published summary", "ar": "ملخص عربي"},
            "description_translations": {
                "en": "Original developer-written description.",
                "ar": "وصف عربي للمطور.",
            },
            "translation_status": "complete",
        }

        class FakeMirror:
            def __init__(self):
                self.published = None

            def publish_app(self, updated, artifacts):
                self.published = (updated, artifacts)
                return {"project_status": "source_available"}

        mirror = FakeMirror()
        with patch("scripts.global_scout.call_apphoryn_internal_api", return_value=localized) as translate:
            report = localize_existing_catalog_rows([app], mirror, interval_seconds=0)

        self.assertEqual(report["translated"], 1)
        self.assertEqual(app["description"], "Original developer-written description.")
        self.assertEqual(app["description_translations"]["ar"], "وصف عربي للمطور.")
        self.assertEqual(mirror.published[0]["description"], "Original developer-written description.")
        self.assertEqual(mirror.published[1], [])
        translate.assert_called_once()

    def test_retry_backoff_is_bounded_and_preserves_upstream_candidate(self):
        entry = {
            "repository": "https://github.com/owner/repo",
            "candidate": {"id": 456, "html_url": "https://github.com/owner/repo"},
            "attempts": 0,
        }
        first = retry_record(entry, 1000, "temporary error")
        second = retry_record({**entry, "attempts": 1}, 1000, "temporary error")
        self.assertEqual(first["attempts"], 1)
        self.assertEqual(first["next_attempt_at"], 1000 + 6 * 3600)
        self.assertEqual(second["attempts"], 2)
        self.assertEqual(second["next_attempt_at"], 1000 + 24 * 3600)
        self.assertEqual(first["candidate"]["id"], 456)

    def test_retry_queue_splits_due_and_waiting_records_and_persists_atomically(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "scout-retry.json"
            rows = [
                {"repository": "https://github.com/owner/due", "next_attempt_at": 10},
                {"repository": "https://github.com/owner/later", "next_attempt_at": 500},
            ]
            save_retry_queue(path, rows)
            due, waiting = load_retry_queue(path, now=100)
            self.assertEqual([row["repository"] for row in due], ["https://github.com/owner/due"])
            self.assertEqual([row["repository"] for row in waiting], ["https://github.com/owner/later"])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), rows)
            self.assertFalse(path.with_suffix(".json.tmp").exists())


if __name__ == "__main__":
    unittest.main()
