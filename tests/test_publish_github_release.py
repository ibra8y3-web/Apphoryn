import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts.publish_github_release import has_build_outputs, prepare_android_build_outputs


class PublishBuildOutputTests(unittest.TestCase):
    def test_missing_output_directory_is_a_clean_noop(self):
        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder) / "artifacts"
            self.assertFalse(has_build_outputs(missing))

    def test_empty_output_directory_is_a_clean_noop(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertFalse(has_build_outputs(folder))

    def test_unsigned_android_release_is_not_published_without_persistent_signing_key(self):
        with tempfile.TemporaryDirectory() as folder:
            apk = Path(folder) / "application-release.apk"
            with zipfile.ZipFile(apk, "w") as archive:
                archive.writestr("AndroidManifest.xml", b"manifest")
                archive.writestr("classes.dex", b"dex\\n035\\0" + b"0" * 64)
            with patch.dict("os.environ", {
                "APPHORYN_ANDROID_KEYSTORE_BASE64": "",
                "APPHORYN_ANDROID_KEYSTORE_PASSWORD": "",
                "APPHORYN_ANDROID_KEY_ALIAS": "",
                "APPHORYN_ANDROID_KEY_PASSWORD": "",
                "ANDROID_HOME": "",
                "ANDROID_SDK_ROOT": "",
            }), patch("scripts.publish_github_release.shutil.which", return_value=None):
                result = prepare_android_build_outputs(folder)
            self.assertEqual(result["not_published"], 1)
            self.assertFalse(apk.exists())
            self.assertTrue((Path(folder) / "application-release.apk.unsigned").is_file())
            self.assertFalse(has_build_outputs(folder))

    def test_nested_build_output_is_detected(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "linux" / "appimage"
            output.mkdir(parents=True)
            (output / "Application.AppImage").write_bytes(b"real-output-placeholder")
            self.assertTrue(has_build_outputs(folder))


if __name__ == "__main__":
    unittest.main()
