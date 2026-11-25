import json
import tempfile
import unittest
from pathlib import Path

from app.repository_crawler import RepositoryCrawler, RepositoryCrawlError


class FakeGitHub:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def list_public_repositories(self, since, per_page):
        self.calls.append({"since": since, "per_page": per_page})
        if not self.pages:
            return []
        return self.pages.pop(0)


class RepositoryCrawlerTests(unittest.TestCase):
    def test_public_repo_scan_deduplicates_and_persists_since_cursor(self):
        fake = FakeGitHub([[
            {"id": 10, "full_name": "alpha/one"},
            {"id": 10, "full_name": "alpha/one"},
            {"id": 11, "full_name": "beta/two"},
        ]])
        with tempfile.TemporaryDirectory() as folder:
            state_path = Path(folder) / "state.json"
            crawler = RepositoryCrawler(fake, state_path)
            rows, report = crawler.scan(max_repositories=50, max_pages=1)
            self.assertEqual([row["id"] for row in rows], [10, 11])
            self.assertEqual(report["last_repository_id"], 11)
            self.assertEqual(fake.calls, [{"since": 0, "per_page": 100}])
            crawler.save()

            resumed = RepositoryCrawler(FakeGitHub([[{"id": 12, "full_name": "gamma/three"}]]), state_path)
            more, resumed_report = resumed.scan(max_repositories=50, max_pages=1)
            self.assertEqual([row["id"] for row in more], [12])
            self.assertEqual(resumed_report["last_repository_id"], 12)

    def test_batch_limit_consumes_the_whole_page_before_checkpointing(self):
        page = [{"id": index, "full_name": f"team/repo-{index}"} for index in range(1, 101)]
        fake = FakeGitHub([page, [{"id": 101, "full_name": "team/repo-101"}]])
        with tempfile.TemporaryDirectory() as folder:
            crawler = RepositoryCrawler(fake, Path(folder) / "state.json")
            rows, report = crawler.scan(max_repositories=10, max_pages=1)
            self.assertEqual(len(rows), 100)
            self.assertEqual(report["last_repository_id"], 100)
            next_rows, _ = crawler.scan(max_repositories=10, max_pages=1)
            self.assertEqual([row["id"] for row in next_rows], [101])

    def test_empty_tail_preserves_cursor_for_new_repositories_on_next_run(self):
        fake = FakeGitHub([[]])
        with tempfile.TemporaryDirectory() as folder:
            state_path = Path(folder) / "state.json"
            crawler = RepositoryCrawler(fake, state_path)
            crawler.state["last_repository_id"] = 900
            _, report = crawler.scan(max_pages=1)
            self.assertTrue(report["reached_current_end"])
            self.assertEqual(crawler.state["last_repository_id"], 900)
            crawler.save()
            resumed = RepositoryCrawler(FakeGitHub([[{"id": 901, "full_name": "new/repo"}]]), state_path)
            rows, _ = resumed.scan(max_pages=1)
            self.assertEqual([row["id"] for row in rows], [901])

    def test_refresh_cursor_is_independent_from_forward_discovery_cursor(self):
        fake = FakeGitHub([[{"id": 101, "full_name": "old/repo"}]])
        with tempfile.TemporaryDirectory() as folder:
            crawler = RepositoryCrawler(fake, Path(folder) / "state.json")
            crawler.state["last_repository_id"] = 900
            rows, report = crawler.scan(max_pages=1, cursor_key="last_refresh_repository_id")
            self.assertEqual([row["id"] for row in rows], [101])
            self.assertEqual(fake.calls[0]["since"], 0)
            self.assertEqual(crawler.state["last_repository_id"], 900)
            self.assertEqual(crawler.state["last_refresh_repository_id"], 101)
            self.assertEqual(report["cursor_key"], "last_refresh_repository_id")

    def test_schema_two_checkpoint_migrates_without_losing_forward_cursor(self):
        with tempfile.TemporaryDirectory() as folder:
            state_path = Path(folder) / "state.json"
            state_path.write_text(json.dumps({
                "schema_version": 2, "last_repository_id": 1234, "totals": {}
            }), encoding="utf-8")
            crawler = RepositoryCrawler(FakeGitHub([]), state_path)
            self.assertEqual(crawler.state["schema_version"], 3)
            self.assertEqual(crawler.state["last_repository_id"], 1234)
            self.assertEqual(crawler.state["last_refresh_repository_id"], 0)

    def test_nonadvancing_api_page_fails_closed(self):
        fake = FakeGitHub([[{"id": 10, "full_name": "same/repo"}]])
        with tempfile.TemporaryDirectory() as folder:
            crawler = RepositoryCrawler(fake, Path(folder) / "state.json")
            crawler.state["last_repository_id"] = 10
            with self.assertRaises(RepositoryCrawlError):
                crawler.scan(max_pages=1)


if __name__ == "__main__":
    unittest.main()
