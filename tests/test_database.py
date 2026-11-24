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
