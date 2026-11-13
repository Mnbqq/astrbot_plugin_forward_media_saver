from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_forward_media_saver.path_utils import (  # noqa: E402
    is_local_path,
    local_path_exists,
    normalize_path_candidates,
)


class PathUtilsTests(unittest.TestCase):
    def test_is_local_path(self):
        self.assertTrue(is_local_path("/app/.config/QQ/a.mp4"))
        self.assertTrue(is_local_path(r"C:\Users\a\a.mp4"))
        self.assertTrue(is_local_path("C:/Users/a/a.mp4"))
        self.assertTrue(is_local_path("file:///tmp/a.mp4"))
        self.assertFalse(is_local_path("https://e.com/a.mp4"))
        self.assertFalse(is_local_path("a.mp4"))

    def test_windows_candidates(self):
        candidates = normalize_path_candidates(r"C:\Users\a b\1.mp4")
        self.assertIn(r"C:\Users\a b\1.mp4", candidates)
        self.assertIn("C:/Users/a b/1.mp4", candidates)

    def test_quoted_path_and_file_uri(self):
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as handle:
            path = Path(handle.name)
        try:
            self.assertEqual(local_path_exists(f'"{path}"'), path.resolve())
            self.assertEqual(local_path_exists(path.as_uri()), path.resolve())
        finally:
            path.unlink(missing_ok=True)

    def test_file_uri_with_space(self):
        with tempfile.NamedTemporaryFile(
            prefix="a b ", suffix=".mp4", delete=False
        ) as handle:
            path = Path(handle.name)
        try:
            self.assertEqual(local_path_exists(path.as_uri()), path.resolve())
        finally:
            path.unlink(missing_ok=True)

    def test_missing_path(self):
        self.assertIsNone(local_path_exists("/definitely/not/exist.mp4"))


if __name__ == "__main__":
    unittest.main()
