import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch
import unittest

from app.artifact_validation import ArtifactValidationError, validate_artifact


def pe_fixture(*, dll: bool = False) -> bytes:
    data = bytearray(512)
    pe_offset = 128
    data[:2] = b"MZ"
    data[0x3C:0x40] = pe_offset.to_bytes(4, "little")
    data[pe_offset:pe_offset + 4] = b"PE\\0\\0"
    data[pe_offset + 4:pe_offset + 6] = (0x8664).to_bytes(2, "little")
    data[pe_offset + 6:pe_offset + 8] = (1).to_bytes(2, "little")
    data[pe_offset + 20:pe_offset + 22] = (0xF0).to_bytes(2, "little")
    characteristics = 0x0002 | (0x2000 if dll else 0)
    data[pe_offset + 22:pe_offset + 24] = characteristics.to_bytes(2, "little")
    optional = pe_offset + 24
    data[optional:optional + 2] = (0x20B).to_bytes(2, "little")
    data[optional + 16:optional + 20] = (0x1000).to_bytes(4, "little")
    return bytes(data)


def elf_fixture(*, shared_library: bool = False, entry_point: int = 0x401000) -> bytes:
    data = bytearray(64)
    data[:4] = b"\\x7fELF"
    data[4] = 2
    data[5] = 1
    data[6] = 1
    data[16:18] = (3 if shared_library else 2).to_bytes(2, "little")
    data[18:20] = (62).to_bytes(2, "little")
    data[20:24] = (1).to_bytes(4, "little")
    data[24:32] = entry_point.to_bytes(8, "little")
    return bytes(data)


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

    def test_pe_application_is_accepted_but_dll_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "app.exe"
            path.write_bytes(pe_fixture())
            result = validate_artifact(path)
            self.assertTrue(result["valid"])
            self.assertEqual(result["format"], "exe")

            dll = Path(folder) / "library.exe"
            dll.write_bytes(pe_fixture(dll=True))
            with self.assertRaisesRegex(ArtifactValidationError, "PE header"):
                validate_artifact(dll)

    def test_linux_pie_executable_is_accepted_but_shared_library_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "app.bin"
            path.write_bytes(elf_fixture())
            result = validate_artifact(path)
            self.assertTrue(result["valid"])
            self.assertEqual(result["format"], "bin")

            library = Path(folder) / "libexample.bin"
            library.write_bytes(elf_fixture(shared_library=True, entry_point=0))
            with self.assertRaisesRegex(ArtifactValidationError, "shared libraries"):
                validate_artifact(library)

    def test_library_jar_without_main_class_is_not_installable(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "library.jar"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n\n")
                archive.writestr("org/example/Library.class", b"compiled class")
            with self.assertRaisesRegex(ArtifactValidationError, "no Main-Class"):
                validate_artifact(path)

    def test_executable_jar_with_main_class_is_accepted(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "application.jar"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(
                    "META-INF/MANIFEST.MF",
                    "Manifest-Version: 1.0\nMain-Class: org.example.Main\n\n",
                )
                archive.writestr("org/example/Main.class", b"compiled class")
            result = validate_artifact(path)
            self.assertTrue(result["valid"])
            self.assertEqual(result["format"], "jar")

if __name__ == "__main__":
    unittest.main()
