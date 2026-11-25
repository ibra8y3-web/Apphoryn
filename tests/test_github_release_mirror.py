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
    def test_read_catalog_treats_zero_byte_manifest_as_empty_and_keeps_sha(self):
        mirror = GitHubReleaseMirror("test-token", "owner/Apphoryn")
        mirror.request = Mock(return_value={
            "type": "file", "encoding": "none", "size": 0,
            "content": "", "sha": "empty-blob-sha",
        })

        manifest, sha = mirror._read_catalog()

        self.assertEqual(manifest, {"schema_version": 1, "generated_at": None, "apps": []})
        self.assertEqual(sha, "empty-blob-sha")

    def test_read_catalog_uses_git_blob_when_contents_api_omits_large_file_body(self):
        manifest_payload = {
            "schema_version": 1,
            "generated_at": "2026-11-25T00:00:00+00:00",
            "apps": [{"id": "existing-app", "name": "Existing App"}],
        }
        blob_sha = "a" * 40
        encoded = base64.b64encode(
            json.dumps(manifest_payload).encode("utf-8")
        ).decode("ascii")
        mirror = GitHubReleaseMirror("test-token", "owner/Apphoryn")
        mirror.request = Mock(side_effect=[
            {
                "type": "file",
                "encoding": "none",
                "size": 1_500_000,
                "content": "",
                "sha": blob_sha,
            },
            {"encoding": "base64", "content": encoded},
        ])

        manifest, sha = mirror._read_catalog()

        self.assertEqual(manifest, manifest_payload)
        self.assertEqual(sha, blob_sha)
        self.assertEqual(mirror.request.call_count, 2)
        self.assertIn(f"/git/blobs/{blob_sha}", mirror.request.call_args.args[1])

    def test_source_only_import_does_not_create_license_notice_release(self):
        mirror = GitHubReleaseMirror("test-token", "owner/Apphoryn")
        mirror._read_catalog = Mock(return_value=({"schema_version": 1, "apps": []}, None))
        mirror._ensure_release = Mock()
        mirror._upload_notice = Mock()
        mirror.request = Mock(return_value={})
        mirror._write_catalog = Mock()

        result = mirror.publish_app({
            "id": "source-project",
            "slug": "owner-source-project",
            "name": "Source Project",
            "repository_url": "https://github.com/owner/source-project",
            "owner": "owner",
            "license_spdx": "MIT",
            "license_eligible": True,
            "icon_url": "https://avatars.githubusercontent.com/u/12345?v=4",
            "translations": {"en": "English", "ar": "العربية"},
            "description_translations": {"en": "Description", "ar": "الوصف"},
            "translation_status": "complete",
            "ai_provider": "openrouter",
            "ai_model": "example:free",
        }, [])

        self.assertIsNone(result["release_url"])
        self.assertEqual(result["mirrored_artifacts"], [])
        self.assertTrue(result["source_project_registered"])
        mirror._ensure_release.assert_not_called()
        mirror._upload_notice.assert_not_called()
        mirror.request.assert_not_called()
        written_manifest = mirror._write_catalog.call_args.args[0]
        source_record = written_manifest["apps"][0]
        self.assertEqual(source_record["icon_url"], "https://avatars.githubusercontent.com/u/12345?v=4")
        self.assertEqual(source_record["description_translations"]["ar"], "الوصف")
        self.assertEqual(source_record["translation_status"], "complete")
        self.assertEqual(source_record["ai_provider"], "openrouter")
        self.assertEqual(source_record["project_status"], "source_available")

    def test_publish_ai_status_persists_only_sanitized_provider_health(self):
        mirror = GitHubReleaseMirror("test-token", "owner/Apphoryn")
        mirror._read_catalog = Mock(return_value=({
            "schema_version": 1,
            "generated_at": None,
            "apps": [{"id": "existing-app"}],
        }, "manifest-sha"))
        mirror._write_catalog = Mock()
        mirror.publish_ai_status({
            "checked_at": "2026-11-25T00:00:00+00:00",
            "free_only": True,
            "available_model_count": 2,
            "providers": [
                {"provider": "openrouter", "state": "available", "models_discovered": 2, "models": [{"model": "vendor/model:free", "free_class": "free_variant"}], "api_key": "must-not-persist"},
                {"provider": "gemini", "state": "paused", "models_discovered": 0},
            ],
        })
        written = mirror._write_catalog.call_args.args[0]
        health = written["ai_status"]
        self.assertEqual(health["available_model_count"], 2)
        self.assertEqual(health["available_provider_count"], 1)
        self.assertEqual([item["state"] for item in health["providers"]], ["available", "paused"])
        self.assertEqual(health["providers"][0]["models"][0]["model"], "vendor/model:free")
        self.assertNotIn("api_key", json.dumps(health))
        self.assertEqual(written["apps"], [{"id": "existing-app"}])

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
