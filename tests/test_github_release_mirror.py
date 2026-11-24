import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.github_release_mirror import GitHubReleaseMirror


def minimal_pe(path: Path) -> str:
    data = bytearray(256)
    data[:2] = b"MZ"
    data[0x3C:0x40] = (128).to_bytes(4, "little")
    data[128:132] = b"PE\0\0"
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


class GitHubReleaseMirrorTests(unittest.TestCase):
    def test_publish_app_uses_apphoryn_release_asset_and_writes_catalog(self):
        with tempfile.TemporaryDirectory() as folder:
            file_path = Path(folder) / "tool.exe"
            digest = minimal_pe(file_path)
            mirror = GitHubReleaseMirror("test-token", "owner/Apphoryn")
            mirror._ensure_release = Mock(return_value={
                "id": 50,
                "html_url": "https://github.com/owner/Apphoryn/releases/tag/apphoryn-tool-1.0",
                "upload_url": "https://uploads.github.com/repos/owner/Apphoryn/releases/50/assets{?name,label}",
            })
            mirror._upload_asset = Mock(return_value={
                "name": "tool.exe",
                "browser_download_url": "https://github.com/owner/Apphoryn/releases/download/apphoryn-tool-1.0/tool.exe",
                "digest": f"sha256:{digest}",
            })
            mirror._read_catalog = Mock(return_value=({"schema_version": 1, "apps": []}, None))
            mirror.request = Mock(return_value={})
            app = {
                "id": "db-id", "slug": "owner-tool", "name": "Tool", "summary": "Tool utility",
                "description": "A real tool.", "repository_url": "https://github.com/owner/tool",
                "owner": "owner", "license_spdx": "MIT", "license_eligible": True,
                "category": "system", "topics": ["utility"], "latest_version": "1.0",
            }
            artifact = {
                "filename": "tool.exe", "platform": "windows", "package_type": "Windows executable",
                "version": "1.0", "size_bytes": file_path.stat().st_size, "sha256": digest,
                "upstream_digest_verified": True, "signature_status": "not_checked", "storage_path": str(file_path),
            }
            result = mirror.publish_app(app, [artifact])
            self.assertEqual(len(result["mirrored_artifacts"]), 1)
            self.assertIn("owner/Apphoryn/releases/download", result["mirrored_artifacts"][0]["download_url"])
            method, url, payload = mirror.request.call_args.args
            self.assertEqual(method, "PUT")
            self.assertIn("apps/web/catalog/index.json", url)
            manifest = json.loads(base64.b64decode(payload["content"]).decode("utf-8"))
            self.assertEqual(len(manifest["apps"]), 1)
            self.assertEqual(manifest["apps"][0]["artifacts"][0]["sha256"], digest)
            self.assertIn("owner/Apphoryn/releases/download", manifest["apps"][0]["artifacts"][0]["download_url"])

    def test_register_source_only_app_without_creating_release_or_installer(self):
        manifest = {"schema_version": 1, "apps": []}
        mirror = GitHubReleaseMirror("test-token", "owner/Apphoryn")
        mirror._read_catalog = Mock(return_value=(manifest, None))
        mirror.request = Mock(return_value={})
        result = mirror.register_source_app({
            "id": "source-id", "slug": "owner-tool", "name": "Tool",
            "repository_url": "https://github.com/owner/tool.git",
            "github_repo_id": "123456", "source_updated_at": "2026-11-24T00:00:00Z",
            "license_spdx": "MIT", "license_eligible": True,
            "default_branch": "main", "topics": ["utility"],
        })
        self.assertEqual(result["source_status"], "source_only")
        self.assertEqual(result["artifacts"], [])
        self.assertTrue(result["scout_checked_at"])
        self.assertEqual(result["repository_url"], "https://github.com/owner/tool")
        method, url, payload = mirror.request.call_args.args
        self.assertEqual(method, "PUT")
        self.assertIn("apps/web/catalog/index.json", url)
        written = json.loads(base64.b64decode(payload["content"]).decode("utf-8"))
        self.assertEqual(len(written["apps"]), 1)
        self.assertEqual(written["apps"][0]["artifacts"], [])

    def test_mark_app_checked_persists_round_robin_release_check_timestamp(self):
        manifest = {"schema_version": 1, "apps": [{
            "id": "tool", "repository_url": "https://github.com/owner/tool", "artifacts": [],
        }]}
        mirror = GitHubReleaseMirror("test-token", "owner/Apphoryn")
        mirror._read_catalog = Mock(return_value=(manifest, "old-sha"))
        mirror.request = Mock(return_value={})
        self.assertTrue(mirror.mark_app_checked("https://github.com/owner/tool"))
        _, _, payload = mirror.request.call_args.args
        written = json.loads(base64.b64decode(payload["content"]).decode("utf-8"))
        self.assertTrue(written["apps"][0]["scout_checked_at"])
        self.assertEqual(written["apps"][0]["repository_url"], "https://github.com/owner/tool")

    def test_publish_build_outputs_updates_existing_app_record(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "artifacts"
            root.mkdir()
            target = root / "windows"
            target.mkdir()
            binary = target / "tool.exe"
            digest = minimal_pe(binary)
            manifest = {"schema_version": 1, "apps": [{
                "id": "tool", "slug": "owner-tool", "name": "Tool",
                "repository_url": "https://github.com/owner/tool", "license_spdx": "MIT", "license_eligible": True, "artifacts": [],
            }]}
            mirror = GitHubReleaseMirror("test-token", "owner/Apphoryn")
            mirror._ensure_release = Mock(return_value={
                "id": 51, "html_url": "https://github.com/owner/Apphoryn/releases/tag/build",
                "upload_url": "https://uploads.github.com/repos/owner/Apphoryn/releases/51/assets{?name,label}",
            })
            mirror._upload_asset = Mock(return_value={
                "name": "windows-tool.exe",
                "browser_download_url": "https://github.com/owner/Apphoryn/releases/download/build/windows-tool.exe",
            })
            mirror._read_catalog = Mock(return_value=(manifest, "manifest-sha"))
            mirror.request = Mock(return_value={})
            result = mirror.publish_built_artifacts("https://github.com/owner/tool", "v1.2.0", root)
            self.assertEqual(len(result["published"]), 1)
            _, _, payload = mirror.request.call_args.args
            written = json.loads(base64.b64decode(payload["content"]).decode("utf-8"))
            self.assertEqual(written["apps"][0]["artifacts"][0]["sha256"], digest)
            self.assertTrue(written["apps"][0]["artifacts"][0]["download_url"].startswith("https://github.com/owner/Apphoryn/releases/"))

    def test_dispatch_records_attempt_and_prevents_eight_hour_rescan_duplicates(self):
        manifest = {"schema_version": 1, "apps": [{
            "id": "tool", "slug": "owner-tool", "name": "Tool",
            "repository_url": "https://github.com/owner/tool", "license_spdx": "MIT",
            "license_eligible": True, "artifacts": [],
        }]}
        mirror = GitHubReleaseMirror("test-token", "owner/Apphoryn")
        mirror._read_catalog = Mock(return_value=(manifest, "old-sha"))
        mirror.request = Mock(return_value={})
        ref = "a" * 40
        mirror.dispatch_build("https://github.com/owner/tool", ref, ["android", "linux"], "owner-tool")
        self.assertEqual(mirror.request.call_count, 2)
        first_call = mirror.request.call_args_list[0]
        self.assertEqual(first_call.args[0], "PUT")
        payload = first_call.args[2]
        updated = json.loads(base64.b64decode(payload["content"]).decode("utf-8"))
        self.assertEqual(updated["apps"][0]["build_attempts"][0]["git_ref"], ref)
        self.assertTrue(mirror.has_recent_build_attempt("https://github.com/owner/tool", ref))

    def test_published_build_ref_is_never_requeued(self):
        manifest = {"schema_version": 1, "apps": [{
            "repository_url": "https://github.com/owner/tool", "license_spdx": "MIT",
            "license_eligible": True, "build_refs": ["b" * 40], "artifacts": [],
        }]}
        mirror = GitHubReleaseMirror("test-token", "owner/Apphoryn")
        mirror._read_catalog = Mock(return_value=(manifest, "sha"))
        self.assertTrue(mirror.has_recent_build_attempt("https://github.com/owner/tool", "b" * 40))


if __name__ == "__main__":
    unittest.main()
