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
                "project_kind": "library", "build_recommended": False,
                "classification_source": "ai", "classification_reason": "Reusable SDK.",
                "topics": ["editor"], "created_at": now, "updated_at": now,
            }
            first = database.upsert_app(record)
            second = database.upsert_app({**record, "id": "another-id", "summary": "Updated"})
            self.assertEqual(first["id"], second["id"])
            self.assertEqual(second["summary"], "Updated")
            self.assertEqual(second["project_kind"], "library")
            self.assertFalse(second["build_recommended"])
            self.assertEqual(second["classification_source"], "ai")
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


    def test_same_content_hash_is_not_duplicated_across_versions_or_filenames(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Database(Path(folder) / "dedupe.sqlite3")
            now = utc_now()
            database.upsert_app({
                "id": "app-1", "slug": "one-two", "name": "One", "summary": "",
                "description": "", "repository_url": "https://github.com/one/two",
                "owner": "one", "license_eligible": True, "category": "other",
                "topics": [], "created_at": now, "updated_at": now,
            })
            first = database.add_artifact({
                "id": "file-1", "app_id": "app-1", "filename": "one-1.0.exe",
                "platform": "windows", "package_type": "Windows installer", "version": "1.0",
                "size_bytes": 256, "sha256": "a" * 64, "created_at": now, "storage_path": "/store/a.exe",
            })
            second = database.add_artifact({
                "id": "file-2", "app_id": "app-1", "filename": "one-1.1.exe",
                "platform": "windows", "package_type": "Windows installer", "version": "1.1",
                "size_bytes": 256, "sha256": "A" * 64, "created_at": now, "storage_path": "/store/b.exe",
            })
            self.assertEqual(first["id"], second["id"])
            self.assertEqual(len(database.list_artifacts("app-1")), 1)
            self.assertEqual(database.get_artifact_by_sha256("app-1", "a" * 64)["id"], first["id"])


    def test_legacy_duplicate_artifacts_are_collapsed_before_unique_sha_index_creation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "legacy-dedupe.sqlite3"
            database = Database(path)
            now = utc_now()
            database.upsert_app({
                "id": "app-legacy", "slug": "legacy-app", "name": "Legacy",
                "summary": "", "description": "", "repository_url": "https://github.com/legacy/app",
                "owner": "legacy", "license_eligible": True, "category": "other",
                "topics": [], "created_at": now, "updated_at": now,
            })
            # Simulate rows created by a prior schema that only deduplicated
            # identical app/filename/version/hash tuples.
            with database.connect() as con:
                con.execute("DROP INDEX idx_artifact_app_sha_unique")
                for artifact_id, filename, version in (
                    ("legacy-one", "legacy-1.0.exe", "1.0"),
                    ("legacy-two", "legacy-1.1.exe", "1.1"),
                ):
                    con.execute(
                        """INSERT INTO artifacts
                           (id,app_id,filename,platform,package_type,version,size_bytes,sha256,
                            upstream_digest_verified,validation_status,signature_status,
                            signer_certificate_sha256,source_url,storage_path,created_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (artifact_id, "app-legacy", filename, "windows", "Windows executable",
                         version, 512, "b" * 64, 0, "validated", "not_checked", None,
                         "https://github.com/legacy/app", f"/store/{filename}", now),
                    )

            migrated = Database(path)
            artifacts = migrated.list_artifacts("app-legacy")
            self.assertEqual(len(artifacts), 1)
            with migrated.connect() as con:
                indexes = con.execute("PRAGMA index_list(artifacts)").fetchall()
            self.assertTrue(any(row["name"] == "idx_artifact_app_sha_unique" and row["unique"] for row in indexes))


if __name__ == "__main__":
    unittest.main()
