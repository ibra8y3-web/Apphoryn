import unittest

from app.services import CatalogService


class FakeGitHub:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def search_repositories(self, query, page, per_page):
        self.calls.append((query, page, per_page))
        return self.pages.get(page, {"total_count": 0, "items": []})


class CatalogDiscoveryPaginationTests(unittest.TestCase):
    def test_discovery_reads_every_available_search_page(self):
        pages = {
            1: {"total_count": 205, "items": [
                {"id": index, "name": f"app-{index}", "full_name": f"owner/app-{index}",
                 "html_url": f"https://github.com/owner/app-{index}", "license": {"spdx_id": "MIT"}}
                for index in range(1, 101)
            ]},
            2: {"total_count": 205, "items": [
                {"id": index, "name": f"app-{index}", "full_name": f"owner/app-{index}",
                 "html_url": f"https://github.com/owner/app-{index}", "license": {"spdx_id": "MIT"}}
                for index in range(101, 201)
            ]},
            3: {"total_count": 205, "items": [
                {"id": index, "name": f"app-{index}", "full_name": f"owner/app-{index}",
                 "html_url": f"https://github.com/owner/app-{index}", "license": {"spdx_id": "MIT"}}
                for index in range(201, 206)
            ]},
        }
        github = FakeGitHub(pages)
        rows = CatalogService(github=github).discover("topic:android-app", limit=100)

        self.assertEqual(len(rows), 205)
        self.assertEqual([call[1] for call in github.calls], [1, 2, 3])
        self.assertTrue(all(call[2] == 100 for call in github.calls))
        self.assertEqual(rows[0]["id"], 1)
        self.assertEqual(rows[-1]["id"], 205)

    def test_search_discovery_respects_github_search_api_1000_result_ceiling(self):
        pages = {
            page: {"total_count": 2500, "items": [
                {"id": (page - 1) * 100 + index, "name": f"app-{(page - 1) * 100 + index}",
                 "full_name": f"owner/app-{(page - 1) * 100 + index}",
                 "html_url": f"https://github.com/owner/app-{(page - 1) * 100 + index}",
                 "license": {"spdx_id": "MIT"}}
                for index in range(1, 101)
            ]}
            for page in range(1, 11)
        }
        github = FakeGitHub(pages)
        rows = CatalogService(github=github).discover("topic:opensource", limit=100)

        self.assertEqual(len(rows), 1000)
        self.assertEqual(len(github.calls), 10)


if __name__ == "__main__":
    unittest.main()
