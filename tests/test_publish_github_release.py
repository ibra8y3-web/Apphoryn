import tempfile
import unittest
from pathlib import Path

from scripts.publish_github_release import has_build_outputs


class PublishBuildOutputTests(unittest.TestCase):
    def test_missing_output_directory_is_a_clean_noop(self):
        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder) / "artifacts"
            self.assertFalse(has_build_outputs(missing))

    def test_empty_output_directory_is_a_clean_noop(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertFalse(has_build_outputs(folder))

    def test_nested_build_output_is_detected(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "linux" / "appimage"
            output.mkdir(parents=True)
            (output / "Application.AppImage").write_bytes(b"real-output-placeholder")
            self.assertTrue(has_build_outputs(folder))


if __name__ == "__main__":
    unittest.main()
