from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_forward_media_saver.media_saver import (  # noqa: E402
    SaveContext,
    build_folder,
)
from astrbot_plugin_forward_media_saver.naming import (  # noqa: E402
    build_template_filename,
    build_template_folder,
    build_values,
    render_template,
)


class NamingTests(unittest.TestCase):
    def values(self):
        return build_values(
            scope="group",
            scope_id="123456",
            scope_name="测试群",
            sender_id="10001",
            sender_name="小明",
            kind="video",
            timestamp=1700000000,
            sequence=3,
            sha256="abcdef123456",
            original_name="a.mp4",
        )

    def test_render(self):
        self.assertEqual(
            render_template("{scope}_{scope_id}", self.values()), "group_123456"
        )
        self.assertEqual(
            render_template("x_{unknown}_y", self.values()), "x_{unknown}_y"
        )

    def test_folder_template(self):
        folder = build_template_folder(
            Path("/tmp/media"), "{date}/{scope_name}/{kind}", self.values()
        )
        self.assertIn("2023-11-15", str(folder))
        self.assertEqual(folder.name, "video")

    def test_media_timestamp(self):
        values = build_values(
            scope="group",
            scope_id="1",
            scope_name="群",
            sender_id="2",
            sender_name="人",
            kind="image",
            timestamp=1700000000,
            media_timestamp=1600000000,
            sequence=1,
            sha256="abcdef12",
        )
        self.assertEqual(values["date"], "2023-11-15")
        self.assertEqual(values["media_date"], "2020-09-13")

    def test_timezone(self):
        utc = build_values(
            scope="group",
            scope_id="1",
            scope_name="群",
            sender_id="2",
            sender_name="人",
            kind="image",
            timestamp=1700000000,
            sequence=1,
            sha256="abcdef12",
            timezone="UTC",
        )
        shanghai = build_values(
            scope="group",
            scope_id="1",
            scope_name="群",
            sender_id="2",
            sender_name="人",
            kind="image",
            timestamp=1700000000,
            sequence=1,
            sha256="abcdef12",
            timezone="Asia/Shanghai",
        )
        self.assertEqual(utc["date"], "2023-11-14")
        self.assertEqual(shanghai["date"], "2023-11-15")

    def test_legacy_folder_timezone(self):
        context = SaveContext(scope="group", scope_id="1")
        utc = build_folder(Path("/tmp/media"), "date", context, 1700000000, "UTC")
        shanghai = build_folder(
            Path("/tmp/media"), "date", context, 1700000000, "Asia/Shanghai"
        )
        self.assertEqual(utc.name, "2023-11-14")
        self.assertEqual(shanghai.name, "2023-11-15")

    def test_filename_template(self):
        name = build_template_filename(
            "{kind}_{sender}_{seq}_{hash}", self.values(), "fallback"
        )
        self.assertEqual(name, "video_10001_03_abcdef12")

    def test_unsafe_template_is_sanitized(self):
        folder = build_template_folder(
            Path("/tmp/media"), "../../{scope_id}", self.values()
        )
        self.assertTrue(str(folder).startswith("/tmp/media/"))
        self.assertNotIn("..", str(folder))


if __name__ == "__main__":
    unittest.main()
