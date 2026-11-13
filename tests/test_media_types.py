from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_forward_media_saver.media_types import (  # noqa: E402
    kind_ext_from_name,
    sanitize_component,
    sniff_audio,
    sniff_media,
)


class SniffTests(unittest.TestCase):
    def test_png(self):
        head = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
        self.assertEqual(sniff_media(head), ("image", "png"))

    def test_jpeg(self):
        self.assertEqual(
            sniff_media(b"\xff\xd8\xff\xe0" + b"\x00" * 20), ("image", "jpg")
        )

    def test_gif(self):
        self.assertEqual(sniff_media(b"GIF89a" + b"\x00" * 20), ("image", "gif"))

    def test_webp(self):
        self.assertEqual(
            sniff_media(b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 10),
            ("image", "webp"),
        )

    def test_mp4(self):
        head = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00" + b"\x00" * 10
        self.assertEqual(sniff_media(head), ("video", "mp4"))

    def test_mov(self):
        head = b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00" + b"\x00" * 10
        self.assertEqual(sniff_media(head), ("video", "mov"))

    def test_webm(self):
        head = b"\x1aE\xdf\xa3" + b"\x00" * 20 + b"webm" + b"\x00" * 10
        self.assertEqual(sniff_media(head), ("video", "webm"))

    def test_audio(self):
        self.assertEqual(sniff_audio(b"ID3" + b"\x00" * 20), "mp3")
        self.assertEqual(sniff_audio(b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 8), "wav")
        self.assertEqual(sniff_audio(b"#!AMR\n" + b"\x00" * 8), "amr")

    def test_unknown(self):
        self.assertEqual(sniff_media(b"<html>not media</html>"), (None, None))

    def test_empty(self):
        self.assertEqual(sniff_media(b""), (None, None))


class SanitizeTests(unittest.TestCase):
    def test_path_traversal(self):
        self.assertNotIn("..", sanitize_component("../../etc/passwd"))
        self.assertNotIn("/", sanitize_component("../../etc/passwd"))

    def test_empty_fallback(self):
        self.assertEqual(sanitize_component("..."), "unknown")

    def test_chinese_is_preserved(self):
        self.assertEqual(sanitize_component("测试群"), "测试群")
        self.assertEqual(sanitize_component("你好/世界"), "你好_世界")

    def test_windows_reserved_name(self):
        self.assertEqual(sanitize_component("CON"), "CON_")

    def test_limit(self):
        self.assertLessEqual(len(sanitize_component("a" * 200)), 64)


class NameExtTests(unittest.TestCase):
    def test_image(self):
        self.assertEqual(kind_ext_from_name("a.JPEG"), ("image", "jpg"))
        self.assertEqual(kind_ext_from_name("a.webp"), ("image", "webp"))

    def test_video(self):
        self.assertEqual(kind_ext_from_name("a.mp4"), ("video", "mp4"))

    def test_audio(self):
        self.assertEqual(kind_ext_from_name("a.amr"), ("audio", "amr"))

    def test_unknown(self):
        self.assertEqual(kind_ext_from_name("a.image"), (None, None))


if __name__ == "__main__":
    unittest.main()
