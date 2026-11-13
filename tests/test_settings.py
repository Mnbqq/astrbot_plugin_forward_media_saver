from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_forward_media_saver.settings import Settings  # noqa: E402


class SettingsTests(unittest.TestCase):
    def test_defaults(self):
        settings = Settings.from_mapping({})
        self.assertTrue(settings.enabled)
        self.assertTrue(settings.save_images)
        self.assertTrue(settings.save_videos)
        self.assertEqual(settings.max_depth, 3)
        self.assertEqual(settings.folder_layout, "date_scope")
        self.assertFalse(settings.dedupe)
        self.assertFalse(settings.notify)
        self.assertEqual(settings.min_video_seconds, 0)
        self.assertFalse(settings.skip_unknown_video_duration)
        self.assertEqual(settings.timezone, "Asia/Shanghai")

    def test_list_can_be_comma_string(self):
        settings = Settings.from_mapping(
            {"group_whitelist": "123, 456，789", "user_blacklist": ["1", "2"]}
        )
        self.assertEqual(settings.group_whitelist, frozenset({"123", "456", "789"}))
        self.assertEqual(settings.user_blacklist, frozenset({"1", "2"}))

    def test_numbers_are_clamped(self):
        settings = Settings.from_mapping(
            {
                "max_depth": 99,
                "max_file_mb": 0,
                "concurrency": -1,
                "max_media_per_forward": "abc",
            }
        )
        self.assertEqual(settings.max_depth, 10)
        self.assertEqual(settings.max_file_mb, 1)
        self.assertEqual(settings.concurrency, 1)
        self.assertEqual(settings.max_media_per_forward, 100)

    def test_invalid_layout_falls_back(self):
        self.assertEqual(
            Settings.from_mapping({"folder_layout": "wat"}).folder_layout,
            "date_scope",
        )

    def test_group_permissions(self):
        settings = Settings.from_mapping(
            {"group_whitelist": ["100"], "group_blacklist": ["200"]}
        )
        self.assertTrue(settings.group_allowed("100"))
        self.assertFalse(settings.group_allowed("999"))
        self.assertFalse(settings.group_allowed("200"))
        self.assertFalse(settings.group_allowed(""))

    def test_user_permissions(self):
        settings = Settings.from_mapping({"user_blacklist": ["42"]})
        self.assertFalse(settings.user_allowed("42"))
        self.assertTrue(settings.user_allowed("43"))

    def test_media_kind_switch(self):
        settings = Settings.from_mapping({"save_images": False, "save_videos": True})
        self.assertFalse(settings.media_kind_enabled("image"))
        self.assertTrue(settings.media_kind_enabled("video"))
        self.assertFalse(settings.media_kind_enabled("audio"))

    def test_bool_strings(self):
        settings = Settings.from_mapping(
            {"enabled": "false", "save_images": "yes", "notify": 1}
        )
        self.assertFalse(settings.enabled)
        self.assertTrue(settings.save_images)
        self.assertTrue(settings.notify)


if __name__ == "__main__":
    unittest.main()
