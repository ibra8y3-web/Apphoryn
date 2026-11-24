import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from app.repository_crawler import RepositoryCrawler, RepositoryCrawlError


class FakeGitHub:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def search_repositories(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            return {"total_count": 0, "items": []}
        return self.responses.pop(0)


class RepositoryCrawlerTests(unittest.TestCase):
    def test_resumable_scan_records_cursor_and_deduplicates_same_page(self):
        fake = FakeGitHub([{
            "total_count": 2,
            "items": [
                {"id": 10, "full_name": "alpha/one"},
                {"id": 10, "full_name": "alpha/one"},
                {"id": 11, "full_name": "beta/two"},
            ],
        }])
        with tempfile.TemporaryDirectory() as folder:
            state_path = Path(folder) / "state.json"
            crawler = RepositoryCrawler(fake, state_path)
            records, report = crawler.scan(max_repositories=10, max_search_requests=1, today=date(2008, 1, 1))
            self.assertEqual([row["id"] for row in records], [10, 11])
            self.assertEqual(report["repositories"], 2)
            crawler.save()
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["next_created_date"], "2008-01-01")
            self.assertEqual(saved["totals"]["repositories_seen"], 2)

    def test_dense_date_range_is_split_without_losing_the_window(self):
        fake = FakeGitHub([
            {"total_count": 1400, "items": []},
            {"total_count": 0, "items": []},
        ])
        with tempfile.TemporaryDirectory() as folder:
            crawler = RepositoryCrawler(fake, Path(folder) / "state.json")
            _, report = crawler.scan(max_repositories=10, max_search_requests=2, today=date(2008, 1, 30))
            self.assertEqual(report["dense_partitions_split"], 1)
            self.assertEqual(len(crawler.state["pending"]), 1)
            self.assertEqual(fake.calls[1]["query"].split()[0], "created:2008-01-01..2008-01-15")

    def test_single_day_dense_partition_is_split_by_star_ranges(self):
        fake = FakeGitHub([{"total_count": 1500, "items": []}])
        with tempfile.TemporaryDirectory() as folder:
            crawler = RepositoryCrawler(fake, Path(folder) / "state.json")
            crawler.scan(max_repositories=1, max_search_requests=1, today=date(2008, 1, 1))
            self.assertEqual(len(crawler.state["pending"]), 6)
            self.assertEqual(crawler.state["pending"][0]["stars"], "0")

    def test_unsupported_leaf_overflow_fails_closed(self):
        fake = FakeGitHub([{"total_count": 1200, "items": []}])
        with tempfile.TemporaryDirectory() as folder:
            crawler = RepositoryCrawler(fake, Path(folder) / "state.json")
            crawler.state["pending"] = [{
                "created_from": "2008-01-01", "created_to": "2008-01-01",
                "stars": "0", "forks": "0", "size": "0..100", "page": 1,
                "total_count": None,
            }]
            with self.assertRaises(RepositoryCrawlError):
                crawler.scan(max_repositories=1, max_search_requests=1, today=date(2008, 1, 1))


if __name__ == "__main__":
    unittest.main()
