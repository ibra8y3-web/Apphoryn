import json
import tempfile
import unittest
from pathlib import Path

from scripts.global_scout import (
    known_current,
    load_catalog_index,
    load_retry_queue,
    repo_key,
    retry_record,
    save_retry_queue,
)


class GlobalScoutTests(unittest.TestCase):
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
        }
        by_id = {"123": existing}
        by_url = {repo_key(existing["repository_url"]): existing}
        unchanged = {"id": 123, "html_url": existing["repository_url"], "updated_at": "2026-01-01T00:00:00Z"}
        changed = {"id": 123, "html_url": existing["repository_url"], "updated_at": "2026-01-02T00:00:00Z"}
        self.assertTrue(known_current(unchanged, by_id, by_url))
        self.assertFalse(known_current(changed, by_id, by_url))
        self.assertFalse(known_current(changed, {}, {}))

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
