import tempfile
import unittest
from pathlib import Path

from app.database import Database, utc_now


class DatabaseTests(unittest.TestCase):
    def test_upsert_and_list_apps(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Database(Path(folder) / "test.sqlite3")
            now = utc_now()
            record = {
                "id": "app-1", "slug": "sample-owner-sample", "name": "Sample",
                "summary": "Real project", "description": "A sample project", 
                "repository_url": "https://github.com/sample/sample", "owner": "sample",
                "license_spdx": "MIT", "license_eligible": True, "category": "developer",
                "topics": ["editor"], "created_at": now, "updated_at": now,
            }
            first = database.upsert_app(record)
            second = database.upsert_app({**record, "id": "another-id", "summary": "Updated"})
            self.assertEqual(first["id"], second["id"])
            self.assertEqual(second["summary"], "Updated")
            self.assertEqual(database.list_apps("developer")[0]["topics"], ["editor"])

    def test_stable_github_repository_id_prevents_duplicates_after_rename(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Database(Path(folder) / "rename.sqlite3")
            now = utc_now()
            base = {
                "id": "stable-app", "slug": "old-owner-old-name", "name": "Old Name",
                "summary": "", "description": "", "repository_url": "https://github.com/old-owner/old-name",
                "github_repo_id": "987654321", "source_updated_at": "2026-11-01T00:00:00Z",
                "owner": "old-owner", "license_spdx": "MIT", "license_eligible": True,
                "category": "developer", "topics": [], "created_at": now, "updated_at": now,
            }
            first = database.upsert_app(base)
            renamed = database.upsert_app({
                **base, "id": "new-generated-id", "slug": "new-owner-new-name",
                "name": "New Name", "repository_url": "https://github.com/new-owner/new-name",
                "owner": "new-owner", "source_updated_at": "2026-11-24T00:00:00Z",
            })
            self.assertEqual(first["id"], renamed["id"])
            self.assertEqual(renamed["repository_url"], "https://github.com/new-owner/new-name")
            self.assertEqual(renamed["github_repo_id"], "987654321")
            self.assertEqual(len(database.list_apps(limit=20)), 1)

    def test_artifact_and_build_job_storage(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Database(Path(folder) / "test.sqlite3")
            now = utc_now()
            database.upsert_app({"id":"app-1","slug":"one-two","name":"One","summary":"","description":"","repository_url":"https://github.com/one/two","owner":"one","license_eligible":True,"category":"other","topics":[],"created_at":now,"updated_at":now})
            artifact = database.add_artifact({"id":"file-1","app_id":"app-1","filename":"app.apk","platform":"android","package_type":"Android package","version":"1.0","size_bytes":100,"sha256":"abc","created_at":now,"storage_path":"/tmp/app.apk"})
            self.assertEqual(artifact["platform"], "android")
            database.add_job({"id":"job-1","app_id":"app-1","repository_url":"https://github.com/one/two","git_ref":"main","targets":["android"],"status":"queued","created_at":now,"updated_at":now})
            self.assertEqual(database.list_jobs()[0]["targets"], ["android"])


if __name__ == "__main__":
    unittest.main()
