import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.github_release_mirror import GitHubReleaseMirror


def minimal_pe(path: Path) -> str:
    data = bytearray(1024)
    pe_offset = 128
    data[:2] = b"MZ"
    data[0x3C:0x40] = pe_offset.to_bytes(4, "little")
    data[pe_offset:pe_offset + 4] = bytes((0x50, 0x45, 0, 0))
    data[pe_offset + 4:pe_offset + 6] = (0x8664).to_bytes(2, "little")
    data[pe_offset + 6:pe_offset + 8] = (1).to_bytes(2, "little")
    data[pe_offset + 20:pe_offset + 22] = (0xF0).to_bytes(2, "little")
    data[pe_offset + 22:pe_offset + 24] = (0x0002).to_bytes(2, "little")
    optional = pe_offset + 24
    data[optional:optional + 2] = (0x20B).to_bytes(2, "little")
    data[optional + 16:optional + 20] = (0x1000).to_bytes(4, "little")
    section = optional + 0xF0
    data[section:section + 5] = b".text"
    data[section + 8:section + 12] = (0x1000).to_bytes(4, "little")
    data[section + 12:section + 16] = (0x1000).to_bytes(4, "little")
    data[section + 16:section + 20] = (128).to_bytes(4, "little")
    data[section + 20:section + 24] = (512).to_bytes(4, "little")
    data[section + 36:section + 40] = (0x60000020).to_bytes(4, "little")
    data[512:640] = bytes((0xC3,)) * 128
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
            self.assertEqual(manifest["apps"][0]["project_status"], "installable")
            self.assertIn("owner/Apphoryn/releases/download", manifest["apps"][0]["artifacts"][0]["download_url"])

    def test_source_only_publish_registers_project_without_empty_release(self):
        manifest = {"schema_version": 1, "apps": []}
        mirror = GitHubReleaseMirror("test-token", "owner/Apphoryn")
        mirror._read_catalog = Mock(return_value=(manifest, None))
        mirror.request = Mock(return_value={})
        mirror._ensure_release = Mock()

        result = mirror.publish_app({
            "id": "source-project", "slug": "owner-source-project", "name": "Source Project",
            "repository_url": "https://github.com/owner/source-project",
            "github_repo_id": "1234567", "owner": "owner",
            "license_spdx": "MIT", "license_eligible": True,
        }, [])

        self.assertTrue(result["source_project_registered"])
        self.assertEqual(result["mirrored_artifacts"], [])
        mirror._ensure_release.assert_not_called()
        _, _, payload = mirror.request.call_args.args
        written = json.loads(base64.b64decode(payload["content"]).decode("utf-8"))
        self.assertEqual(len(written["apps"]), 1)
        self.assertEqual(written["apps"][0]["project_status"], "source_available")
        self.assertEqual(written["apps"][0]["artifacts"], [])

    def test_invalid_installer_is_rejected_before_creating_release(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fake-installer.exe"
            path.write_bytes(b"this is not a Windows executable")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            mirror = GitHubReleaseMirror("test-token", "owner/Apphoryn")
            mirror._read_catalog = Mock(return_value=({"schema_version": 1, "apps": []}, None))
            mirror.request = Mock(return_value={})
            mirror._ensure_release = Mock()

            with self.assertRaises(Exception):
                mirror.publish_app({
                    "id": "source-project", "slug": "owner-source-project", "name": "Source Project",
                    "repository_url": "https://github.com/owner/source-project",
                    "github_repo_id": "1234567", "owner": "owner",
                    "license_spdx": "MIT", "license_eligible": True,
                }, [{
                    "filename": "fake-installer.exe", "platform": "windows",
                    "package_type": "Windows executable", "sha256": digest,
                    "storage_path": str(path),
                }])

            mirror._ensure_release.assert_not_called()
            mirror.request.assert_not_called()


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

    def test_dispatch_scout_queues_bounded_global_repository_scan(self):
        mirror = GitHubReleaseMirror("test-token", "owner/Apphoryn", "main")
        mirror.request = Mock(return_value={})
        mirror.dispatch_scout(max_repositories=500, max_pages=8, skip_seeds=True)
        method, endpoint, payload = mirror.request.call_args.args
        self.assertEqual(method, "POST")
        self.assertTrue(endpoint.endswith("/actions/workflows/harvest.yml/dispatches"))
        self.assertEqual(payload["ref"], "main")
        self.assertEqual(payload["inputs"], {
            "max_repositories": "500", "max_pages": "8", "skip_seeds": True,
        })

    def test_dispatch_scout_rejects_unbounded_limits(self):
        mirror = GitHubReleaseMirror("test-token", "owner/Apphoryn")
        mirror.request = Mock(return_value={})
        with self.assertRaisesRegex(Exception, "max_repositories"):
            mirror.dispatch_scout(max_repositories=10001)
        mirror.request.assert_not_called()

    def test_duplicate_build_output_updates_build_ref_without_empty_release(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            binary = root / "tool.exe"
            digest = minimal_pe(binary)
            manifest = {"schema_version": 1, "apps": [{
                "id": "tool", "slug": "owner-tool", "name": "Tool",
                "repository_url": "https://github.com/owner/tool",
                "license_spdx": "MIT", "license_eligible": True,
                "project_status": "installable",
                "artifacts": [{"sha256": digest, "filename": "windows-tool.exe", "platform": "windows"}],
            }]}
            mirror = GitHubReleaseMirror("test-token", "owner/Apphoryn")
            mirror._read_catalog = Mock(return_value=(manifest, "manifest-sha"))
            mirror.request = Mock(return_value={})
            mirror._ensure_release = Mock()
            mirror._upload_asset = Mock()

            result = mirror.publish_built_artifacts(
                "https://github.com/owner/tool", "v2.0.0", root
            )

            self.assertEqual(result["published"], [])
            self.assertEqual(result["already_present"], 1)
            mirror._ensure_release.assert_not_called()
            mirror._upload_asset.assert_not_called()
            _, _, payload = mirror.request.call_args.args
            written = json.loads(base64.b64decode(payload["content"]).decode("utf-8"))
            self.assertIn("v2.0.0", written["apps"][0]["build_refs"])
            self.assertEqual(written["apps"][0]["project_status"], "installable")

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


    def test_register_project_keeps_source_only_project_out_of_installable_release_flow(self):
        manifest = {"schema_version": 1, "apps": []}
        mirror = GitHubReleaseMirror("test-token", "owner/Apphoryn")
        mirror._read_catalog = Mock(return_value=(manifest, None))
        mirror.request = Mock(return_value={})
        mirror._ensure_release = Mock()

        project = {
            "id": "project-id", "slug": "owner-source-project", "name": "Source Project",
            "summary": "A buildable open-source project", "description": "Source only so far.",
            "repository_url": "https://github.com/owner/source-project", "github_repo_id": "123456789",
            "owner": "owner", "license_spdx": "MIT", "license_eligible": True,
            "category": "developer", "topics": ["desktop"], "default_branch": "main",
            "source_commit_sha": "a" * 40,
        }
        result = mirror.register_project(project)

        self.assertEqual(result["project_status"], "source_available")
        mirror._ensure_release.assert_not_called()
        method, endpoint, payload = mirror.request.call_args.args
        self.assertEqual(method, "PUT")
        self.assertIn("apps/web/catalog/index.json", endpoint)
        stored = json.loads(base64.b64decode(payload["content"]).decode("utf-8"))["apps"][0]
        self.assertEqual(stored["repository_url"], project["repository_url"])
        self.assertEqual(stored["project_status"], "source_available")
        self.assertEqual(stored["artifacts"], [])
        self.assertEqual(stored["github_repo_id"], "123456789")

    def test_register_project_updates_same_record_after_upstream_rename(self):
        manifest = {"schema_version": 1, "apps": [
            {
                "id": "stable-id", "slug": "old-project", "name": "Old Project",
                "repository_url": "https://github.com/old/project",
                "github_repo_id": "7654321", "license_spdx": "MIT",
                "license_eligible": True, "artifacts": [],
            },
            {
                "id": "legacy-duplicate", "slug": "legacy-project-copy", "name": "Legacy Duplicate",
                "repository_url": "https://github.com/legacy/project-copy",
                "github_repo_id": "7654321", "license_spdx": "MIT",
                "license_eligible": True,
                "artifacts": [{
                    "id": "existing-package", "sha256": "c" * 64, "filename": "existing.exe",
                    "platform": "windows", "download_url": "https://github.com/owner/Apphoryn/releases/download/old/existing.exe",
                }],
            },
        ]}
        mirror = GitHubReleaseMirror("test-token", "owner/Apphoryn")
        mirror._read_catalog = Mock(return_value=(manifest, "old-sha"))
        mirror.request = Mock(return_value={})
        result = mirror.register_project({
            "id": "new-generated-id", "slug": "new-owner-project", "name": "Renamed Project",
            "repository_url": "https://github.com/new-owner/project",
            "github_repo_id": "7654321", "owner": "new-owner",
            "license_spdx": "MIT", "license_eligible": True,
            "category": "developer", "topics": [],
        })
        _, _, payload = mirror.request.call_args.args
        written = json.loads(base64.b64decode(payload["content"]).decode("utf-8"))
        self.assertEqual(result["id"], "stable-id")
        self.assertEqual(len(written["apps"]), 1)
        self.assertEqual(written["apps"][0]["repository_url"], "https://github.com/new-owner/project")
        self.assertEqual(written["apps"][0]["id"], "stable-id")
        self.assertEqual([item["sha256"] for item in written["apps"][0]["artifacts"]], ["c" * 64])


if __name__ == "__main__":
    unittest.main()
