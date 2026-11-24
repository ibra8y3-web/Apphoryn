import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch
import unittest

from app.artifact_validation import ArtifactValidationError, validate_artifact


class ArtifactValidationTests(unittest.TestCase):
    def test_rejects_random_bytes_with_apk_extension(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fake.apk"
            path.write_bytes(b"this is not an apk" * 16)
            with self.assertRaises(ArtifactValidationError):
                validate_artifact(path)

    def test_apk_requires_android_manifest_and_code_before_signature_check(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "missing-code.apk"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("AndroidManifest.xml", b"manifest")
            with self.assertRaisesRegex(ArtifactValidationError, "DEX/native code"):
                validate_artifact(path, require_apk_signature=False)

    def test_apk_signature_fails_closed_when_apksigner_is_unavailable(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "unsigned.apk"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("AndroidManifest.xml", b"manifest")
                archive.writestr("classes.dex", b"dex\n035\0" + b"0" * 64)
            with patch("app.artifact_validation.shutil.which", return_value=None):
                with self.assertRaisesRegex(ArtifactValidationError, "apksigner is not installed"):
                    validate_artifact(path, require_apk_signature=True)

    def test_apk_verifier_records_signer_certificate_fingerprint(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "signed.apk"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("AndroidManifest.xml", b"manifest")
                archive.writestr("classes.dex", b"dex\n035\0" + b"0" * 64)
            fingerprint = "AA:" * 31 + "AA"
            completed = type("Completed", (), {"returncode": 0, "stdout": f"Signer #1 certificate SHA-256 digest: {fingerprint}", "stderr": ""})()
            with patch("app.artifact_validation.shutil.which", return_value="apksigner"), patch("app.artifact_validation.subprocess.run", return_value=completed):
                result = validate_artifact(path, require_apk_signature=True)
            self.assertEqual(result["signature_status"], "verified")
            self.assertEqual(result["signer_certificate_sha256"], "aa" * 32)

    def test_pkg_checks_xar_header_length_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "installer.pkg"
            header = bytearray(28)
            header[:4] = b"xar!"
            header[4:6] = (28).to_bytes(2, "big")
            header[6:8] = (1).to_bytes(2, "big")
            header[8:16] = (0).to_bytes(8, "big")
            header[16:24] = (0).to_bytes(8, "big")
            header[24:28] = (1).to_bytes(4, "big")
            path.write_bytes(header + bytes(36))
            result = validate_artifact(path)
            self.assertEqual(result["format"], "pkg")

    def test_pe_header_is_checked_not_only_file_extension(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "app.exe"
            content = bytearray(256)
            content[:2] = b"MZ"
            content[0x3C:0x40] = (128).to_bytes(4, "little")
            content[128:132] = b"PE\0\0"
            path.write_bytes(content)
            result = validate_artifact(path)
            self.assertTrue(result["valid"])
            self.assertEqual(result["format"], "exe")


if __name__ == "__main__":
    unittest.main()
