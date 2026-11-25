import hashlib
import tempfile
import unittest
from pathlib import Path

from app.artifact_store import ArtifactStore, ArtifactStoreError


class ArtifactStoreTests(unittest.TestCase):
    def test_objects_are_content_addressed_and_identical_files_are_deduplicated(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first = root / "installer-one.exe"
            second = root / "installer-renamed.exe"
            payload = b"verified package fixture" * 20
            first.write_bytes(payload)
            second.write_bytes(payload)
            digest = hashlib.sha256(payload).hexdigest()
            store = ArtifactStore(root / "store")

            stored_first = store.put(first, expected_sha256=digest, max_bytes=4096)
            stored_second = store.put(second, expected_sha256=digest, max_bytes=4096)

            self.assertEqual(stored_first, stored_second)
            self.assertTrue(stored_first.is_file())
            self.assertEqual(stored_first.name, f"{digest}.exe")
            self.assertTrue(store.contains(stored_first))
            self.assertEqual(stored_first.read_bytes(), payload)

    def test_storage_rejects_digest_mismatch_without_persisting_an_object(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "installer.apk"
            source.write_bytes(b"package fixture" * 20)
            store = ArtifactStore(root / "store")

            with self.assertRaisesRegex(ArtifactStoreError, "does not match"):
                store.put(source, expected_sha256="0" * 64)

            objects = list((root / "store").rglob("*.apk"))
            self.assertEqual(objects, [])

    def test_storage_rejects_symlink_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            real_file = root / "real.exe"
            real_file.write_bytes(b"package fixture" * 20)
            link = root / "linked.exe"
            try:
                link.symlink_to(real_file)
            except (OSError, NotImplementedError):
                self.skipTest("Symbolic links are not available on this runner.")
            store = ArtifactStore(root / "store")

            with self.assertRaisesRegex(ArtifactStoreError, "non-symlink"):
                store.put(link)

    def test_storage_rejects_non_package_extensions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "README"
            source.write_bytes(b"not an installer" * 20)
            store = ArtifactStore(root / "store")
            with self.assertRaisesRegex(ArtifactStoreError, "package extension"):
                store.put(source)


if __name__ == "__main__":
    unittest.main()
